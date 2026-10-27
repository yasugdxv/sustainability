"""RSSの取得が通常のHTTPで失敗したとき、TinyFish Fetch経由で読むためのテスト（2026-10-27追加）

背景: Zyte停止中は、RSS（Amnesty・VCMI・Business Wire・Google Blog・Molson Coors・Longevity等）が、
403・タイムアウトで取れないままだった。Fetch(format="html")は、フィードを<link>が消えた形で返すため、
feedparserでは読めない。そのため、題名・URL・日付・要約を、緩く取り出す専用の読み方を使う。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402
import requests  # noqa: E402

import article_crawler as ac  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

BASE = "https://example.com/feed"

# 実際に返った形(Amnesty・Google Blog): <link>が消え、URLが題名の直後に裸で残る。pubDateは小文字
BARE_URL_FEED = (
    '<?xml version="1.0" encoding="UTF-8"?><rss><channel><title>Latest</title>'
    "<item> <title>China’s crackdown</title> https://example.com/a/1 <dc:creator><![CDATA[Amnesty]]></dc:creator>"
    " <pubdate>Thu, 08 Oct 2026 00:01:00 +0000</pubdate> <guid>https://example.com/?p=1</guid>"
    " <description>&lt;p&gt;An interview with &lt;b&gt;Su&lt;/b&gt;&lt;/p&gt;</description></item>"
    "<item><title>Second</title>https://example.com/a/2<media:content></media:content></item></channel></rss>")

ATOM_FEED = ('<feed><entry><title>Atom記事</title><link href="/atom/1" rel="alternate"/>'
             "<updated>2026-10-20T09:30:00Z</updated><summary>要約</summary></entry></feed>")

# Molson Coors・Longevityの形: 全体が<pre>の中でエスケープされて返る
ESCAPED_FEED = ("<html><body><pre>&lt;?xml version=\"1.0\"?&gt;&lt;rss&gt;&lt;channel&gt;"
                "&lt;item&gt;&lt;title&gt;Escaped記事&lt;/title&gt;&lt;link&gt;https://example.com/e/1&lt;/link&gt;"
                "&lt;pubDate&gt;Tue, 1 Sep 2026 22:54:00 UT&lt;/pubDate&gt;&lt;/item&gt;&lt;/channel&gt;&lt;/rss&gt;</pre></body></html>")


# ─── 読み方 ──────────────────────────────────────────────────────
def test_parse_feed_with_link_tag_removed_reads_bare_url_title_date_and_summary():
    cands = ac._parse_fetched_feed(BARE_URL_FEED, BASE)
    assert [c["url"] for c in cands] == ["https://example.com/a/1", "https://example.com/a/2"]
    assert cands[0]["title"] == "China’s crackdown"
    assert cands[0]["published_at"] == datetime(2026, 10, 8, 0, 1, tzinfo=timezone.utc)
    assert cands[0]["summary"] == "An interview with Su"                   # HTMLタグは除く
    assert cands[1]["published_at"] is None                                 # 日付が無くても、候補にする


def test_parse_bare_url_placed_after_description_and_ignores_urls_inside_text_fields_and_attributes():
    # Business Wireの形: URLが、説明文の後ろに残る。説明文の中のURL・enclosureの属性のURLは、記事のURLではない
    text = ("<item> <title>題名 https://wrong.example/title</title> <pubdate>Tue, 1 Sep 2026 22:54:00 UT</pubdate>"
            ' <enclosure url="https://wrong.example/img.jpg"></enclosure>'
            " <description>本文 https://wrong.example/desc</description> https://example.com/news/20260901/nl/?feedref=abc"
            " <guid>20260901nl</guid> </item>")
    assert [c["url"] for c in ac._parse_fetched_feed(text, BASE)] == ["https://example.com/news/20260901/nl/?feedref=abc"]


def test_parse_atom_entry_with_href_link_resolved_against_feed_url():
    cands = ac._parse_fetched_feed(ATOM_FEED, BASE)
    assert cands[0]["url"] == "https://example.com/atom/1"
    assert cands[0]["published_at"] == datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)


def test_parse_escaped_feed_inside_pre():
    cands = ac._parse_fetched_feed(ESCAPED_FEED, BASE)
    assert [c["title"] for c in cands] == ["Escaped記事"]
    assert cands[0]["url"] == "https://example.com/e/1"
    assert cands[0]["published_at"] == datetime(2026, 9, 1, 22, 54, tzinfo=timezone.utc)   # 「UT」の時刻表記も読む


def test_parse_skips_items_without_title_or_url_and_caps_entries():
    text = "<item><title>URLなし</title></item><item>https://example.com/x</item>" + "".join(
        f"<item><title>t{i}</title>https://example.com/p/{i}</item>" for i in range(ac.FEED_MAX_ENTRIES + 5))
    cands = ac._parse_fetched_feed(text, BASE)
    assert len(cands) == ac.FEED_MAX_ENTRIES and cands[0]["title"] == "t0"


# ─── Fetchの呼び出し ───────────────────────────────────────────────
class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.fixture
def _fetch_api(monkeypatch):
    monkeypatch.setattr(ac, "_get_tinyfish_api_key", lambda: "dummy")
    sent = []

    def install(data):
        monkeypatch.setattr(ac.requests, "post", lambda url, **kw: sent.append(kw["json"]) or _Resp(data))
        return sent
    return install


def test_list_via_fetch_requests_html_format_and_returns_candidates(_fetch_api):
    sent = _fetch_api({"results": [{"final_url": BASE, "text": BARE_URL_FEED}]})
    cands, status = ac.list_rss_candidates_via_fetch({"target_url": BASE}, {}, True)
    assert sent[0]["format"] == "html" and len(cands) == 2 and status == 200


def test_list_via_fetch_raises_when_fetch_reports_an_error(_fetch_api):
    _fetch_api({"results": [], "errors": [{"url": BASE, "error": "bot_blocked"}]})
    with pytest.raises(RuntimeError, match="bot_blocked"):
        ac.list_rss_candidates_via_fetch({"target_url": BASE}, {}, True)


def test_list_via_fetch_raises_when_page_is_not_a_feed(_fetch_api):
    _fetch_api({"results": [{"final_url": "https://example.com/", "text": "<html><body>Quick Links Recent</body></html>"}]})
    with pytest.raises(RuntimeError, match="フィードとして読めませんでした"):     # Intelのように、RSSではないページへ転送された場合
        ac.list_rss_candidates_via_fetch({"target_url": BASE}, {}, True)


# ─── 経路: 通常HTTPが失敗したときだけ、Fetchで読む ──────────────────
def _feed_target(method="Zyte"):
    return {"crawl_target_id": "t-1", "target_name": "フィード", "crawl_method": method, "endpoint_type": "RSS",
            "target_url": BASE, "lookback_days": 7}


def _http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"{status} Error", response=resp)


def test_feed_target_tier_fetch_falls_back_to_fetch_when_direct_http_fails(monkeypatch):
    monkeypatch.setattr(ac, "list_rss_candidates", lambda *a, **k: (_ for _ in ()).throw(_http_error(403)))
    monkeypatch.setattr(ac, "list_rss_candidates_via_fetch", lambda *a, **k: ([{"url": "https://example.com/a"}], 200))
    cands, status, single, err, is_404 = ac._list_candidates_with_tier(_feed_target(), {}, True, "tinyfish_fetch")
    assert err is None and len(cands) == 1


def test_feed_target_tier_fetch_falls_back_on_timeout_too(monkeypatch):
    monkeypatch.setattr(ac, "list_rss_candidates", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("45秒")))
    monkeypatch.setattr(ac, "list_rss_candidates_via_fetch", lambda *a, **k: ([{"url": "https://example.com/a"}], 200))
    assert ac._list_candidates_with_tier(_feed_target(), {}, True, "tinyfish_fetch")[3] is None


def test_feed_target_404_does_not_call_fetch(monkeypatch):
    monkeypatch.setattr(ac, "list_rss_candidates", lambda *a, **k: (_ for _ in ()).throw(_http_error(404)))
    monkeypatch.setattr(ac, "list_rss_candidates_via_fetch",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("404でFetchを呼んではいけない")))
    cands, status, single, err, is_404 = ac._list_candidates_with_tier(_feed_target(), {}, True, "tinyfish_fetch")
    assert is_404 and "404" in err


def test_feed_target_direct_success_does_not_call_fetch(monkeypatch):
    monkeypatch.setattr(ac, "list_rss_candidates", lambda *a, **k: ([{"url": "https://example.com/a"}], 200))
    monkeypatch.setattr(ac, "list_rss_candidates_via_fetch",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("通常取得が成功したらFetchは呼ばない")))
    assert ac._list_candidates_with_tier(_feed_target(), {}, True, "tinyfish_fetch")[3] is None


def test_feed_target_agent_tier_is_still_refused():
    err = ac._list_candidates_with_tier(_feed_target(), {}, True, "tinyfish_agent")[3]
    assert "Agentでは取得しません" in err


def test_rss_method_listing_failure_is_recovered_via_fetch_and_articles_use_fetch(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", False)
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    monkeypatch.setattr(ac, "_list_candidates_for_target",
                        lambda *a, **k: ([], 403, False, "HTTPError: 403 Client Error: Forbidden", False, False, False))
    cands = [{"url": f"https://example.com/a/{i}", "title": f"記事{i}", "published_at": None} for i in range(2)]
    monkeypatch.setattr(ac, "list_rss_candidates_via_fetch", lambda *a, **k: (cands, 200))
    tiers, saved = [], []
    monkeypatch.setattr(ac, "extract_article", lambda url, *a, tier=None, **k: tiers.append(tier) or {
        "ok": True, "title": "題", "text": "本文" * 50, "published_at": None, "final_url": None})
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: saved.append(a[3]) or (True, True))

    result = ac.process_target(_feed_target("RSS"), client, {}, True)

    assert result["run_result"] == "成功" and len(saved) == 2 and tiers == ["tinyfish_fetch"] * 2
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert funnel["p1_listing_fallback"] == "tinyfish_fetch"
