"""一覧ページの外部ドメインのリンクを記事候補にする設定(allow_external)のテスト（2026-10-27追加）

背景: G20(g20.org)のトップのニュース欄は、米国政府の各機関(ustr.gov・state.gov等)の記事へのリンクだけで
構成されており、「同じドメインのリンクのみ」の既定の条件では、1件も拾えなかった。
search_conditionsに"allow_external:true"を指定したターゲットだけ、外部ドメインのリンクも候補にする。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402

LIST_HTML = ("<html><body>"
             '<a href="https://ustr.gov/about/policy-offices/press-office/press-releases/2026/october/a">a</a>'
             '<a href="https://www.state.gov/releases/office-of-the-spokesman/2026/09/b">b</a>'
             '<a href="https://www.whitehouse.gov/releases/2026/09/c/">c</a>'
             '<a href="https://twitter.com/g20">sns</a>'
             '<a href="https://ustr.gov/about/policy-offices/press-office/contact">対象外のパス</a>'
             '<a href="https://g20.org/about/history">自サイト内</a>'
             "</body></html>")
INCLUDE = "/about/policy-offices/press-office/press-releases/,/releases/"


@pytest.fixture(autouse=True)
def _page(monkeypatch):
    monkeypatch.setattr(ac, "_fetch_page_with_tier", lambda *a, **k: (LIST_HTML, "https://g20.org/", 200))


def _target(search=None, include=INCLUDE):
    return {"target_url": "https://g20.org/", "domain": "g20.org", "crawl_method": "Zyte",
            "include_paths": include, "search_conditions": search}


def _urls(target):
    return [c["url"] for c in ac.list_html_candidates(target, {}, True, tier="tinyfish_fetch")[0]]


def test_external_links_are_ignored_by_default():
    assert _urls(_target(search=None, include="/")) == ["https://g20.org/about/history"]    # 従来どおり、同じドメインのみ


def test_allow_external_picks_external_article_links_matching_include_paths():
    urls = _urls(_target(search="allow_external:true"))
    assert urls == ["https://ustr.gov/about/policy-offices/press-office/press-releases/2026/october/a",
                    "https://www.state.gov/releases/office-of-the-spokesman/2026/09/b",
                    "https://www.whitehouse.gov/releases/2026/09/c"]                          # SNS・条件外のパス・自サイト内は入らない


def test_allow_external_is_ignored_without_include_paths():
    assert _urls(_target(search="allow_external:true", include="")) == ["https://g20.org/about/history"]   # 外部リンクが全部入るのを防ぐ


def test_allow_external_can_be_combined_with_max_links():
    assert len(_urls(_target(search="max_links:2;allow_external:true"))) == 2


@pytest.mark.parametrize("value,expected", [("allow_external:true", True), ("allow_external:TRUE", True),
                                            ("allow_external:false", False), (None, False), ("max_links:30", False)])
def test_target_allow_external_parsing(value, expected):
    assert ac._target_allow_external({"search_conditions": value}) is expected
