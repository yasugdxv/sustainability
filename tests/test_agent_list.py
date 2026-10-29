"""一覧をTinyFish Agentで取る機能(search_conditionsのlist_via_agent)のテスト（2026-10-29追加）

背景: CDP・AB InBevは、一覧がJS描画で、Fetchには記事リンクが出ず、Zyte停止後は取れなかった。
一覧用の指示でAgentを呼ぶと、日付・タイトル・URLを取れたため、指定した対象だけが使う。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402

AGENT_TEXT = ("<html><body><article>listing\n\n"
              "Oct 7, 2026 | New CDP Analysis Finds SMEs Are Making Progress | https://www.cdp.net/en/press-releases/new-cdp-analysis\n"
              "Sep 21, 2026 | Companies Expect Hit From Water Impacts | https://www.cdp.net/en/press-releases/water-impacts\n"
              "Aug 6, 2026 | CDP and Japan&amp;#x27;s Ministry | https://www.cdp.net/en/press-releases/japan-ministry\n"
              "Aug 1, 2026 | 他ドメイン | https://example.com/en/press-releases/x\n"
              "Jul 1, 2026 | 対象外パス | https://www.cdp.net/en/about/team\n"
              "Oct 7, 2026 | 重複 | https://www.cdp.net/en/press-releases/new-cdp-analysis\n"
              "説明文の行で、URLなし\n"
              "</article></body></html>")


def _target(search="list_via_agent:true", include="/en/press-releases/"):
    return {"crawl_target_id": "t1", "target_url": "https://www.cdp.net/en/press-releases", "domain": "cdp.net",
            "crawl_method": "Zyte", "endpoint_type": "ニュース・一覧ページ", "include_paths": include, "search_conditions": search}


@pytest.fixture(autouse=True)
def _agent(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "_tinyfish_agent_site_calls", {})
    calls = []
    monkeypatch.setattr(ac, "_call_tinyfish_agent", lambda url, px, vf, goal=None: calls.append(goal) or (AGENT_TEXT, url, 200))
    return calls


@pytest.mark.parametrize("value,expected", [("list_via_agent:true", True), ("max_links:30;list_via_agent:TRUE", True),
                                            ("list_via_agent:false", False), (None, False), ("max_links:30", False)])
def test_target_list_via_agent_parsing(value, expected):
    assert ac._target_list_via_agent({"search_conditions": value}) is expected


def test_list_candidates_parses_lines_and_filters_domain_path_and_duplicates(_agent):
    cands, status = ac.list_candidates_via_agent(_target(), {}, True)

    assert status == 200
    assert [c["url"] for c in cands] == ["https://www.cdp.net/en/press-releases/new-cdp-analysis",
                                         "https://www.cdp.net/en/press-releases/water-impacts",
                                         "https://www.cdp.net/en/press-releases/japan-ministry"]
    assert cands[0]["published_at"].strftime("%Y-%m-%d") == "2026-10-07"
    assert cands[2]["title"] == "CDP and Japan's Ministry"          # HTMLの文字参照が戻る
    assert _agent == [ac.TINYFISH_AGENT_LIST_GOAL]                  # 一覧用の指示で呼ぶ


def test_list_candidates_raises_when_no_url_is_read(monkeypatch):
    monkeypatch.setattr(ac, "_call_tinyfish_agent", lambda *a, **k: ("<html><body><article>listing\n\n説明のみ</article></body></html>", "u", 200))
    with pytest.raises(RuntimeError):
        ac.list_candidates_via_agent(_target(), {}, True)


def test_list_candidates_respects_agent_budget(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 0)
    with pytest.raises(RuntimeError):
        ac.list_candidates_via_agent(_target(), {}, True)


def test_process_target_uses_agent_list_and_fetch_for_articles(monkeypatch):
    seen = {}
    monkeypatch.setattr(ac, "list_candidates_via_agent", lambda t, px, vf: ([{"url": "https://www.cdp.net/en/press-releases/a", "title": "a", "published_at": None}], 200))
    monkeypatch.setattr(ac, "_process_target_articles", lambda *a, **k: seen.update(args=a, kwargs=k) or {"run_result": "成功"})
    monkeypatch.setattr(ac, "_list_candidates_for_target", MagicMock(side_effect=AssertionError("通常の一覧取得は呼ばれないはず")))

    ac.process_target(_target(), MagicMock(), {}, True)

    assert seen["kwargs"]["article_tier"] == "tinyfish_fetch" and seen["kwargs"]["extra_funnel"] == {"p1_agent_list": 1}
    assert seen["args"][7] is None        # error_message


def test_process_target_records_agent_list_failure_as_error_message(monkeypatch):
    seen = {}
    monkeypatch.setattr(ac, "list_candidates_via_agent", MagicMock(side_effect=RuntimeError("TinyFish Agent: 目標未達（blocked）")))
    monkeypatch.setattr(ac, "_process_target_articles", lambda *a, **k: seen.update(args=a) or {"run_result": "失敗"})

    ac.process_target(_target(), MagicMock(), {}, True)

    assert seen["args"][4] == [] and "目標未達" in seen["args"][7]
