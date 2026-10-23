"""フィード型の一覧の読み取り、サイレント故障の検知、登録の検査のテスト（2026-10-23追加）

背景: 方式を「RSS→Zyte/HTML」に変えたのに、種別がRSSのままの8対象が、フィードのURLを「1ページ」として扱い、
全期間で記事0件（Amnestyは、フィードXMLの7万文字を1件の「記事」として保存）。また、include_pathsがフィードの
パスのままのHTML 6対象も、半年近く0件のまま、気づかれなかった。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
import audit_crawl_targets as audit  # noqa: E402
import crawl_funnel_report as rep  # noqa: E402


def _target(**kw):
    t = {"crawl_target_id": "t1", "target_name": "T", "target_url": "https://example.com/feed/", "domain": "example.com",
         "crawl_method": "Zyte", "endpoint_type": "RSS", "include_paths": "/feed/", "exclude_paths": "", "search_conditions": ""}
    t.update(kw)
    return t


# ─── フィード型の判定 ───
@pytest.mark.parametrize("url,etype,expected", [
    ("https://www.ran.org/understory/feed/", "RSS", True),
    ("https://unwater.org/rss.xml", "RSS", True),
    ("https://amnesty.org/en/latest/feed", "RSS", True),
    ("https://blog.google/rss", "RSS", True),
    ("https://www.salesforce.com/news/topics/company-news/", "RSS", False),      # 種別はRSSだが、HTMLの一覧
    ("https://example.com/feed/", "ニュース・一覧ページ", False),                  # 種別がRSSではない
    ("https://example.com/news/", "サイトトップ", False),
])
def test_is_feed_target(url, etype, expected):
    assert ac._is_feed_target(_target(target_url=url, endpoint_type=etype)) is expected


# ─── 一覧の振り分け ───
def test_feed_target_with_zyte_method_is_read_as_rss_in_pass1(monkeypatch):
    rss = MagicMock(return_value=([{"url": "https://example.com/a1", "title": "t", "published_at": None}], 200))
    monkeypatch.setattr(ac, "list_rss_candidates", rss)

    cands, status, single, err, is_404, *_ = ac._list_candidates_for_target(_target(), {}, True)

    assert rss.called and len(cands) == 1 and single is False and err is None     # 1ページ扱いにならない


def test_feed_target_with_tier_uses_rss_but_never_the_agent(monkeypatch):
    rss = MagicMock(return_value=([{"url": "https://example.com/a1", "title": "t", "published_at": None}], 200))
    monkeypatch.setattr(ac, "list_rss_candidates", rss)

    cands, _, single, err, _ = ac._list_candidates_with_tier(_target(), {}, True, "tinyfish_fetch")
    assert len(cands) == 1 and single is False and err is None
    assert rss.call_args.kwargs["allow_escalation"] is False

    rss.reset_mock()
    cands, _, single, err, _ = ac._list_candidates_with_tier(_target(), {}, True, "tinyfish_agent")
    assert cands == [] and "Agent" in err and not rss.called                       # 構造が壊れるAgentでは、有料の呼び出しをしない


def test_listing_type_with_rss_endpoint_but_html_url_stays_a_single_page(monkeypatch):
    """Salesforceのように、種別がRSSでもURLがフィードの形でなければ、従来どおり（フィードとして読まない）"""
    rss = MagicMock()
    monkeypatch.setattr(ac, "list_rss_candidates", rss)
    t = _target(target_url="https://www.salesforce.com/news/topics/company-news/", crawl_method="HTML", include_paths="")

    _, _, single, _, _, *_ = ac._list_candidates_for_target(t, {}, True)

    assert single is True and not rss.called


# ─── サイレント故障の検知 ───
def _list_html(monkeypatch, links, include_paths, domain="example.com"):
    html = "<html><body>" + "".join(f'<a href="{h}">x</a>' for h in links) + "</body></html>"
    monkeypatch.setattr(ac, "_fetch_with_escalation", MagicMock(return_value=(html, f"https://{domain}/news/", 200, None)))
    t = {"target_url": f"https://{domain}/news/", "domain": domain, "include_paths": include_paths,
         "exclude_paths": "", "search_conditions": ""}
    cands, _ = ac.list_html_candidates(t, {}, True)
    return t, cands


def test_include_nomatch_is_recorded_when_article_like_links_exist_but_none_match(monkeypatch):
    links = [f"https://example.com/en/news-and-stories/story-number-{i}" for i in range(5)]
    t, cands = _list_html(monkeypatch, links, include_paths="/news-and-stories/press-release")     # UNEPと同じ型

    assert cands == []
    st = t["_listing_stats"]
    assert st["include_nomatch"] == 5 and st["top"] == ["/en/news-and-stories"]
    assert ac._include_nomatch_funnel(t) == {"include_nomatch": {"n": 5, "top": ["/en/news-and-stories"]}}


def test_no_warning_when_some_links_match_or_too_few_links(monkeypatch):
    links = ["https://example.com/news/a-long-article-slug-2026", "https://example.com/other/news/x1", "https://example.com/other/news/x2",
             "https://example.com/other/news/x3"]
    t, cands = _list_html(monkeypatch, links, include_paths="/news/")
    assert len(cands) == 1 and "_listing_stats" not in t and ac._include_nomatch_funnel(t) == {}

    t, cands = _list_html(monkeypatch, ["https://example.com/en/news/a1", "https://example.com/en/news/a2"], include_paths="/news/")
    assert cands == [] and "_listing_stats" not in t                       # 2件では、警告しない（偶然の可能性）


def test_non_article_links_do_not_trigger_the_warning(monkeypatch):
    links = [f"https://example.com/about/{x}" for x in ("history", "team", "contact", "careers", "offices")]   # 記事らしくないリンク
    t, cands = _list_html(monkeypatch, links, include_paths="/news/")
    assert cands == [] and "_listing_stats" not in t


def test_pass1_funnel_carries_the_warning():
    src = (Path(__file__).parent.parent / "article_crawler.py").read_text(encoding="utf-8")
    assert "crawl_funnel.update(_include_nomatch_funnel(target))" in src


# ─── 毎日の集計 ───
def test_funnel_report_lists_include_nomatch_targets():
    logs = [{"crawl_target_id": "t1", "run_result": "更新なし", "started_at": "2026-10-23T00:00:00+00:00",
             "notes": ac._compose_notes("", {"include_nomatch": {"n": 56, "top": ["/en/news-and-stories"]}})},
            {"crawl_target_id": "t2", "run_result": "成功", "started_at": "2026-10-23T00:01:00+00:00", "notes": None}]
    s = rep.summarize(logs)
    assert s["include_nomatch"] == [("t1", {"n": 56, "top": ["/en/news-and-stories"]})]
    assert "[警告] 条件(include_paths)に合う記事が0件" in rep.render(s, "x")


# ─── 登録の検査 ───
def _codes(targets, counts=None):
    return sorted((i["code"], i["target"]) for i in audit.find_issues(targets, counts))


def test_audit_flags_the_known_misconfigurations():
    ts = [
        _target(crawl_target_id="a", target_name="Amnesty", crawl_method="Zyte"),                                    # フィードなのに方式がZyte
        _target(crawl_target_id="b", target_name="Salesforce", crawl_method="HTML", target_url="https://x.com/news/", include_paths=""),
        _target(crawl_target_id="c", target_name="FarsNews", crawl_method="HTML", endpoint_type="ニュース・一覧ページ",
                target_url="https://farsnews.ir/showcase", include_paths="/rss/"),                                  # HTML一覧なのにフィード形式のinclude
        _target(crawl_target_id="d", target_name="OK-RSS", crawl_method="RSS"),
        _target(crawl_target_id="e", target_name="OK-HTML", crawl_method="HTML", endpoint_type="ニュース・一覧ページ",
                target_url="https://x.com/news/", include_paths="/news/"),
    ]
    got = _codes(ts)
    assert ("FEED_TYPE_NOT_RSS", "Amnesty") in got
    assert ("RSS_TYPE_BUT_HTML_URL", "Salesforce") in got
    assert ("FEED_INCLUDE_ON_HTML", "FarsNews") in got
    assert not any(t in ("OK-RSS", "OK-HTML") for _, t in got)


def test_audit_no_articles_30d_only_for_active_methods():
    ts = [_target(crawl_target_id="a", target_name="A", crawl_method="RSS"),
          _target(crawl_target_id="b", target_name="B", crawl_method="RSS"),
          _target(crawl_target_id="c", target_name="C", crawl_method="廃止")]
    got = _codes(ts, {"a": 5})
    assert got == [("NO_ARTICLES_30D", "B")]
    assert _codes(ts) == []                                                    # 記事数を渡さなければ、検査しない
