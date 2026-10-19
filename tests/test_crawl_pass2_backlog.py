"""Pass2の取りこぼし対策のテスト（2026-10-19追加）

背景: 本番では、チャンクが時間切れで強制終了されると、メモリ上にしか無かった「再試行待ち」の記事が
Pass2に到達しないまま失われていた（再試行待ちの84〜100%にPass2の記録が無く、該当targetの
crawl_logsは「更新なし」等で残って取りこぼしが見えなかった）。
対策: ①Pass1の時点で再試行待ちをnotesへ永続化、②時間予算つきで未完了を引き継ぐバックログ処理、
③期限切れは失敗として確定、④各段階の投入数をその場で記録。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
import run_daily  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "ARTICLE_FETCH_SLEEP", 0)
    monkeypatch.setattr(ac, "close_browser", lambda: None)


def _ok():
    return {"ok": True, "title": "t", "text": "本文" * 50, "final_url": None, "published_at": None}


def _fail():
    return {"ok": False, "error": "blocked", "http_status": 403}


def _target(tid="t-1", method="HTML"):
    return {"crawl_target_id": tid, "target_name": "サイト", "publisher_name": "サイト",
            "crawl_method": method, "lookback_days": 7}


def _log_row(log_id, tid, notes, started=None):
    return {"crawl_log_id": log_id, "crawl_target_id": tid,
            "started_at": (started or NOW - timedelta(hours=3)).isoformat(),
            "items_detected": 0, "new_items": 0, "updated_items": 0, "extraction_failures": 0,
            "error_message": None, "run_result": "更新なし", "notes": notes}


def _item(url, log_id="log-1", target=None):
    return {"target": target or _target(), "cand": {"url": url, "title": "題名", "published_at": None},
            "crawl_log_id": log_id, "lookback": 7, "is_bot_block": False}


def _funnel(row):
    return ac._split_notes(row["notes"])[1]


# ─── ヘルパー ────────────────────────────────────────────────────────
def test_merge_funnel_none_deletes_key_but_keeps_others():
    merged = ac._merge_funnel({"p1_pending": [{"u": "x"}], "p1_article_failed": 1}, {"p1_pending": None, "pass2": "done"})
    assert "p1_pending" not in merged and merged["p1_article_failed"] == 1 and merged["pass2"] == "done"


def test_pending_entry_roundtrip_keeps_url_title_date_and_bot_flag():
    pub = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
    item = {"cand": {"url": "https://x/1", "title": "t" * 200, "published_at": pub}, "is_bot_block": True}
    entry = ac._pending_entry(item)
    assert entry["u"] == "https://x/1" and entry["b"] == 1 and len(entry["t"]) == 80
    cand = ac._entry_to_cand(entry)
    assert cand["url"] == "https://x/1" and cand["published_at"] == pub


# ─── ① Pass1の時点で再試行待ちを永続化する ───────────────────────────────────
def test_pass1_persists_pending_articles_in_notes_even_if_pass2_never_runs(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": []})
    cands = [{"url": f"https://x/{i}", "title": f"記事{i}", "published_at": None} for i in range(3)]
    monkeypatch.setattr(ac, "_list_candidates_for_target", lambda *a, **k: (cands, 200, False, None, False, False, False))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _fail())

    result = ac.process_target(_target(), client, {}, True)  # ← Pass2は呼ばない(=チャンクが時間切れで止まった状況)

    assert len(result["pending_escalations"]) == 3
    funnel = _funnel(client.inserted["crawl_logs"][0])
    assert funnel["p1_article_failed"] == 3
    assert [e["u"] for e in funnel["p1_pending"]] == ["https://x/0", "https://x/1", "https://x/2"]


def test_pass1_caps_stored_pending_and_records_overflow(monkeypatch):
    monkeypatch.setattr(ac, "PENDING_STORE_MAX", 2)
    client = FakeSupabaseClient({"crawl_logs": []})
    cands = [{"url": f"https://x/{i}", "title": None, "published_at": None} for i in range(5)]
    monkeypatch.setattr(ac, "_list_candidates_for_target", lambda *a, **k: (cands, 200, False, None, False, False, False))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _fail())

    ac.process_target(_target(), client, {}, True)

    funnel = _funnel(client.inserted["crawl_logs"][0])
    assert funnel["p1_article_failed"] == 5 and len(funnel["p1_pending"]) == 2
    assert funnel["p1_pending_overflow"] == 3  # 保存できなかった分は件数だけ残る（見えなくならない）


# ─── ② Pass2が解消したら再試行待ちを消し、時間切れなら残す ─────────────────────────
def _client_with_log(items):
    entries = [ac._pending_entry(i) for i in items]
    return FakeSupabaseClient({"crawl_logs": [_log_row("log-1", "t-1", ac._compose_notes(
        "", {"p1_article_failed": len(items), "p1_pending": entries}))]})


def test_escalate_pending_marks_done_and_clears_pending_when_resolved(monkeypatch):
    items = [_item("https://x/1"), _item("https://x/2")]
    client = _client_with_log(items)
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _ok())

    result = ac.escalate_pending(items, client, {}, True)

    assert result == {"recovered": 2, "final_failed": 0}
    f = _funnel(client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0])
    assert "p1_pending" not in f and f["pass2"] == "done" and f["tiers"]["tinyfish_fetch"][:2] == [2, 2]


def test_escalate_pending_deadline_keeps_unfinished_pending_and_does_not_finalize_failure(monkeypatch):
    items = [_item("https://x/1"), _item("https://x/2"), _item("https://x/3")]
    client = _client_with_log(items)
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: (True, True))
    calls = []

    def fake_extract(url, *a, **k):
        calls.append(url)
        return _ok() if url.endswith("/1") else _fail()

    monkeypatch.setattr(ac, "extract_article", fake_extract)
    # 1件目の判定では予算内(0)、2件目を始める判定で時間切れ(100)にする（1件目だけ処理して打ち切り）
    clock = iter([0, 100, 100, 100, 100, 100])
    monkeypatch.setattr(ac.time, "monotonic", lambda: next(clock))

    result = ac.escalate_pending(items, client, {}, True, deadline=50)

    assert calls == ["https://x/1"]
    assert result["final_failed"] == 0 and result["unfinished"] == 2     # 失敗として確定していない
    row = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    f = _funnel(row)
    assert [e["u"] for e in f["p1_pending"]] == ["https://x/2", "https://x/3"]  # 未完了だけが残る
    assert "pass2" not in f and row["extraction_failures"] == 0


def test_escalate_pending_records_attempts_for_logs_without_any_recovery(monkeypatch):
    """救済が1件も無かったログ行でも、各段階の投入数がその場で残る（従来は最後まで終わらないと残らなかった）"""
    items = [_item("https://x/1")]
    client = _client_with_log(items)
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _fail())

    ac.escalate_pending(items, client, {}, True)

    f = _funnel(client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0])
    assert {k: v[:2] for k, v in f["tiers"].items()} == {
        "tinyfish_fetch": [1, 0], "zyte": [1, 0], "tinyfish_agent": [1, 0]}
    assert f["final_failed"] == 1 and f["pass2"] == "done"


# ─── ③ バックログ: 未完了のPass2を復元して引き継ぐ ──────────────────────────────
def _backlog_client():
    pend = ac._compose_notes("", {"p1_article_failed": 2, "p1_pending": [
        {"u": "https://x/1", "t": "記事1"}, {"u": "https://x/2", "b": 1}]})
    listing = ac._compose_notes("", {"p1_listing_failed": 1})
    routed = ac._compose_notes("", {"p1_routed_to_pass2": "Zyte"})
    done = ac._compose_notes("", {"p1_listing_failed": 1, "listing_retry": "ok"})
    old = ac._compose_notes("", {"p1_article_failed": 1, "p1_pending": [{"u": "https://old/1"}]})
    return FakeSupabaseClient({
        "crawl_targets": [_target("t-1"), _target("t-2"), _target("t-3", "Zyte"), _target("t-4")],
        "crawl_logs": [
            _log_row("log-pend", "t-1", pend),
            _log_row("log-listing", "t-2", listing),
            _log_row("log-routed", "t-3", routed),
            _log_row("log-done", "t-4", done),                                           # 解決済み → 対象外
            _log_row("log-old", "t-1", old, started=NOW - timedelta(hours=100)),          # 期限切れ
            _log_row("log-orphan", "t-gone", pend),                                       # target消滅 → 期限扱い
        ]})


def test_load_backlog_restores_pending_listing_and_expired_rows():
    article_items, listing_items, expired = ac.load_pass2_backlog(_backlog_client(), now=NOW)

    assert sorted(i["cand"]["url"] for i in article_items) == ["https://x/1", "https://x/2"]
    assert article_items[0]["crawl_log_id"] == "log-pend"
    assert {i["crawl_log_id"] for i in article_items} == {"log-pend"}
    assert [i["is_bot_block"] for i in sorted(article_items, key=lambda i: i["cand"]["url"])] == [False, True]
    assert {i["crawl_log_id"] for i in listing_items} == {"log-listing", "log-routed"}
    routed = next(i for i in listing_items if i["crawl_log_id"] == "log-routed")
    assert routed["tiers"] == ac._ZYTE_METHOD_TIERS["Zyte"]                    # 直送対象は元の段階順で再試行
    assert "tiers" not in next(i for i in listing_items if i["crawl_log_id"] == "log-listing")
    assert {row["crawl_log_id"] for row, _, _ in expired} == {"log-old", "log-orphan"}


def test_run_backlog_resolves_pending_and_expires_old_rows_as_visible_failures(monkeypatch):
    client = _backlog_client()
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _ok())
    monkeypatch.setattr(ac, "_list_candidates_for_target", lambda *a, **k: ([], 200, False, None, False, False, False))
    monkeypatch.setattr(ac, "_resolve_tiered_listing", lambda *a, **k: ("zyte", [], 200, False, None, False))
    monkeypatch.setattr(ac, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))

    result = ac.run_pass2_backlog(budget_sec=600, client=client, proxies={}, verify=True)

    rows = {r["crawl_log_id"]: r for r in client.tables["crawl_logs"]}
    assert result["article_items"] == 2 and result["listing_items"] == 2 and result["expired"] == 2
    # 記事単位の再試行待ちは解消され、救済した2件が加算される
    f = _funnel(rows["log-pend"])
    assert "p1_pending" not in f and f["pass2"] == "done" and rows["log-pend"]["items_detected"] == 2
    # 一覧取得が未完了だったログ行も、再試行の結果が記録される
    assert _funnel(rows["log-listing"]).get("listing_retry") == "ok"
    assert _funnel(rows["log-routed"]).get("listing_retry") == "ok"
    # 期限切れは「更新なし」のまま隠さず、失敗として確定する
    old = rows["log-old"]
    assert old["extraction_failures"] == 1 and old["run_result"] == "抽出失敗"
    assert _funnel(old)["pass2"] == "expired" and "p1_pending" not in _funnel(old)


def test_run_backlog_with_zero_budget_changes_nothing_but_expiry(monkeypatch):
    """時間予算が無ければ何も試行せず、再試行待ちはそのまま次回へ繰り越される（失敗確定もしない）"""
    client = _backlog_client()
    called = []
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: called.append(1) or _ok())
    monkeypatch.setattr(ac, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))

    ac.run_pass2_backlog(budget_sec=0, client=client, proxies={}, verify=True)

    assert called == []
    rows = {r["crawl_log_id"]: r for r in client.tables["crawl_logs"]}
    f = _funnel(rows["log-pend"])
    assert [e["u"] for e in f["p1_pending"]] == ["https://x/1", "https://x/2"] and "pass2" not in f
    assert rows["log-pend"]["extraction_failures"] == 0
    assert "listing_retry" not in _funnel(rows["log-listing"])


# ─── ④ 日次パイプラインに組み込まれている ──────────────────────────────────────
def test_run_daily_runs_backlog_step_right_after_crawl_with_budget_within_timeout():
    names = [name for name, _, _ in run_daily.STEPS]
    assert names.index("crawl_pass2_backlog") == names.index("crawl") + 1
    _, args, timeout = next(s for s in run_daily.STEPS if s[0] == "crawl_pass2_backlog")
    assert "--pass2-backlog" in args and "--budget-sec" not in args   # 予算は件数から自動算出
    assert ac.BACKLOG_BUDGET_MAX_SEC < timeout  # 予算の上限の方がステップのタイムアウトより短い（強制終了される前に自分で止まる）


def test_competitor_job_that_skips_crawl_does_not_run_the_backlog_step():
    """daily-competitor-crawl(run.shが--skip-crawl等を個別指定)で、新しいバックログ処理が
    意図せず動かないこと。--skip-crawl-pass2-backlogをrun.shへ追加しなくても連動して外れる"""
    competitor_job_skips = {"crawl", "reference_list", "api_articles", "filter_coverage_check",
                            "analyze", "selector"}
    skip = run_daily.effective_skip(competitor_job_skips)
    assert "crawl_pass2_backlog" in skip
    # 記事系ジョブ(daily-crawl-analyze: --skip-competitor-crawl --skip-selector)ではバックログが動く
    assert "crawl_pass2_backlog" not in run_daily.effective_skip({"competitor_crawl", "selector"})
    assert run_daily.effective_skip(set()) == set()


def test_backlog_processes_newest_first_so_fresh_articles_win_when_budget_is_short():
    older = ac._compose_notes("", {"p1_listing_failed": 1})
    client = FakeSupabaseClient({
        "crawl_targets": [_target("t-1"), _target("t-2")],
        "crawl_logs": [
            _log_row("log-older", "t-1", older, started=NOW - timedelta(hours=40)),
            _log_row("log-newer", "t-2", older, started=NOW - timedelta(hours=2)),
        ]})
    _, listing_items, _ = ac.load_pass2_backlog(client, now=NOW)
    assert [i["crawl_log_id"] for i in listing_items] == ["log-newer", "log-older"]


def test_end_to_end_chunk_killed_before_pass2_then_backlog_recovers_every_article(monkeypatch):
    """本番で起きていた状況の再現: Pass1で記事の取得に失敗 → チャンクが時間切れで止まりPass2に到達しない
    → 翌ステップ(バックログ)が、notesに残った再試行待ちだけから全記事を回収し、最終的に過不足なく
    保存されること。公開日(datetime)もnotesを経由して正しく復元されること。"""
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    cands = [{"url": f"https://news.example/{i}", "title": f"記事{i}", "published_at": recent} for i in range(3)]
    tgt = _target("t-1")
    client = FakeSupabaseClient({"crawl_targets": [tgt], "crawl_logs": []})
    monkeypatch.setattr(ac, "_list_candidates_for_target", lambda *a, **k: (cands, 200, False, None, False, False, False))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _fail())

    # ── Pass1。ここでプロセスが止まった想定で、Pass2は呼ばない ──
    summary = ac.process_target(tgt, client, {}, True)
    row = client.tables["crawl_logs"][0]
    assert summary["run_result"] == "更新なし" and row["extraction_failures"] == 0   # 従来はここで取りこぼしが隠れていた
    assert len(_funnel(row)["p1_pending"]) == 3                                        # が、再試行待ちはDBに残っている

    # ── 次ステップ: バックログ。今度は取得できる ──
    saved = []
    monkeypatch.setattr(ac, "save_article", lambda client, target, canonical, url, *a, **k: saved.append(url) or (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _ok())
    result = ac.run_pass2_backlog(budget_sec=600, client=client, proxies={}, verify=True)

    assert sorted(saved) == [c["url"] for c in cands]                                  # 3件すべて保存された
    row = client.tables["crawl_logs"][0]
    f = _funnel(row)
    assert row["items_detected"] == 3 and row["new_items"] == 3 and row["run_result"] == "成功"
    assert f["pass2"] == "done" and "p1_pending" not in f
    assert result["article_items"] == 3 and result["expired"] == 0

    # ── 2回目のバックログは、もう何も残っていない（二重処理しない） ──
    saved.clear()
    again = ac.run_pass2_backlog(budget_sec=600, client=client, proxies={}, verify=True)
    assert again["article_items"] == 0 and saved == []


# ─── 段階の順番・予算・Agent上限（2026-10-19） ───────────────────────────────
def test_tier_order_defaults_to_tinyfish_fetch_then_zyte_then_agent_while_unmeasured():
    assert ac.choose_tier_order({"tinyfish_fetch": {"p": 0.76, "t": None}, "zyte": {"p": None, "t": None}}) ==         ("tinyfish_fetch", "zyte", "tinyfish_agent")
    assert ac.ESCALATION_TIERS == ("tinyfish_fetch", "zyte", "tinyfish_agent")


@pytest.mark.parametrize("tf,zy,expected_first", [
    # E(TF→Z)=10+0.24*60=24.4 / E(Z→TF)=60+0.1*10=61 → TinyFish Fetchが先
    ({"p": 0.76, "t": 10}, {"p": 0.9, "t": 60}, "tinyfish_fetch"),
    # E(TF→Z)=40+0.24*8=41.9 / E(Z→TF)=8+0.1*40=12 → Zyteが先（Zyteの方が速く確実）
    ({"p": 0.76, "t": 40}, {"p": 0.9, "t": 8}, "zyte"),
    # 同点はTinyFish Fetchを先にする（従来の順を維持）
    ({"p": 0.5, "t": 10}, {"p": 0.5, "t": 10}, "tinyfish_fetch"),
])
def test_tier_order_picks_smaller_expected_time_and_agent_is_always_last(tf, zy, expected_first):
    order = ac.choose_tier_order({"tinyfish_fetch": tf, "zyte": zy})
    assert order[0] == expected_first and order[-1] == "tinyfish_agent" and sorted(order) == sorted(ac._KNOWN_TIERS)


def test_tier_order_env_override_accepts_valid_and_ignores_invalid(capsys):
    assert ac._tier_order_from_env({"CRAWL_TIER_ORDER": "zyte, tinyfish_fetch, tinyfish_agent"}) ==         ("zyte", "tinyfish_fetch", "tinyfish_agent")
    assert ac._tier_order_from_env({"CRAWL_TIER_ORDER": "zyte,zyte"}) == ac.choose_tier_order()  # 不正は無視
    assert "無効" in capsys.readouterr().out
    assert ac._tier_order_from_env({}) == ac.choose_tier_order()


def test_escalate_pending_follows_the_configured_tier_order(monkeypatch):
    monkeypatch.setattr(ac, "ESCALATION_TIERS", ("zyte", "tinyfish_fetch", "tinyfish_agent"))
    items = [_item("https://x/1")]
    client = _client_with_log(items)
    calls = []
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **k: calls.append(tier) or _fail())

    ac.escalate_pending(items, client, {}, True)

    assert calls == ["zyte", "tinyfish_fetch", "tinyfish_agent"]   # 設定した順に、最後がAgent


def test_compute_backlog_budget_scales_with_workload_and_is_clamped():
    assert ac.compute_backlog_budget(0, 0) == ac.BACKLOG_BUDGET_MIN_SEC            # 下限
    assert ac.compute_backlog_budget(10_000, 1_000) == ac.BACKLOG_BUDGET_MAX_SEC    # 上限
    mid = ac.compute_backlog_budget(100, 20)                                        # 100*30+20*90=4800 → ×1.2
    assert mid == int(4800 * 1.2) and ac.BACKLOG_BUDGET_MIN_SEC < mid < ac.BACKLOG_BUDGET_MAX_SEC
    assert ac.compute_backlog_budget(200, 20) > ac.compute_backlog_budget(100, 20)  # 件数が増えれば予算も増える


def test_run_backlog_auto_budget_and_agent_cap(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 10)   # チャンク用の既定値（テスト後に戻る）
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: _fail())
    monkeypatch.setattr(ac, "datetime", type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))
    client = _backlog_client()

    result = ac.run_pass2_backlog(client=client, proxies={}, verify=True)       # 予算・上限とも指定なし

    assert result["budget_sec"] == ac.compute_backlog_budget(result["article_items"], result["listing_items"])
    assert ac.TINYFISH_AGENT_RUN_CAP == ac.BACKLOG_AGENT_CAP == 30               # バックログ用の上限に切り替わる
    # 手動指定は自動算出より優先される
    again = ac.run_pass2_backlog(budget_sec=123, agent_cap=5, client=client, proxies={}, verify=True)
    assert again["budget_sec"] == 123 and ac.TINYFISH_AGENT_RUN_CAP == 5


def test_agent_cap_limits_how_many_articles_reach_the_agent_tier(monkeypatch):
    """Agentは最後の砦。上限を超えた分はAgentを呼ばず、最終失敗として確定する（費用の上限を守る）"""
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 2)
    items = [_item(f"https://x/{i}") for i in range(5)]
    client = _client_with_log(items)
    agent_calls = []

    def fake_extract(url, proxies, verify, tier=None, **k):
        if tier == "tinyfish_agent":
            agent_calls.append(url)
        return _fail()

    monkeypatch.setattr(ac, "extract_article", fake_extract)
    result = ac.escalate_pending(items, client, {}, True)

    assert len(agent_calls) == 2 and result["final_failed"] == 5
