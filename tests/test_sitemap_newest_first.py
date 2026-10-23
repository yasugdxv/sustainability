"""サイトマップの一覧を、新しい順に並べてから、上限を適用するテスト（2026-10-23追加）

背景: 従来は、ファイルの先頭から上限(30件)まで取っていた。ISSB(IFRS)のサイトマップは、古い順(2018年〜)に
2,135件のニュース記事が並び、古い30件しか見ず、新しい記事に届かなかった。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402


def _sitemap(entries):
    """entries: [(loc, lastmod or None)]"""
    items = "".join(f"<url><loc>{loc}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>" for loc, lm in entries)
    return f'<?xml version="1.0"?><urlset>{items}</urlset>'


def _list(monkeypatch, entries, include="/news/", search_conditions=""):
    resp = MagicMock(status_code=200, text=_sitemap(entries))
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: resp)
    target = {"target_url": "https://example.com/sitemap.xml", "domain": "example.com", "include_paths": include,
              "exclude_paths": "", "search_conditions": search_conditions}
    cands, status = ac.list_sitemap_candidates(target, {}, True)
    return cands


def test_oldest_first_sitemap_without_lastmod_returns_the_newest_by_url_date(monkeypatch):
    """ISSBと同じ型: 古い順に並び、lastmodが無く、URLに年月が入っている"""
    entries = [(f"https://example.com/news/{y}/{m:02d}/article-{y}-{m}", None) for y in range(2018, 2027) for m in range(1, 13)]
    cands = _list(monkeypatch, entries)
    urls = [c["url"] for c in cands]
    assert len(cands) == ac.MAX_HTML_LINKS_PER_TARGET
    assert urls[0].endswith("/2026/12/article-2026-12") and all("/2018/" not in u for u in urls)   # 新しい順で、古い記事は入らない
    assert all(c["published_at"] is None for c in cands)        # URLの日付は、並べ替えだけに使い、公開日にはしない（lookbackで誤って除外しない）


def test_lastmod_is_used_when_available_and_newest_comes_first(monkeypatch):
    entries = [("https://example.com/news/a", "2026-01-01"), ("https://example.com/news/b", "2026-10-20"),
               ("https://example.com/news/c", "2026-05-05")]
    cands = _list(monkeypatch, entries)
    assert [c["url"].rsplit("/", 1)[1] for c in cands] == ["b", "c", "a"]
    assert cands[0]["published_at"] == datetime(2026, 10, 20, tzinfo=timezone.utc)


def test_undated_entries_come_last_in_file_order(monkeypatch):
    entries = [("https://example.com/news/x1", None), ("https://example.com/news/2026/10/y", None), ("https://example.com/news/x2", None)]
    cands = _list(monkeypatch, entries)
    assert [c["url"].rsplit("/", 1)[1] for c in cands] == ["y", "x1", "x2"]


def test_sitemap_with_no_dates_at_all_keeps_the_file_order(monkeypatch):
    entries = [(f"https://example.com/news/page-{i}", None) for i in range(50)]
    cands = _list(monkeypatch, entries)
    assert [c["url"].rsplit("/", 1)[1] for c in cands][:3] == ["page-0", "page-1", "page-2"]
    assert len(cands) == ac.MAX_HTML_LINKS_PER_TARGET


def test_max_links_setting_is_respected(monkeypatch):
    entries = [(f"https://example.com/news/2026/{m:02d}/a{m}", None) for m in range(1, 13)]
    assert len(_list(monkeypatch, entries, search_conditions="max_links:5")) == 5


def test_include_paths_still_filter(monkeypatch):
    entries = [("https://example.com/news/2026/10/a", None), ("https://example.com/other/2026/10/b", None)]
    assert [c["url"] for c in _list(monkeypatch, entries)] == ["https://example.com/news/2026/10/a"]
