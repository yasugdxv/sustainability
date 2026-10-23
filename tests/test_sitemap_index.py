"""サイトマップ索引(<sitemapindex>)への対応のテスト（2026-10-23追加）

背景: EPA(子38件)・WBCSD(CDATA付きの索引)・IEAなどは、サイトマップが索引で、子サイトマップに記事のURLが入る。
従来は、索引の<url>が無く、候補が0件だった。
"""
import gzip
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402


def _urlset(entries):
    items = "".join(f"<url><loc>{u}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>" for u, lm in entries)
    return f"<urlset>{items}</urlset>"


def _index(children, cdata=False):
    wrap = (lambda u: f"<![CDATA[{u}]]>") if cdata else (lambda u: u)
    items = "".join(f"<sitemap><loc>{wrap(u)}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</sitemap>" for u, lm in children)
    return f"<sitemapindex>{items}</sitemapindex>"


class FakeWeb:
    def __init__(self, pages):
        self.pages = pages          # url -> text / bytes / Exception
        self.calls = []

    def __call__(self, url, *a, **k):
        self.calls.append(url)
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        r = MagicMock(status_code=200)
        r.text = v if isinstance(v, str) else v.decode("utf-8", errors="replace")
        r.content = v if isinstance(v, bytes) else v.encode("utf-8")
        r.raise_for_status = lambda: None
        return r


def _target(**kw):
    t = {"target_url": "https://example.com/sitemap.xml", "domain": "example.com", "include_paths": "/news/", "exclude_paths": "", "search_conditions": ""}
    t.update(kw)
    return t


def _run(monkeypatch, pages, **kw):
    web = FakeWeb(pages)
    monkeypatch.setattr(ac.requests, "get", web)
    cands, status = ac.list_sitemap_candidates(_target(**kw), {}, True)
    return cands, web


def test_index_children_are_read_and_the_newest_come_first_across_children(monkeypatch):
    pages = {
        "https://example.com/sitemap.xml": _index([("https://example.com/s1.xml", "2026-10-01"), ("https://example.com/s2.xml", "2026-10-20")]),
        "https://example.com/s1.xml": _urlset([("https://example.com/news/old", "2026-01-05"), ("https://example.com/news/mid", "2026-09-30")]),
        "https://example.com/s2.xml": _urlset([("https://example.com/news/new", "2026-10-22"), ("https://example.com/other/x", "2026-10-22")]),
    }
    cands, _ = _run(monkeypatch, pages)
    assert [c["url"].rsplit("/", 1)[1] for c in cands] == ["new", "mid", "old"]       # 子をまたいで、新しい順。include外(other)は除く


def test_cdata_wrapped_index_locs_work(monkeypatch):
    """WBCSD: 索引の<loc>が、CDATAで包まれている"""
    pages = {"https://example.com/sitemap.xml": _index([("https://example.com/post-sitemap.xml", "2026-10-01")], cdata=True),
             "https://example.com/post-sitemap.xml": _urlset([("https://example.com/news/a", "2026-10-01")])}
    cands, web = _run(monkeypatch, pages)
    assert [c["url"] for c in cands] == ["https://example.com/news/a"]
    assert "https://example.com/post-sitemap.xml" in web.calls


def test_a_failing_child_does_not_stop_the_others(monkeypatch):
    pages = {"https://example.com/sitemap.xml": _index([("https://example.com/a.xml", "2026-10-02"), ("https://example.com/b.xml", "2026-10-01")]),
             "https://example.com/a.xml": ConnectionError("down"),
             "https://example.com/b.xml": _urlset([("https://example.com/news/ok", "2026-10-01")])}
    cands, _ = _run(monkeypatch, pages)
    assert [c["url"].rsplit("/", 1)[1] for c in cands] == ["ok"]


def test_max_children_limits_the_children_read_newest_first(monkeypatch):
    children = [(f"https://example.com/c{i}.xml", f"2026-10-{10 + i:02d}") for i in range(5)]
    pages = {"https://example.com/sitemap.xml": _index(children)}
    pages.update({u: _urlset([(f"https://example.com/news/n{i}", f"2026-10-{10 + i:02d}")]) for i, (u, _) in enumerate(children)})
    cands, web = _run(monkeypatch, pages, search_conditions="max_links:60;max_children:2")
    assert sorted(web.calls[1:]) == ["https://example.com/c3.xml", "https://example.com/c4.xml"]     # 新しい2つだけ
    assert [c["url"].rsplit("/", 1)[1] for c in cands] == ["n4", "n3"]


def test_gzipped_child_sitemap(monkeypatch):
    pages = {"https://example.com/sitemap.xml": _index([("https://example.com/s.xml.gz", "2026-10-01")]),
             "https://example.com/s.xml.gz": gzip.compress(_urlset([("https://example.com/news/z", "2026-10-01")]).encode("utf-8"))}
    cands, _ = _run(monkeypatch, pages)
    assert [c["url"] for c in cands] == ["https://example.com/news/z"]


def test_plain_sitemap_still_works_and_markdown_copies_are_skipped(monkeypatch):
    """CDP: 記事URLと、同じ内容の .md 版が並ぶ。.md は、重複して保存しない"""
    pages = {"https://example.com/sitemap.xml": _urlset([("https://example.com/news/a", "2026-10-01"), ("https://example.com/news/a.md", "2026-10-01")])}
    cands, _ = _run(monkeypatch, pages)
    assert [c["url"] for c in cands] == ["https://example.com/news/a"]


def test_default_max_links_applies_after_sorting(monkeypatch):
    pages = {"https://example.com/sitemap.xml": _urlset([(f"https://example.com/news/n{i:03d}", f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}") for i in range(80)])}
    cands, _ = _run(monkeypatch, pages)
    assert len(cands) == ac.MAX_HTML_LINKS_PER_TARGET and cands[0]["url"].endswith("n079")


@pytest.mark.parametrize("sc,key,default,expected", [
    ("max_links:60;max_children:50", "max_children", None, "50"), ("max_links:60;max_children:50", "max_links", None, "60"),
    ("max_links:30", "max_links", None, "30"), ("force_zyte_browser", "max_links", None, None), ("", "max_links", "x", "x"), (None, "max_links", None, None)])
def test_search_setting(sc, key, default, expected):
    assert ac._search_setting({"search_conditions": sc}, key, default) == expected


def test_target_max_links_is_backward_compatible():
    assert ac._target_max_links({"search_conditions": "max_links:45"}) == 45
    assert ac._target_max_links({"search_conditions": "force_zyte_browser"}) == ac.MAX_HTML_LINKS_PER_TARGET
    assert ac._target_max_links({"search_conditions": "max_links:abc"}) == ac.MAX_HTML_LINKS_PER_TARGET
