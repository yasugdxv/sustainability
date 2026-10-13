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
    assert updated_log["notes"] == "Bot対策検知1件"


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
