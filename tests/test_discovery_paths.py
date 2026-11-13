"""記事を『見つける手段』のテスト（2026-11-13追加）

背景: 本文は取れても、一覧に出ないため、見つけられないサイトがあった。
・cop30.br: JS描画。Plone/VoltoのAPI(@search)に、記事(@id)と公開日(effective)がある
・fao.org / who.int: 一覧がJS描画で、Zyteのブラウザ描画でしか記事リンクが出ない(TinyFish Fetchは記事リンク0件を、成功として返す)
・oecd.org: サイトマップが通常取得だと403。Zyteのブラウザ描画で、XMLの表示画面として取れる
・wbcsd.org: sitemap.rss(RSS形式のサイトマップ)に、更新日つきで、ページが載っている
実API・実HTTPは呼ばない。
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402
import requests  # noqa: E402

import article_crawler as ac  # noqa: E402


# ─── JSON API: Plone/Volto(@id / effective) ───────────────────────────────
def test_json_api_reads_plone_search_items_with_at_id_and_effective(monkeypatch):
    body = {"@id": "https://cop30.br/@search", "items": [
        {"@id": "https://cop30.br/en/news-about-cop30/a-news", "title": "Trade and Climate", "effective": "2026-11-12T22:16:35+00:00"},
        {"@id": "https://cop30.br/en/news-about-cop30/daily-brief/nov12", "title": "Daily Brief", "effective": "2026-11-12T05:00:00+00:00"}]}
    resp = MagicMock(status_code=200, text=json.dumps(body), url="https://cop30.br/@search")
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: resp)
    target = {"target_url": "https://cop30.br/++api++/@search?path=/en/news-about-cop30", "domain": "cop30.br", "crawl_method": "JSON API"}

    cands, status = ac.list_json_api_candidates(target, {}, True)

    assert [c["url"] for c in cands] == ["https://cop30.br/en/news-about-cop30/a-news", "https://cop30.br/en/news-about-cop30/daily-brief/nov12"]
    assert cands[0]["title"] == "Trade and Climate"
    assert cands[0]["published_at"].isoformat().startswith("2026-11-12T22:16:35")


def test_json_api_existing_keys_still_win_over_at_id(monkeypatch):
    body = [{"url": "https://example.com/a", "@id": "https://example.com/ignored", "title": "t", "date": "2026-11-01"}]
    resp = MagicMock(status_code=200, text=json.dumps(body), url="https://example.com/api")
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: resp)

    cands, _ = ac.list_json_api_candidates({"target_url": "https://example.com/api", "domain": "example.com"}, {}, True)

    assert [c["url"] for c in cands] == ["https://example.com/a"]


# ─── 強制ブラウザ描画: 段階指定の取得でも効く／Zyteを先に試す ──────────────
def test_fetch_page_with_tier_passes_force_browser_to_zyte(monkeypatch):
    seen = {}
    monkeypatch.setattr(ac, "_fetch_with_zyte", lambda url, p, v, force_browser=False: seen.update(force=force_browser) or ("<html></html>", url, 200, None))

    ac._fetch_page_with_tier("https://example.com/", {}, True, "zyte", force_zyte_browser=True)

    assert seen["force"] is True


def test_list_html_candidates_with_tier_reads_force_flag_from_search_conditions(monkeypatch):
    seen = {}

    def fake(url, proxies, verify, tier, want_links=False, force_zyte_browser=False):
        seen["force"] = force_zyte_browser
        return '<html><a href="/biodiversity/news/detail/x">x</a></html>', url, 200

    monkeypatch.setattr(ac, "_fetch_page_with_tier", fake)
    target = {"target_url": "https://www.fao.org/biodiversity/news/en", "domain": "fao.org", "include_paths": "/biodiversity/news/detail/",
              "search_conditions": "force_zyte_browser"}

    cands, _ = ac.list_html_candidates(target, {}, True, tier="zyte")

    assert seen["force"] is True and [c["url"] for c in cands] == ["https://www.fao.org/biodiversity/news/detail/x"]


def _tier_order(monkeypatch, target):
    order = []

    def fake(t, proxies, verify, tier):
        order.append(tier)
        return [{"url": "https://example.com/a"}], 200, False, None, False

    monkeypatch.setattr(ac, "_list_candidates_with_tier", fake)
    ac._resolve_tiered_listing(target, {}, True, ("tinyfish_fetch", "zyte", "tinyfish_agent"))
    return order


def test_force_browser_target_tries_zyte_first_when_zyte_enabled(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", True)
    assert _tier_order(monkeypatch, {"search_conditions": "force_zyte_browser", "crawl_method": "Zyte"})[0] == "zyte"


def test_target_without_force_flag_keeps_tinyfish_fetch_first(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", True)
    assert _tier_order(monkeypatch, {"search_conditions": None, "crawl_method": "Zyte"})[0] == "tinyfish_fetch"


def test_force_browser_flag_is_ignored_when_zyte_disabled(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", False)
    order = _tier_order(monkeypatch, {"search_conditions": "force_zyte_browser", "crawl_method": "Zyte"})
    assert order[0] == "tinyfish_fetch"          # Zyte停止中は、これまでどおり


# ─── サイトマップ: RSS形式・ブラウザ描画のXML表示画面 ──────────────────────────
RSS_SITEMAP = ("<rss><channel>"
               "<item><title>a</title><link>https://www.wbcsd.org/resources/a/</link><pubDate><![CDATA[Tue, 27 Oct 2026 12:03:10 +0000]]></pubDate></item>"
               "<item><link>https://www.wbcsd.org/about/</link><pubDate>Tue, 20 Oct 2026 08:36:07 +0000</pubDate></item>"
               "</channel></rss>")
VIEWER = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><style id="xml-viewer-style">/* Copyright 2014 The Chromium Authors */</style></head><body>'
          '<div>This XML file does not appear to have any style information</div>'
          '<div><span>&lt;urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"&gt;</span>'
          '<div><span>&lt;loc&gt;</span>https://www.oecd.org/en/about/news/press-releases/2026/09/old.html<span>&lt;/loc&gt;</span>'
          '<span>&lt;lastmod&gt;</span>2026-09-29T08:57:01.212Z<span>&lt;/lastmod&gt;</span></div>'
          '<div><span>&lt;loc&gt;</span>https://www.oecd.org/en/about/news/press-releases/2026/10/new.html<span>&lt;/loc&gt;</span>'
          '<span>&lt;lastmod&gt;</span>2026-10-01T16:06:36.533Z<span>&lt;/lastmod&gt;</span></div></body></html>')


def test_normalize_rss_sitemap_maps_link_and_pubdate():
    xml = ac._normalize_sitemap_text(RSS_SITEMAP)
    assert "<url><loc>https://www.wbcsd.org/resources/a/</loc><lastmod>Tue, 27 Oct 2026 12:03:10 +0000</lastmod></url>" in xml


def test_normalize_rendered_xml_viewer_extracts_urls_and_lastmod_and_skips_namespace_urls():
    xml = ac._normalize_sitemap_text(VIEWER)
    assert xml.count("<url>") == 2
    assert "sitemaps.org" not in xml
    assert "<loc>https://www.oecd.org/en/about/news/press-releases/2026/10/new.html</loc><lastmod>2026-10-01T16:06:36.533Z</lastmod>" in xml


def test_normalize_leaves_real_sitemaps_and_plain_html_untouched():
    urlset = '<?xml version="1.0"?><urlset><url><loc>https://example.com/a</loc></url></urlset>'
    assert ac._normalize_sitemap_text(urlset) == urlset
    page = "<html><body><a href='https://example.com/x'>https://example.com/x</a> 2026-10-01T00:00:00Z</body></html>"
    assert ac._normalize_sitemap_text(page) == page        # XML表示画面ではない通常のHTMLから、URLを拾わない


def _blocked_get(monkeypatch):
    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.HTTPError("403")))


def test_sitemap_root_falls_back_to_zyte_and_list_returns_newest_first(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", True)
    _blocked_get(monkeypatch)
    monkeypatch.setattr(ac, "_fetch_with_zyte", lambda *a, **k: (VIEWER, "https://www.oecd.org/en-about-sitemap.xml", 200, None))
    target = {"target_url": "https://www.oecd.org/en-about-sitemap.xml", "domain": "oecd.org", "include_paths": "/en/about/news/press-releases/"}

    cands, status = ac.list_sitemap_candidates(target, {}, True)

    assert status == 200
    assert [c["url"].rsplit("/", 1)[1] for c in cands] == ["new.html", "old.html"]      # 新しい順
    assert cands[0]["published_at"].isoformat().startswith("2026-10-01T16:06")


def test_sitemap_root_reads_rss_form_via_zyte_http_raw_bytes(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", True)
    _blocked_get(monkeypatch)
    monkeypatch.setattr(ac, "_fetch_with_zyte", lambda *a, **k: ("", "https://www.wbcsd.org/sitemap.rss", 200, RSS_SITEMAP.encode("utf-8")))
    target = {"target_url": "https://www.wbcsd.org/sitemap.rss", "domain": "wbcsd.org", "include_paths": "/resources/"}

    cands, _ = ac.list_sitemap_candidates(target, {}, True)

    assert [c["url"] for c in cands] == ["https://www.wbcsd.org/resources/a/"]          # include_pathsで、/about/ は除かれる
    assert cands[0]["published_at"].isoformat().startswith("2026-10-27T12:03:10")


def test_sitemap_root_with_zyte_disabled_raises_the_original_error(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", False)
    _blocked_get(monkeypatch)
    monkeypatch.setattr(ac, "_fetch_with_zyte", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Zyte停止中は呼ばれないはず")))

    with pytest.raises(requests.exceptions.HTTPError):
        ac.list_sitemap_candidates({"target_url": "https://example.com/sitemap.xml", "domain": "example.com"}, {}, True)
