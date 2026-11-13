"""ZyteのHTTP取得で本文を抽出できないとき、Browser Renderingで1回だけ取り直すテスト（2026-11-13追加）

背景: tescoplc.comの記事は、ZyteのHTTP取得では、読める文字が6,000字あっても本文を抽出できず(本文がスクリプトの中)、
Browser Renderingなら本文4,000字が取れる。実API・実の抽出は使わず、取得関数と trafilatura.extract をモックして検証する。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_crawler as ac  # noqa: E402

URL = "https://example.com/some-press-release/"
LONG_TEXT_PAGE = "<html><body><p>" + "読める文字です。" * 300 + "</p><!--HTTP--></body></html>"       # 読める文字 約2,400字
SHORT_TEXT_PAGE = "<html><body><p>" + "少しだけ。" * 20 + "</p><!--HTTP--></body></html>"            # 読める文字 約100字
BROWSER_PAGE = "<html><body><article><h1>題名</h1><p>" + "本文です。" * 100 + "</p></article><!--BROWSER--></body></html>"


def _setup(monkeypatch, http_html, browser_exc=None):
    calls = []

    def fake_zyte(url, proxies, verify, force_browser=False):
        calls.append("browser" if force_browser else "http")
        if force_browser:
            if browser_exc:
                raise browser_exc
            return BROWSER_PAGE, url, 200, None
        return http_html, url, 200, http_html.encode("utf-8")

    def fake_extract(html, **kwargs):
        if "<!--BROWSER-->" in html:
            return json.dumps({"title": "題名", "text": "本文です。" * 100, "date": "2026-06-09"})
        return None                                         # HTTPのHTMLからは、抽出できない

    monkeypatch.setattr(ac, "_fetch_with_zyte", fake_zyte)
    monkeypatch.setattr(ac.trafilatura, "extract", fake_extract)
    return calls


def test_zyte_tier_retries_with_browser_when_text_is_readable_but_extraction_fails(monkeypatch):
    calls = _setup(monkeypatch, LONG_TEXT_PAGE)

    r = ac._extract_article_once(URL, {}, True, tier="zyte")

    assert calls == ["http", "browser"]
    assert r["ok"] is True and r["title"] == "題名" and r["published_at"].year == 2026


def test_use_zyte_without_tier_also_retries(monkeypatch):
    calls = _setup(monkeypatch, LONG_TEXT_PAGE)

    r = ac._extract_article_once(URL, {}, True, use_zyte=True)

    assert calls == ["http", "browser"] and r["ok"] is True


def test_page_with_little_readable_text_does_not_cost_a_browser_call(monkeypatch):
    calls = _setup(monkeypatch, SHORT_TEXT_PAGE)

    r = ac._extract_article_once(URL, {}, True, tier="zyte")

    assert calls == ["http"] and r["ok"] is False


def test_other_tiers_do_not_use_zyte_browser(monkeypatch):
    calls = _setup(monkeypatch, LONG_TEXT_PAGE)
    monkeypatch.setattr(ac, "_call_tinyfish_fetch", lambda *a, **k: (LONG_TEXT_PAGE, URL, 200))

    r = ac._extract_article_once(URL, {}, True, tier="tinyfish_fetch")

    assert calls == [] and r["ok"] is False


def test_browser_failure_is_reported_as_extraction_failure_not_raised(monkeypatch):
    calls = _setup(monkeypatch, LONG_TEXT_PAGE, browser_exc=RuntimeError("browser failed"))

    r = ac._extract_article_once(URL, {}, True, tier="zyte")

    assert calls == ["http", "browser"] and r["ok"] is False and "本文抽出に失敗" in r["error"]
