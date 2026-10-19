"""バックログ実行の記録（ログファイル・JSONレポート・異常検出・前回比）のテスト（2026-10-19追加）

手動でバックログ処理を実行したときに、動向とおかしな点を後から確認できることを保証する。
"""
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(ac, "BACKLOG_LOG_DIR", tmp_path / "default_logs")  # リポジトリへ書かない
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 10)
    monkeypatch.setattr(ac, "ARTICLE_FETCH_SLEEP", 0)
    monkeypatch.setattr(ac, "close_browser", lambda: None)
    monkeypatch.setattr(ac, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))


def _ok():
    return {"ok": True, "title": "t", "text": "本文" * 50, "final_url": None, "published_at": None}


def _fail(msg="403 Forbidden"):
    return {"ok": False, "error": msg, "http_status": 403}


def _target(tid, method="HTML"):
    return {"crawl_target_id": tid, "target_name": f"サイト{tid}", "publisher_name": f"サイト{tid}",
            "crawl_method": method, "lookback_days": 7}


def _log_row(log_id, tid, notes, hours_ago=3):
    return {"crawl_log_id": log_id, "crawl_target_id": tid,
            "started_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "items_detected": 0, "new_items": 0, "updated_items": 0, "extraction_failures": 0,
            "error_message": None, "run_result": "更新なし", "notes": notes}


def _backlog_client():
    """記事単位の再試行待ち2件(log-pend) + 一覧取得が未完了2対象 + 期限切れ1行"""
    pend = ac._compose_notes("", {"p1_article_failed": 2, "p1_pending": [{"u": "https://x/1"}, {"u": "https://x/2"}]})
    listing = ac._compose_notes("", {"p1_listing_failed": 1})
    routed = ac._compose_notes("", {"p1_routed_to_pass2": "Zyte"})
    old = ac._compose_notes("", {"p1_article_failed": 1, "p1_pending": [{"u": "https://old/1"}]})
    return FakeSupabaseClient({
        "crawl_targets": [_target("t-1"), _target("t-2"), _target("t-3", "Zyte")],
        "crawl_logs": [_log_row("log-pend", "t-1", pend), _log_row("log-listing", "t-2", listing),
                       _log_row("log-routed", "t-3", routed), _log_row("log-old", "t-1", old, hours_ago=100)]})


def _healthy(monkeypatch):
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _ok())
    monkeypatch.setattr(ac, "_list_candidates_for_target", lambda *a, **k: ([], 200, False, None, False, False, False))
    monkeypatch.setattr(ac, "_resolve_tiered_listing", lambda *a, **k: ("zyte", [], 200, False, None, False))


def _run(tmp_path, client=None, **kw):
    return ac.run_pass2_backlog(budget_sec=kw.pop("budget_sec", 600), client=client or _backlog_client(),
                                proxies={}, verify=True, log_dir=tmp_path / "L", **kw)


def _json(tmp_path):
    return json.loads(next((tmp_path / "L").glob("*_backlog.json")).read_text(encoding="utf-8"))


def _logtext(tmp_path):
    return next((tmp_path / "L").glob("*_backlog.log")).read_text(encoding="utf-8")


# ── ログファイルとJSONレポート ──
def test_run_writes_log_file_and_json_report_with_the_key_numbers(monkeypatch, tmp_path, capsys):
    _healthy(monkeypatch)

    result = _run(tmp_path)

    names = sorted(p.name for p in (tmp_path / "L").iterdir())
    assert len(names) == 2 and names[0].endswith("_backlog.json") and names[1].endswith("_backlog.log")
    report = _json(tmp_path)
    assert report["before"] == {"article_items": 2, "listing_items": 2, "expired": 1}
    assert report["after"]["article_items"] == 0                      # 処理後に残った繰越も記録する
    assert report["articles"]["recovered"] == 2 and report["articles"]["final_failed"] == 0
    assert report["articles"]["tiers"]["tinyfish_fetch"][:2] == [2, 2]
    assert len(report["listing"]) == 2 and {x["outcome"] for x in report["listing"]} == {"ok"}
    assert report["budget_sec"] == 600 and report["expired"] == 1 and report["error"] is None
    assert result["report_path"].endswith("_backlog.json") and result["anomalies"] == report["anomalies"]
    # 画面に出した内容が、そのままログファイルにも残る（手動実行でも後から追える）
    assert "[backlog-summary]" in _logtext(tmp_path) and "Pass2バックログ:" in _logtext(tmp_path)
    assert "[backlog-summary]" in capsys.readouterr().out


def test_run_writes_report_even_when_an_exception_stops_the_run(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("TinyFish接続エラー")

    monkeypatch.setattr(ac, "escalate_pending", boom)

    with pytest.raises(RuntimeError):
        _run(tmp_path)

    report = _json(tmp_path)
    assert "RuntimeError: TinyFish接続エラー" in report["error"]
    assert any("例外" in a for a in report["anomalies"])                # 異常として記録される
    assert "Traceback" in _logtext(tmp_path)


def test_run_restores_stdout_and_stderr_even_after_failure(monkeypatch, tmp_path):
    before_out, before_err = sys.stdout, sys.stderr
    monkeypatch.setattr(ac, "escalate_pending", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        _run(tmp_path)
    assert sys.stdout is before_out and sys.stderr is before_err


def test_run_compares_with_previous_run(monkeypatch, tmp_path, capsys):
    _healthy(monkeypatch)
    prev_dir = tmp_path / "L"
    prev_dir.mkdir()
    (prev_dir / "20261018_000000_backlog.json").write_text(json.dumps({
        "started_at": "2026-10-18 00:00:00 JST", "elapsed_sec": 100.0,
        "before": {"article_items": 480, "listing_items": 70, "expired": 0}}), encoding="utf-8")

    _run(tmp_path)

    out = capsys.readouterr().out
    assert "前回比: 記事480→2件、一覧取得70→2対象" in out and "2026-10-18 00:00:00 JST" in out


def test_healthy_run_reports_no_anomaly_in_the_summary(monkeypatch, tmp_path, capsys):
    _healthy(monkeypatch)
    client = _backlog_client()
    client.tables["crawl_logs"] = [r for r in client.tables["crawl_logs"] if r["crawl_log_id"] != "log-old"]  # 期限切れ行なし

    result = _run(tmp_path, client=client)

    out = capsys.readouterr().out
    assert result["anomalies"] == [] and "  異常: なし" in out and "異常の可能性" not in out


# ── 異常検出（純粋関数） ──
def _report(**over):
    base = {"budget_sec": 1000, "elapsed_sec": 500.0, "agent_cap": 30, "expired": 0, "error": None,
            "phase_sec": {"articles": 100.0, "listing": 0.0}, "listing": [],
            "articles": {"attempted_items": 10, "recovered": 8, "final_failed": 2, "unfinished": 0,
                         "agent_cap_reached": False, "tiers": {"tinyfish_fetch": [10, 8, 100.0]}}}
    base.update(over)
    return base


def test_detect_anomalies_is_empty_for_a_healthy_run():
    assert ac.detect_anomalies(_report()) == []


@pytest.mark.parametrize("override,expected", [
    ({"articles": {"attempted_items": 20, "final_failed": 20, "recovered": 0, "unfinished": 0,
                   "tiers": {"zyte": [20, 0, 50.0]}}}, "zyteが20件試して成功0件"),
    ({"articles": {"attempted_items": 10, "final_failed": 6, "tiers": {}}}, "最終失敗が6/10件"),
    ({"articles": {"attempted_items": 10, "unfinished": 4, "tiers": {}}}, "次回へ繰り越されました"),
    ({"articles": {"attempted_items": 1, "agent_cap_reached": True, "tiers": {}}}, "Agentの上限（30件）に達しました"),
    ({"listing": [{"outcome": "failed", "sec": 5}, {"outcome": "failed", "sec": 5}, {"outcome": "ok", "sec": 5}]},
     "一覧取得の再試行が2/3対象で失敗"),
    ({"listing": [{"outcome": "skipped", "sec": 0}] * 3}, "一覧取得3対象が次回へ繰り越され"),
    ({"expired": 5}, "期限切れ5ログ行"),
    ({"elapsed_sec": 1200.0}, "予算1000秒を超えました"),
    ({"phase_sec": {"articles": 900.0, "listing": 0.0}}, "記事1件あたり90秒"),
    ({"listing": [{"outcome": "ok", "sec": 400}]}, "一覧取得は1対象あたり平均400秒"),
    ({"error": "RuntimeError: x"}, "例外が発生しました"),
])
def test_detect_anomalies_flags_each_problem(override, expected):
    assert any(expected in a for a in ac.detect_anomalies(_report(**override)))


def test_tee_writes_to_both_screen_and_file_and_tolerates_closed_file(tmp_path):
    screen, f = io.StringIO(), open(tmp_path / "x.log", "w", encoding="utf-8")
    tee = ac._Tee(screen, f)
    tee.write("こんにちは\n")
    f.close()
    tee.write("閉じた後\n")                                    # 例外にならない
    assert screen.getvalue() == "こんにちは\n閉じた後\n"
    assert (tmp_path / "x.log").read_text(encoding="utf-8") == "こんにちは\n"


# ── 統計の収集（既存関数の戻り値は変えない） ──
def test_escalate_pending_collects_failure_reasons_and_tier_stats(monkeypatch):
    items = [{"target": _target("t-1"), "cand": {"url": f"https://x/{i}", "title": None, "published_at": None},
              "crawl_log_id": "log-pend", "lookback": 7, "is_bot_block": False} for i in (1, 2)]
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _fail())
    stats = {}

    ac.escalate_pending(items, _backlog_client(), {}, True, stats=stats)

    art = stats["articles"]
    assert art["attempted_items"] == 2 and art["final_failed"] == 2 and art["recovered"] == 0
    assert art["errors"]["zyte|403 Forbidden"] == 2 and art["errors"]["tinyfish_fetch|403 Forbidden"] == 2
    assert art["tiers"]["tinyfish_agent"][:2] == [2, 0]


def test_escalate_listings_records_per_target_outcome_without_changing_the_return(monkeypatch):
    items = [{"target": _target("t-2"), "crawl_log_id": "log-listing", "lookback": 7, "started_at": NOW},
             {"target": _target("t-1"), "crawl_log_id": "log-pend", "lookback": 7, "started_at": NOW}]
    outcomes = iter([([], 200, False, None, False, False, False),
                     ([], None, False, "HTTPError: 403", False, False, False)])
    monkeypatch.setattr(ac, "_list_candidates_for_target", lambda *a, **k: next(outcomes))
    stats = {}

    returned = ac.escalate_pending_listings(items, _backlog_client(), {}, True, stats=stats)

    assert returned == []                                          # 戻り値は従来どおり
    assert [x["outcome"] for x in stats["listing"]] == ["ok", "failed"]
    assert "403" in stats["listing"][1]["error"] and all("sec" in x for x in stats["listing"])
