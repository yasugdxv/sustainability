"""article_crawler.py の2パス方式フォールバック
(Pass1: 全target通常クロール → Pass2: 失敗分をまとめてTinyFish Fetch→Zyte→Agentの
順に段階的再試行)のテスト。

2026-10-13追加: 以前は1記事ごとにZyte→TinyFish Fetch→Agentのチェーンを
最後まで通していたが、1targetに失敗記事が集中すると他targetの処理を
圧迫する問題が本番で見つかった(2026-10-09〜12実測: 1target最大87分、
1日の処理件数が507件→233件まで減少)。全target分の失敗をまとめてから
段階的に処理する方式に変更した。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402


@pytest.fixture(autouse=True)
def _zyte_enabled(monkeypatch):
    """このファイルは、Zyte有効時の挙動のテスト（Zyteは2026-10-19から既定で停止。停止中の挙動は
    tests/test_zyte_disabled.pyで確認する）"""
    monkeypatch.setattr(ac, "ZYTE_ENABLED", True)
    monkeypatch.setattr(ac, "ESCALATION_TIERS", ("tinyfish_fetch", "zyte", "tinyfish_agent"))
    monkeypatch.setattr(ac, "_ZYTE_METHOD_TIERS", ac._build_zyte_method_tiers(True))
from tests._fakes import FakeSupabaseClient  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_tinyfish_state(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy-tinyfish-key")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)


# ─── _compute_run_result() ────────────────────────────────────────────
@pytest.mark.parametrize("error_message,extraction_failures,new_items,updated_items,expected", [
    ("boom", 0, 0, 0, "失敗"),
    (None, 1, 1, 0, "一部成功"),
    (None, 0, 1, 0, "成功"),
    (None, 0, 0, 1, "成功"),
    (None, 1, 0, 0, "抽出失敗"),
    (None, 0, 0, 0, "更新なし"),
])
def test_compute_run_result(error_message, extraction_failures, new_items, updated_items, expected):
    assert ac._compute_run_result(error_message, extraction_failures, new_items, updated_items) == expected


# ─── update_crawl_log_after_escalation() ──────────────────────────────
def _make_client_with_log(**overrides):
    row = {
        "crawl_log_id": "log-1", "items_detected": 2, "new_items": 1, "updated_items": 0,
        "extraction_failures": 3, "error_message": None, "notes": None,
    }
    row.update(overrides)
    return FakeSupabaseClient({"crawl_logs": [row]})


def test_update_crawl_log_after_escalation_adds_recovered_counts():
    client = _make_client_with_log()
    ac.update_crawl_log_after_escalation(
        client, "log-1", recovered_items_detected=2, recovered_new_items=2, recovered_updated_items=0)
    updated = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated["items_detected"] == 4
    assert updated["new_items"] == 3
    assert updated["extraction_failures"] == 3  # 変更なし
    assert updated["run_result"] == "一部成功"  # 失敗も成功も両方ある


def test_update_crawl_log_after_escalation_adds_final_failures_and_sets_bot_block_note():
    client = _make_client_with_log(items_detected=0, new_items=0, updated_items=0, extraction_failures=0)
    ac.update_crawl_log_after_escalation(client, "log-1", final_failures=2, bot_block_detections=2)
    updated = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated["extraction_failures"] == 2
    assert updated["run_result"] == "抽出失敗"
    assert updated["notes"] == "Bot対策検知2件"


def test_update_crawl_log_after_escalation_missing_row_is_noop():
    client = FakeSupabaseClient({"crawl_logs": []})
    ac.update_crawl_log_after_escalation(client, "missing-id", final_failures=1)  # 例外にならないこと


# ─── _apply_recovered_candidate() ──────────────────────────────────────
def test_apply_recovered_candidate_saves_within_lookback(monkeypatch):
    save_mock = MagicMock(return_value=(True, True))
    monkeypatch.setattr(ac, "save_article", save_mock)
    target = {"publisher_name": "Example", "target_name": "Example Target"}
    cand = {"url": "https://example.com/a", "title": None, "published_at": None}
    extracted = {
        "ok": True, "title": "記事タイトル", "text": "本文",
        "published_at": datetime.now(timezone.utc), "final_url": "https://example.com/a",
        "canonical_url": "https://example.com/a", "updated_at": None, "is_document": False,
    }

    result = ac._apply_recovered_candidate(client=None, target=target, cand=cand, extracted=extracted,
                                            lookback=7)

    assert result == (True, True)
    save_mock.assert_called_once()


def test_apply_recovered_candidate_rejects_outside_lookback(monkeypatch):
    save_mock = MagicMock(side_effect=AssertionError("lookback対象外ならsave_articleは呼ばれないはず"))
    monkeypatch.setattr(ac, "save_article", save_mock)
    target = {"publisher_name": "Example", "target_name": "Example Target"}
    cand = {"url": "https://example.com/a", "title": None, "published_at": None}
    old_date = datetime.now(timezone.utc) - timedelta(days=365)
    extracted = {"ok": True, "title": "t", "text": "本文", "published_at": old_date,
                 "final_url": "https://example.com/a", "canonical_url": "https://example.com/a",
                 "updated_at": None, "is_document": False}

    result = ac._apply_recovered_candidate(client=None, target=target, cand=cand, extracted=extracted,
                                            lookback=7)

    assert result is None
    save_mock.assert_not_called()


def test_apply_recovered_candidate_rejects_site_name_title(monkeypatch):
    """一覧ページ等の誤抽出(タイトルがサイト名そのもの)は保存しないこと"""
    save_mock = MagicMock(side_effect=AssertionError("サイト名と一致するタイトルは保存されないはず"))
    monkeypatch.setattr(ac, "save_article", save_mock)
    target = {"publisher_name": "Example Corp", "target_name": "Example Target"}
    cand = {"url": "https://example.com/a", "title": None, "published_at": None}
    extracted = {"ok": True, "title": "Example Corp", "text": "本文",
                 "published_at": datetime.now(timezone.utc), "final_url": "https://example.com/a",
                 "canonical_url": "https://example.com/a", "updated_at": None, "is_document": False}

    result = ac._apply_recovered_candidate(client=None, target=target, cand=cand, extracted=extracted,
                                            lookback=7)

    assert result is None
    save_mock.assert_not_called()


# ─── extract_article(tier=...): 単一手段のみを試す ─────────────────────
def test_extract_article_tier_zyte_calls_only_zyte(monkeypatch):
    monkeypatch.setattr(ac, "_fetch_with_zyte",
                         lambda *a, **k: ("<html><body><article>本文テキストです。これは十分な長さの文章です。</article></body></html>",
                                          "https://example.com/a", 200, None))
    tf_fetch_mock = MagicMock(side_effect=AssertionError("tier=zyteならTinyFishは呼ばれないはず"))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch", tf_fetch_mock)

    result = ac.extract_article("https://example.com/a", {}, True, tier="zyte")

    assert result["ok"] is True
    tf_fetch_mock.assert_not_called()


def test_extract_article_tier_tinyfish_fetch_calls_only_tinyfish(monkeypatch):
    zyte_mock = MagicMock(side_effect=AssertionError("tier=tinyfish_fetchならZyteは呼ばれないはず"))
    monkeypatch.setattr(ac, "_fetch_with_zyte", zyte_mock)
    monkeypatch.setattr(ac, "_call_tinyfish_fetch",
                         lambda *a, **k: ("<html><body><article>本文テキストです。これは十分な長さの文章です。</article></body></html>",
                                          "https://example.com/a", 200))

    result = ac.extract_article("https://example.com/a", {}, True, tier="tinyfish_fetch")

    assert result["ok"] is True
    zyte_mock.assert_not_called()


def test_extract_article_tier_none_does_not_chain(monkeypatch):
    """tier未指定(通常クロール)が失敗しても、以前のように自動でZyte等へ
    チェーンしないこと(2026-10-13でエスカレーションをmain()側へ移したため)"""
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(ac.requests.exceptions.ConnectionError("boom")))
    zyte_mock = MagicMock(side_effect=AssertionError("tier未指定時にZyteへ自動エスカレーションしないはず"))
    monkeypatch.setattr(ac, "_fetch_with_zyte", zyte_mock)

    result = ac.extract_article("https://example.com/a", {}, True)

    assert result["ok"] is False
    zyte_mock.assert_not_called()


# ─── process_target(): 非404失敗はpending_escalationsへ、404は即時失敗 ──
def _single_page_target(**overrides):
    target = {
        "crawl_target_id": "t1", "crawl_method": "HTML", "endpoint_type": "単一ページ",
        "target_url": "https://example.com/news", "target_name": "Example", "publisher_name": "Example",
        "lookback_days": 7,
    }
    target.update(overrides)
    return target


def test_process_target_defers_non_404_failure_to_pending_escalations(monkeypatch):
    client = FakeSupabaseClient({})
    monkeypatch.setattr(ac, "extract_article",
                         lambda *a, **k: {"ok": False, "error": "Bot対策検知", "http_status": 403})

    summary = ac.process_target(_single_page_target(), client, {}, True)

    assert summary["extraction_failures"] == 0
    assert len(summary["pending_escalations"]) == 1
    assert summary["pending_escalations"][0]["is_bot_block"] is True
    assert summary["pending_escalations"][0]["crawl_log_id"] == summary["crawl_log_id"]


def test_process_target_counts_404_immediately_without_deferring(monkeypatch):
    client = FakeSupabaseClient({})
    monkeypatch.setattr(ac, "extract_article",
                         lambda *a, **k: {"ok": False, "error": "Not Found", "http_status": 404})

    summary = ac.process_target(_single_page_target(), client, {}, True)

    assert summary["extraction_failures"] == 1
    assert summary["pending_escalations"] == []
    assert summary["run_result"] == "抽出失敗"


# ─── escalate_pending(): 段階順序・救済・最終失敗確定 ──────────────────
def _pending_item(crawl_log_id="log-1", url="https://example.com/a", is_bot_block=True):
    return {
        "target": {"publisher_name": "Example", "target_name": "Example"},
        "cand": {"url": url, "title": None, "published_at": None},
        "crawl_log_id": crawl_log_id,
        "lookback": 36500,  # テストの簡略化のためlookback対象外にならない値にする
        "is_bot_block": is_bot_block,
    }


def _ok_extracted():
    return {"ok": True, "title": "記事タイトル", "text": "本文テキスト", "published_at": None,
            "final_url": "https://example.com/a", "canonical_url": "https://example.com/a",
            "updated_at": None, "is_document": False}


def test_escalate_pending_recovers_at_tinyfish_fetch_stage(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
         "extraction_failures": 0, "error_message": None, "notes": None},
    ]})
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: (True, True))
    calls = []

    def fake_extract_article(url, proxies, verify, tier=None, **kw):
        calls.append(tier)
        if tier == "tinyfish_fetch":
            return _ok_extracted()
        raise AssertionError(f"tinyfish_fetchで救済済みのはずがtier={tier}が呼ばれた")

    monkeypatch.setattr(ac, "extract_article", fake_extract_article)

    result = ac.escalate_pending([_pending_item()], client, {}, True)

    assert result == {"recovered": 1, "final_failed": 0}
    assert calls == ["tinyfish_fetch"]
    updated_log = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated_log["new_items"] == 1
    assert updated_log["run_result"] == "成功"


def test_escalate_pending_falls_through_all_stages_then_fails(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
         "extraction_failures": 0, "error_message": None, "notes": None},
    ]})
    calls = []

    def fake_extract_article(url, proxies, verify, tier=None, **kw):
        calls.append(tier)
        return {"ok": False, "error": "still blocked", "http_status": 403}

    monkeypatch.setattr(ac, "extract_article", fake_extract_article)

    result = ac.escalate_pending([_pending_item(is_bot_block=True)], client, {}, True)

    assert calls == ["tinyfish_fetch", "zyte", "tinyfish_agent"]  # 順序通り全段階を経ること
    assert result == {"recovered": 0, "final_failed": 1}
    updated_log = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated_log["extraction_failures"] == 1
    assert updated_log["run_result"] == "抽出失敗"
    # Bot対策検知の表記は先頭に残し(自動降格の判定が前方一致で見るため)、ファネルを追記する
    assert updated_log["notes"].startswith("Bot対策検知1件")
    prefix, funnel = ac._split_notes(updated_log["notes"])
    assert prefix == "Bot対策検知1件"
    assert funnel["final_failed"] == 1
    assert {k: v[:2] for k, v in funnel["tiers"].items()} == {
        "tinyfish_fetch": [1, 0], "zyte": [1, 0], "tinyfish_agent": [1, 0]}  # 3要素目は所要秒数


def test_escalate_pending_respects_agent_budget_cap(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 1)
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
         "extraction_failures": 0, "error_message": None, "notes": None},
    ]})
    agent_calls = []

    def fake_extract_article(url, proxies, verify, tier=None, **kw):
        if tier == "tinyfish_agent":
            agent_calls.append(url)
        return {"ok": False, "error": "still blocked", "http_status": 403}

    monkeypatch.setattr(ac, "extract_article", fake_extract_article)

    # 2件ともAgent段まで到達するが、上限1件のため1件しかAgentが実際には呼ばれないこと
    pending = [_pending_item(url="https://example.com/a"), _pending_item(url="https://example.com/b")]
    result = ac.escalate_pending(pending, client, {}, True)

    assert len(agent_calls) <= 1
    assert result["final_failed"] == 2


# ─── _fetch_with_escalation(allow_escalation=False): 一覧取得用の「1回だけ試す」モード ──
# 2026-10-14追加: 一覧取得(list_html_candidates/list_rss_candidates)がPass1では常にフル
# エスカレーション(Zyte→TinyFish Fetch)していたことが、HTML方式+32%/RSS方式+13%の
# 1targetあたり処理時間増加の原因だったと判明したため、「本来の手段を1回だけ試して
# 失敗ならその場で諦める」モードを追加した
def test_fetch_with_escalation_allow_escalation_false_raises_without_zyte_fallback(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(ac.requests.exceptions.ConnectionError("boom")))
    zyte_mock = MagicMock(side_effect=AssertionError("allow_escalation=FalseならZyteは呼ばれないはず"))
    monkeypatch.setattr(ac, "_fetch_with_zyte", zyte_mock)

    with pytest.raises(ac.requests.exceptions.ConnectionError):
        ac._fetch_with_escalation("https://example.com/list", {}, True, allow_escalation=False)
    zyte_mock.assert_not_called()


def test_fetch_with_escalation_allow_escalation_false_start_with_zyte_tries_only_zyte(monkeypatch):
    """crawl_method='Zyte'等(start_with_zyte=True)でallow_escalation=Falseの場合、
    最初からZyteを1回試すだけで、それが失敗してもTinyFishへはエスカレーションしないこと"""
    zyte_mock = MagicMock(side_effect=RuntimeError("zyte failed"))
    monkeypatch.setattr(ac, "_fetch_with_zyte", zyte_mock)
    tf_mock = MagicMock(side_effect=AssertionError("allow_escalation=FalseならTinyFishは呼ばれないはず"))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch", tf_mock)

    with pytest.raises(RuntimeError):
        ac._fetch_with_escalation("https://example.com/list", {}, True,
                                   start_with_zyte=True, allow_escalation=False)
    zyte_mock.assert_called_once()
    tf_mock.assert_not_called()


def test_fetch_with_escalation_allow_escalation_true_still_chains_as_before(monkeypatch):
    """allow_escalation未指定(デフォルトTrue)の場合は既存通りZyte→TinyFishへ連鎖すること(非回帰)"""
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(ac.requests.exceptions.ConnectionError("boom")))
    monkeypatch.setattr(ac, "_fetch_with_zyte", MagicMock(side_effect=RuntimeError("zyte failed")))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch",
                         lambda *a, **k: ("<html>recovered</html>", "https://example.com/list", 200))

    html, final_url, status_code, raw = ac._fetch_with_escalation("https://example.com/list", {}, True)

    assert html == "<html>recovered</html>"


# ─── list_html_candidates()/list_rss_candidates(): allow_escalationの伝播 ──────
def test_list_html_candidates_passes_allow_escalation_false_through(monkeypatch):
    spy = MagicMock(return_value=("<html></html>", "https://example.com/news", 200, None))
    monkeypatch.setattr(ac, "_fetch_with_escalation", spy)
    target = {"target_url": "https://example.com/news", "domain": "example.com",
              "include_paths": "", "exclude_paths": "", "search_conditions": ""}

    ac.list_html_candidates(target, {}, True, allow_escalation=False)

    assert spy.call_args.kwargs["allow_escalation"] is False


def test_list_rss_candidates_allow_escalation_false_does_not_fallback_to_zyte(monkeypatch):
    def fake_requests_get(*a, **k):
        raise ac.requests.exceptions.RequestException("boom")

    monkeypatch.setattr(ac.requests, "get", fake_requests_get)
    zyte_mock = MagicMock(side_effect=AssertionError("allow_escalation=FalseならZyteは呼ばれないはず"))
    monkeypatch.setattr(ac, "_fetch_with_zyte", zyte_mock)
    target = {"target_url": "https://example.com/rss"}

    with pytest.raises(ac.requests.exceptions.RequestException):
        ac.list_rss_candidates(target, {}, True, allow_escalation=False)
    zyte_mock.assert_not_called()


# ─── process_target(): 一覧取得自体の失敗(非404)はpending_listing_escalationへ ──
def _rss_target(**overrides):
    target = {
        "crawl_target_id": "t1", "crawl_method": "RSS",
        "target_url": "https://example.com/rss", "target_name": "Example", "publisher_name": "Example",
        "lookback_days": 36500,
    }
    target.update(overrides)
    return target


def test_process_target_defers_non_404_listing_failure(monkeypatch):
    client = FakeSupabaseClient({})
    monkeypatch.setattr(ac, "list_rss_candidates",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("listing boom")))

    summary = ac.process_target(_rss_target(), client, {}, True)

    assert summary["pending_escalations"] == []
    assert summary["pending_listing_escalation"] is not None
    assert summary["pending_listing_escalation"]["crawl_log_id"] == summary["crawl_log_id"]
    assert summary["run_result"] == "更新なし"
    # Pass1時点では暫定行(件数0)が保存されていること
    saved = client.select("crawl_logs", {"select": "*", "crawl_log_id": f"eq.{summary['crawl_log_id']}"})[0]
    assert saved["items_detected"] == 0 and saved["new_items"] == 0


def test_process_target_listing_404_still_fails_immediately(monkeypatch):
    """一覧取得自体が明確な404の場合は、以前通りPass2へ回さずその場で失敗確定すること(非回帰)"""
    client = FakeSupabaseClient({})
    resp = MagicMock(status_code=404)
    err = ac.requests.exceptions.HTTPError(response=resp)
    monkeypatch.setattr(ac, "list_rss_candidates", lambda *a, **k: (_ for _ in ()).throw(err))

    summary = ac.process_target(_rss_target(), client, {}, True)

    assert summary.get("pending_listing_escalation") is None
    assert summary["run_result"] == "失敗"


# ─── escalate_pending_listings(): 一覧取得をフルエスカレーション許可で再試行 ──
def test_escalate_pending_listings_recovers_and_merges_article_escalations(monkeypatch):
    """一覧取得の再試行が成功した場合、暫定行をUPDATEで上書きし、新たに判明した
    記事単位の失敗(非404)をpending_escalationsとして返す(=通常のescalate_pending()へ
    合流させるため)"""
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "crawl_target_id": "t1", "items_detected": 0, "new_items": 0,
         "updated_items": 0, "extraction_failures": 0, "error_message": None, "notes": None},
    ]})
    candidates = [{"url": "https://example.com/a", "title": "記事A", "published_at": None}]
    monkeypatch.setattr(ac, "list_rss_candidates", lambda *a, **k: (candidates, 200))
    monkeypatch.setattr(ac, "extract_article",
                         lambda *a, **k: {"ok": False, "error": "Bot対策検知", "http_status": 403})

    pending_listings = [{
        "target": _rss_target(crawl_target_id="t1"), "crawl_log_id": "log-1",
        "lookback": 36500, "started_at": datetime.now(timezone.utc),
    }]
    merged = ac.escalate_pending_listings(pending_listings, client, {}, True)

    assert len(merged) == 1
    assert merged[0]["crawl_log_id"] == "log-1"
    assert merged[0]["is_bot_block"] is True
    updated_log = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated_log["error_message"] is None  # 一覧取得自体は成功している


def test_escalate_pending_listings_still_failing_marks_log_as_failed(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "crawl_target_id": "t1", "items_detected": 0, "new_items": 0,
         "updated_items": 0, "extraction_failures": 0, "error_message": None, "notes": None},
    ]})
    monkeypatch.setattr(ac, "list_rss_candidates",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("still down")))

    pending_listings = [{
        "target": _rss_target(crawl_target_id="t1"), "crawl_log_id": "log-1",
        "lookback": 36500, "started_at": datetime.now(timezone.utc),
    }]
    merged = ac.escalate_pending_listings(pending_listings, client, {}, True)

    assert merged == []
    updated_log = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated_log["run_result"] == "失敗"
    assert "still down" in updated_log["error_message"]


def test_escalate_pending_listings_empty_input_is_noop():
    assert ac.escalate_pending_listings([], None, {}, True) == []


# ─── ファネル記録（crawl_logs.notes、2026-10-16追加） ──────────────────────
def test_notes_helpers_roundtrip_and_keep_bot_block_prefix_first():
    notes = ac._compose_notes("Bot対策検知2件", {"p1_article_failed": 3, "tiers": {"zyte": [3, 1]}})
    assert notes.startswith("Bot対策検知")  # downgrade_bot_blocked_targets()の前方一致判定を壊さない
    prefix, funnel = ac._split_notes(notes)
    assert prefix == "Bot対策検知2件"
    assert funnel == {"p1_article_failed": 3, "tiers": {"zyte": [3, 1]}}


def test_notes_helpers_without_bot_block_do_not_look_like_bot_block():
    notes = ac._compose_notes("", {"p1_listing_failed": 1})
    assert not notes.startswith("Bot対策検知")
    assert ac._split_notes(notes) == ("", {"p1_listing_failed": 1})
    assert ac._compose_notes("", {}) is None
    assert ac._split_notes(None) == ("", {})
    assert ac._split_notes("funnel={broken") == ("", {})  # 壊れたJSONでも例外にしない


def test_escalate_pending_records_tiers_in_notes_and_keeps_pass1_count(monkeypatch):
    # Pass1で「記事単位の失敗2件」と記録済みの行。tinyfish_fetchで救済できる
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
         "extraction_failures": 0, "error_message": None,
         "notes": ac._compose_notes("", {"p1_article_failed": 2})},
    ]})
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **kw: _ok_extracted())

    ac.escalate_pending([_pending_item(), _pending_item()], client, {}, True)

    row = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    _, funnel = ac._split_notes(row["notes"])
    assert funnel["p1_article_failed"] == 2          # Pass1の失敗件数が上書きされない
    assert {k: v[:2] for k, v in funnel["tiers"].items()} == {"tinyfish_fetch": [2, 2]}
    assert len(funnel["tiers"]["tinyfish_fetch"]) == 3  # [試行, 成功, 所要秒数]


def test_pass1_listing_failure_provisional_row_is_marked_in_notes(monkeypatch):
    """一覧取得に失敗したtargetの暫定行は「更新なし」で保存されるが、Pass2まで到達しないと
    そのまま残る。本物の更新なしと区別できるよう、notesに印を付ける"""
    client = FakeSupabaseClient({"crawl_logs": []})
    monkeypatch.setattr(ac, "_list_candidates_for_target",
                        lambda *a, **k: ([], 403, False, "HTTPError: 403", False, False, False))
    target = {"crawl_target_id": "t-1", "target_name": "x", "crawl_method": "HTML"}

    result = ac.process_target(target, client, {}, True)

    assert result["pending_listing_escalation"] is not None
    row = client.inserted["crawl_logs"][0]
    assert row["run_result"] == "更新なし"
    assert ac._split_notes(row["notes"])[1] == {"p1_listing_failed": 1}


# ─── crawl_method='Zyte'/'JSON API': Pass1では一切試行せずPass2へ丸ごと回送 ──────
# 2026-10-15追加: これらのtargetは通常のHTTP取得が機能しないと既に分かっているため、
# Pass1で同期的にZyteを呼ぶのをやめ、即座にPass2(TinyFish Fetch→Zyte→Agent)へ回送する。
# 以前は記事数の多いtarget(例: McKinsey)で一覧取得+全記事のZyte呼び出しが合計85分に
# 達し、Pass1全体の進行を圧迫していた
def _zyte_method_target(**overrides):
    target = {
        "crawl_target_id": "t1", "crawl_method": "Zyte", "endpoint_type": "ニュース・一覧ページ",
        "target_url": "https://example.com/news", "target_name": "Example", "publisher_name": "Example",
        "lookback_days": 36500,
    }
    target.update(overrides)
    return target


def test_process_target_defers_zyte_method_without_any_fetch_attempt(monkeypatch):
    """crawl_method='Zyte'はPass1で一覧取得・記事取得とも一切呼ばれず、
    即座にpending_listing_escalationへ回送されること"""
    client = FakeSupabaseClient({})
    listing_mock = MagicMock(side_effect=AssertionError("Pass1で一覧取得が呼ばれないはず"))
    monkeypatch.setattr(ac, "list_html_candidates", listing_mock)
    extract_mock = MagicMock(side_effect=AssertionError("Pass1で記事取得が呼ばれないはず"))
    monkeypatch.setattr(ac, "extract_article", extract_mock)

    summary = ac.process_target(_zyte_method_target(), client, {}, True)

    listing_mock.assert_not_called()
    extract_mock.assert_not_called()
    assert summary["pending_escalations"] == []
    assert summary["pending_listing_escalation"]["tiers"] == ("tinyfish_fetch", "zyte", "tinyfish_agent")
    assert summary["run_result"] == "更新なし"


def test_process_target_defers_json_api_method_with_zyte_first_tiers(monkeypatch):
    """crawl_method='JSON API'も同様にPass1で回送される(Zyte有効時のtier順は zyte→Fetch→Agent)"""
    client = FakeSupabaseClient({})
    monkeypatch.setattr(ac, "list_json_api_candidates",
                         MagicMock(side_effect=AssertionError("Pass1で一覧取得が呼ばれないはず")))

    target = _zyte_method_target(crawl_method="JSON API", endpoint_type="")
    summary = ac.process_target(target, client, {}, True)

    assert summary["pending_listing_escalation"]["tiers"] == ("zyte", "tinyfish_fetch", "tinyfish_agent")


# ─── _resolve_tiered_listing(): tier順に試して最初の成功を返す ──────────────
def test_resolve_tiered_listing_returns_first_successful_tier(monkeypatch):
    def fake_list_with_tier(target, proxies, verify, tier):
        if tier == "tinyfish_fetch":
            return [], 200, False, "RuntimeError: still blocked", False
        return [{"url": "https://example.com/a", "title": None, "published_at": None}], 200, False, None, False

    monkeypatch.setattr(ac, "_list_candidates_with_tier", fake_list_with_tier)

    resolved_tier, candidates, http_status, single_page_mode, error_message, is_404 = \
        ac._resolve_tiered_listing(_zyte_method_target(), {}, True, ("tinyfish_fetch", "zyte", "tinyfish_agent"))

    assert resolved_tier == "zyte"
    assert len(candidates) == 1
    assert error_message is None


def test_resolve_tiered_listing_returns_none_when_all_tiers_fail(monkeypatch):
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                         lambda target, proxies, verify, tier: ([], None, False, f"{tier} failed", False))

    resolved_tier, candidates, http_status, single_page_mode, error_message, is_404 = \
        ac._resolve_tiered_listing(_zyte_method_target(), {}, True, ("tinyfish_fetch", "zyte"))

    assert resolved_tier is None
    assert "zyte failed" in error_message  # 最後に試した段階のエラーが残ること


# ─── escalate_pending_listings(): "tiers"指定時はtier順で一覧取得→記事取得へ使い回す ──
def test_escalate_pending_listings_with_tiers_resolves_and_reuses_tier_for_articles(monkeypatch):
    """一覧取得がzyte段階で成功した場合、各候補の記事取得もtier="zyte"で
    呼ばれること(use_browser/use_zyteではなくtier指定の呼び出しになること)"""
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "crawl_target_id": "t1", "items_detected": 0, "new_items": 0,
         "updated_items": 0, "extraction_failures": 0, "error_message": None, "notes": None},
    ]})
    candidates = [{"url": "https://example.com/a", "title": None, "published_at": None}]

    def fake_list_with_tier(target, proxies, verify, tier):
        if tier == "tinyfish_fetch":
            return [], 200, False, "still blocked", False
        return candidates, 200, False, None, False

    monkeypatch.setattr(ac, "_list_candidates_with_tier", fake_list_with_tier)
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: (True, True))
    extract_calls = []

    def fake_extract_article(url, proxies, verify, use_browser=False, use_zyte=False, tier=None):
        extract_calls.append({"use_browser": use_browser, "use_zyte": use_zyte, "tier": tier})
        return _ok_extracted()

    monkeypatch.setattr(ac, "extract_article", fake_extract_article)

    pending_listings = [{
        "target": _zyte_method_target(crawl_target_id="t1"), "crawl_log_id": "log-1",
        "lookback": 36500, "started_at": datetime.now(timezone.utc),
        "tiers": ("tinyfish_fetch", "zyte", "tinyfish_agent"),
    }]
    merged = ac.escalate_pending_listings(pending_listings, client, {}, True)

    assert merged == []  # 記事取得は成功したので記事単位の再試行要求は無い
    assert extract_calls == [{"use_browser": False, "use_zyte": False, "tier": "zyte"}]
    updated_log = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated_log["new_items"] == 1


def test_escalate_pending_listings_with_tiers_all_fail_marks_log_failed(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "crawl_target_id": "t1", "items_detected": 0, "new_items": 0,
         "updated_items": 0, "extraction_failures": 0, "error_message": None, "notes": None},
    ]})
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                         lambda target, proxies, verify, tier: ([], None, False, f"{tier} failed", False))

    pending_listings = [{
        "target": _zyte_method_target(crawl_target_id="t1"), "crawl_log_id": "log-1",
        "lookback": 36500, "started_at": datetime.now(timezone.utc),
        "tiers": ("tinyfish_fetch", "zyte", "tinyfish_agent"),
    }]
    merged = ac.escalate_pending_listings(pending_listings, client, {}, True)

    assert merged == []
    updated_log = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    assert updated_log["run_result"] == "失敗"


# ─── list_json_api_candidates(tier=...): 未知のtierは非対応 ───────────────
def test_list_json_api_candidates_rejects_unknown_tier():
    with pytest.raises(ValueError):
        ac.list_json_api_candidates(_zyte_method_target(crawl_method="JSON API"), {}, True,
                                     tier="unknown")


def test_pass1_preroute_to_pass2_is_marked_as_routed_not_failed():
    """Zyte/JSON API方式の対象は、Pass1で試さずPass2へ直送する。失敗ではないので、
    notesはp1_listing_failedではなくp1_routed_to_pass2で記録する"""
    client = FakeSupabaseClient({"crawl_logs": []})
    method = next(iter(ac._ZYTE_METHOD_TIERS))
    target = {"crawl_target_id": "t-1", "target_name": "x", "crawl_method": method}

    result = ac.process_target(target, client, {}, True)

    assert result["pending_listing_escalation"]["tiers"] == ac._ZYTE_METHOD_TIERS[method]
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert funnel == {"p1_routed_to_pass2": method}
