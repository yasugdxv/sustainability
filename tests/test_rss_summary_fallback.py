"""Bot対策などで本文を取得できないRSSの記事を、フィードの題名＋要約で代用保存する機能のテスト（2026-10-23追加）

方針: 全段階で本文を取得できず、最終失敗した記事に限り、題名＋要約(80字以上)を保存する。補った内容は足さない。
保存した記事は extraction_error に RSS_SUMMARY_ONLY を記録し、分析では「要約のみ」と明示して、必ず要確認(needs_review)にする。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_analyzer as an  # noqa: E402
import article_crawler as ac  # noqa: E402
import crawl_funnel_report as rep  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

SUMMARY = "The Council adopted its position ahead of the climate and biodiversity summits, stressing multilateral cooperation and finance. " * 1


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", False)
    monkeypatch.setattr(ac, "ESCALATION_TIERS", ("tinyfish_fetch", "tinyfish_agent"))
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "_tinyfish_agent_site_calls", {})
    monkeypatch.setattr(ac.time, "sleep", lambda *_: None)


# ─── フィードの要約の取り出し ───
def test_rss_entry_summary_strips_html_and_prefers_the_longer_content():
    e = {"summary": "<p>Short&nbsp;intro <b>bold</b></p>", "content": [{"value": "<div>" + "Long body text. " * 10 + "</div>"}]}
    out = ac._rss_entry_summary(e)
    assert "<" not in out and out.startswith("Long body text.") and "  " not in out
    assert ac._rss_entry_summary({"summary": "<p>A &amp; B</p>"}) == "A & B"
    assert ac._rss_entry_summary({}) == ""
    assert len(ac._rss_entry_summary({"summary": "x" * 5000})) == ac.RSS_SUMMARY_MAX_CHARS


def test_list_rss_candidates_carries_the_summary(monkeypatch):
    xml = ("<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
           f"<item><title>Title A</title><link>https://example.com/a</link><description><![CDATA[<p>{SUMMARY}</p>]]></description></item>"
           "<item><title>Title B</title><link>https://example.com/b</link></item></channel></rss>").encode("utf-8")
    resp = MagicMock(content=xml, url="https://example.com/feed", status_code=200)
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: resp)

    cands, _ = ac.list_rss_candidates({"target_url": "https://example.com/feed"}, {}, True)

    assert cands[0]["summary"].startswith("The Council adopted") and cands[1]["summary"] == ""


def test_pending_entry_keeps_the_summary_for_the_backlog_step():
    cand = {"url": "https://x.com/a", "title": "T", "published_at": None, "summary": "s" * 500}
    entry = ac._pending_entry({"cand": cand})
    assert len(entry["s"]) == 300
    assert ac._entry_to_cand(entry)["summary"] == "s" * 300
    assert "s" not in ac._pending_entry({"cand": {"url": "https://x.com/a", "title": "T", "published_at": None}})
    assert ac._entry_to_cand({"u": "https://x.com/a"})["summary"] == ""


# ─── 最終失敗時の代用保存 ───
def _item(summary=SUMMARY, title="Council sets position", published=None, target_name="Council RSS", publisher="Council", log_id="log-1"):
    return {"crawl_log_id": log_id, "lookback": 7,
            "target": {"crawl_target_id": "t1", "target_name": target_name, "publisher_name": publisher, "domain": "example.com", "target_url": "https://example.com/feed"},
            "cand": {"url": "https://example.com/news/a1", "title": title, "published_at": published or datetime.now(timezone.utc) - timedelta(days=1), "summary": summary}}


def _client():
    return FakeSupabaseClient({"crawl_logs": [{"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
                                               "extraction_failures": 0, "error_message": None, "notes": None}]})


def _run(monkeypatch, items, error="RuntimeError: TinyFish Agent: 目標未達（Page blocked by Cloudflare CAPTCHA）"):
    saved = []
    monkeypatch.setattr(ac, "save_article", lambda client, target, url, fu, fi, title, pub, upd, text, **kw: saved.append(
        {"url": url, "title": title, "text": text, **kw}) or (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **kw: {"ok": False, "error": error, "http_status": None})
    client = _client()
    ac.escalate_pending(items, client, {}, True)
    _, funnel = ac._split_notes(client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]["notes"])
    return saved, funnel


def test_blocked_article_is_saved_as_title_plus_summary_and_marked(monkeypatch):
    saved, funnel = _run(monkeypatch, [_item()])

    assert len(saved) == 1
    s = saved[0]
    assert s["text"] == f"Council sets position\n\n{SUMMARY.strip()}"              # 補った内容は、足さない
    assert s["extraction_error"].startswith(ac.RSS_SUMMARY_ONLY_MARK)
    assert "CAPTCHA" in s["extraction_error"]                                        # 失敗の理由も残る
    assert funnel["summary_saved"] == 1 and funnel["final_failed"] == 1               # 全文は取れていないので、最終失敗にも数える


@pytest.mark.parametrize("label,kwargs,error", [
    ("要約が短い", {"summary": "short"}, "Cloudflare CAPTCHA"),
    ("要約なし", {"summary": ""}, "Cloudflare CAPTCHA"),
    ("期間外", {"published": datetime.now(timezone.utc) - timedelta(days=60)}, "Cloudflare CAPTCHA"),
    ("題名がサイト名のみ", {"title": "Council RSS"}, "Cloudflare CAPTCHA"),
    ("記事ではない(404)", {}, "RuntimeError: 目標未達（Page returned 404 - the URL does not exist）"),
])
def test_not_saved_when(monkeypatch, label, kwargs, error):
    saved, funnel = _run(monkeypatch, [_item(**kwargs)], error=error)
    assert saved == [] and "summary_saved" not in funnel, label


def test_summary_starting_with_the_title_is_not_duplicated(monkeypatch):
    saved, _ = _run(monkeypatch, [_item(title="Council sets position", summary="Council sets position ahead of summits, " + SUMMARY)])
    assert saved[0]["text"].count("Council sets position") == 1


def test_a_failure_while_saving_the_summary_does_not_stop_pass2(monkeypatch):
    monkeypatch.setattr(ac, "save_article", MagicMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **kw: {"ok": False, "error": "Cloudflare CAPTCHA", "http_status": None})
    client = _client()

    result = ac.escalate_pending([_item()], client, {}, True)

    assert result == {"recovered": 0, "final_failed": 1}


def test_full_text_success_does_not_use_the_summary(monkeypatch):
    saved = []
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: saved.append(k) or (True, True))
    monkeypatch.setattr(ac, "extract_article", lambda url, proxies, verify, tier=None, **kw: {"ok": True, "text": "本文" * 200, "title": "t", "http_status": 200})
    ac.escalate_pending([_item()], _client(), {}, True)
    assert all("extraction_error" not in k for k in saved)                            # 全文が取れれば、通常の保存(要約のみの印は付かない)


# ─── save_article に、extraction_error が渡る ───
def test_save_article_stores_the_marker(monkeypatch):
    class C:
        def __init__(self):
            self.inserted = {}

        def select(self, table, params=None):
            return []

        def insert(self, table, rows, **kw):
            self.inserted[table] = rows
            return [{"article_url_id": "u1", "article_id": "a1"}]

    c = C()
    target = {"crawl_target_id": "t1", "target_url": "https://example.com/feed", "publisher_name": "P", "publisher_tag_id": "SJ-1"}
    ac.save_article(c, target, "https://example.com/news/a1", "https://example.com/news/a1", "https://example.com/news/a1", "T", None, None,
                    "T\n\n" + SUMMARY, extraction_error="RSS_SUMMARY_ONLY: test")
    assert c.inserted["articles"][0]["extraction_error"] == "RSS_SUMMARY_ONLY: test"
    c2 = C()
    ac.save_article(c2, target, "https://example.com/news/a2", "https://example.com/news/a2", "https://example.com/news/a2", "T", None, None, "本文" * 100)
    assert "extraction_error" not in c2.inserted["articles"][0]


# ─── 分析 ───
def test_analyzer_marks_summary_only_articles_and_tells_the_model():
    art = {"title": "T", "extracted_text": "T\n\n" + SUMMARY, "published_at": None, "final_url": "u", "extraction_error": "RSS_SUMMARY_ONLY: x"}
    assert an.is_summary_only(art) and not an.is_summary_only({"extraction_error": None}) and not an.is_summary_only({})
    prompt = an.build_user_prompt(art, {"publisher_name": "P", "domain": "d"})
    assert "本文を取得できず" in prompt and "補わず" in prompt
    assert "本文を取得できず" not in an.build_user_prompt({**art, "extraction_error": None}, {"publisher_name": "P", "domain": "d"})


def test_analyzer_forces_needs_review_for_summary_only_articles():
    payload = json.dumps({"tags": [], "needs_review": False, "needs_web_verification": False, "primary_source_status": "出所不明"})
    azure = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: SimpleNamespace(output_text=payload)))
    base = {"title": "T", "extracted_text": SUMMARY, "published_at": None, "final_url": "u"}
    r1 = an.analyze_article(azure, "m", "rubric", "tags", {**base, "extraction_error": "RSS_SUMMARY_ONLY: x"}, {"publisher_name": "P", "domain": "d"})
    r2 = an.analyze_article(azure, "m", "rubric", "tags", base, {"publisher_name": "P", "domain": "d"})
    assert r1["needs_review"] is True and r2["needs_review"] is False


# ─── 毎日の集計 ───
def test_funnel_report_shows_summary_saved():
    logs = [{"crawl_target_id": "t1", "run_result": "一部成功", "started_at": "2026-10-23T00:00:00+00:00",
             "notes": ac._compose_notes("", {"final_failed": 12, "summary_saved": 9})},
            {"crawl_target_id": "t2", "run_result": "成功", "started_at": "2026-10-23T00:01:00+00:00",
             "notes": ac._compose_notes("", {"summary_saved": 3})}]
    s = rep.summarize(logs)
    assert s["summary_saved"] == 12 and "[RSS要約で代用保存] 12件" in rep.render(s, "x")
