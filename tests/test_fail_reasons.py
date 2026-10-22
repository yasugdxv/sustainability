"""Pass2で最終失敗した記事の、失敗理由の記録のテスト（2026-10-22追加）

背景: 2026-10-22に、Fetch 223件・Agent 42件が全滅したが、記事ごとの失敗理由がDBに残っておらず、
原因（サイト側の拒否か、TinyFish側の不調か、残高切れか）を後から切り分けられなかった。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", False)
    monkeypatch.setattr(ac, "ESCALATION_TIERS", ("tinyfish_fetch", "tinyfish_agent"))
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "_tinyfish_agent_site_calls", {})
    monkeypatch.setattr(ac.time, "sleep", lambda *_: None)


# ─── 失敗理由の分類 ───
@pytest.mark.parametrize("tier,reason,expected", [
    ("tinyfish_agent", "HTTPError: 402 Client Error: Payment Required for url: https://agent.tinyfish.ai/v1/x", "credit_exhausted"),
    ("tinyfish_fetch", "HTTPError: 429 Too Many Requests", "rate_limited"),
    ("tinyfish_agent", "RuntimeError: TinyFish Agent: 目標未達（Page is blocked by Cloudflare CAPTCHA）", "bot_blocked"),
    ("tinyfish_agent", "目標未達（Access Denied - the page blocked access）", "bot_blocked"),
    ("tinyfish_agent", "目標未達（Page requires login. No credentials are configured）", "login_required"),
    ("tinyfish_agent", "目標未達（The page is a contact form page with no article）", "not_article"),
    ("tinyfish_agent", "目標未達（Page returned 404 - the URL does not exist）", "not_article"),
    ("tinyfish_agent", "RuntimeError: TinyFish Agent: 本文が短すぎます（149文字）", "empty_or_short"),
    ("tinyfish_agent", "ChunkedEncodingError: Response ended prematurely", "network_error"),
    ("tinyfish_agent", "文書抽出エラー ValueError: Can not convert date", "document_error"),
    ("tinyfish_fetch", "RuntimeError: TinyFish Fetch: 取得失敗({'url': 'https://example.com/a'", "fetch_failed"),
    ("tinyfish_agent", "something unexpected", "other"),
])
def test_classify_failure(tier, reason, expected):
    assert ac._classify_failure(tier, reason) == expected


def test_numbers_inside_the_article_url_are_not_mistaken_for_status_codes():
    """エラー文には記事URLが含まれる。URLの中の「403」「404」を、HTTPステータスと取り違えない"""
    reason = "RuntimeError: TinyFish Fetch: 取得失敗({'url': 'https://example.com/news/pr26403-and-404-report'"
    assert ac._classify_failure("tinyfish_fetch", reason) == "fetch_failed"


# ─── notesへの記録 ───
def _item(url="https://example.com/a1", log_id="log-1", target_id="t1"):
    return {"crawl_log_id": log_id, "target": {"crawl_target_id": target_id, "target_name": "T", "domain": "example.com"},
            "cand": {"url": url, "title": "t", "published_at": datetime(2026, 10, 22, tzinfo=timezone.utc)},
            "lookback": 7}


def _client():
    return FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
         "extraction_failures": 0, "error_message": None, "notes": None}]})


def test_final_failures_record_reason_categories_and_sample_urls(monkeypatch):
    def fake_extract(url, proxies, verify, tier=None, **kw):
        if tier == "tinyfish_fetch":
            return {"ok": False, "error": "RuntimeError: TinyFish Fetch: 取得失敗({'url': '...'", "http_status": None}
        return {"ok": False, "error": "RuntimeError: TinyFish Agent: 目標未達（Page is blocked by Cloudflare CAPTCHA）",
                "http_status": None}

    monkeypatch.setattr(ac, "extract_article", fake_extract)
    client = _client()
    pending = [_item(f"https://example.com/a{i}") for i in range(5)]

    ac.escalate_pending(pending, client, {}, True)

    row = client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]
    _, funnel = ac._split_notes(row["notes"])
    assert funnel["final_failed"] == 5
    assert funnel["fail_reasons"] == {"bot_blocked": 5}              # 最後に失敗した段階(Agent)の理由で分類
    assert len(funnel["fail_samples"]) == ac.FAIL_SAMPLE_MAX         # URLの例は、上限まで
    assert funnel["fail_samples"][0]["u"] == "https://example.com/a0"
    assert funnel["fail_samples"][0]["r"].startswith("tinyfish_agent:")
    assert "agent_skipped" not in funnel


def test_articles_skipped_by_the_agent_cap_are_counted_separately(monkeypatch):
    """Agentの上限で試せなかった記事は、Fetchの失敗理由で分類し、agent_skippedに件数を残す
    （「Agentで失敗した」のか「Agentを試せていない」のかを、後から区別できるように）"""
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 0)
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **kw: {
        "ok": False, "error": "RuntimeError: TinyFish Fetch: 取得失敗({'url': '...'", "http_status": None})
    client = _client()

    ac.escalate_pending([_item("https://example.com/a1"), _item("https://example.com/a2")], client, {}, True)

    _, funnel = ac._split_notes(client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]["notes"])
    assert funnel["fail_reasons"] == {"fetch_failed": 2}
    assert funnel["agent_skipped"] == 2


def test_recovered_articles_leave_no_failure_record(monkeypatch):
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **kw: {
        "ok": False, "error": "x", "http_status": 500})
    monkeypatch.setattr(ac, "_apply_recovered_candidate", lambda *a, **k: (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **kw: {
        "ok": True, "http_status": 200, "text": "本文" * 100, "title": "t"})
    client = _client()

    ac.escalate_pending([_item()], client, {}, True)

    _, funnel = ac._split_notes(client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]["notes"])
    assert "fail_reasons" not in funnel and "final_failed" not in funnel


def test_funnel_report_shows_failure_reasons():
    import crawl_funnel_report as rep
    logs = [{"crawl_target_id": "t1", "run_result": "抽出失敗", "started_at": "2026-10-22T00:00:00+00:00",
             "notes": ac._compose_notes("", {"final_failed": 3, "fail_reasons": {"bot_blocked": 2, "not_article": 1},
                                            "agent_skipped": 1})},
            {"crawl_target_id": "t2", "run_result": "成功", "started_at": "2026-10-22T00:01:00+00:00",
             "notes": ac._compose_notes("", {"final_failed": 1, "fail_reasons": {"bot_blocked": 1}})}]
    summary = rep.summarize(logs)
    assert summary["fail_reasons"] == {"bot_blocked": 3, "not_article": 1} and summary["agent_skipped"] == 1
    text = rep.render(summary, "x")
    assert "bot_blocked 3" in text and "Agentの上限で未実施 1件" in text
