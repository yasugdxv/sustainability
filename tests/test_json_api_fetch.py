"""JSON APIの一覧を、TinyFish Fetch(format=html)で取るテスト（2026-10-28追加）

背景: Zyte停止中、JSON API方式のcrawl_method(McDonald's・クボタ)が取得できず失敗が続いていた。
FetchはformatをhtmlにするとJSONを壊さずに返すことを確認したため、その経路を追加した。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402

TARGET = {"target_url": "https://www.kubota.co.jp/news/json/news_list.json", "domain": "kubota.co.jp",
          "crawl_method": "JSON API", "endpoint_type": "API・データ"}


@pytest.mark.parametrize("raw", [
    '<html>   <body>{ "news": [ { "date": "2026年10月27日", "title": "A", "url": "/news/2026/a.html" } ] }</body></html>',
    '{ "news": [ { "date": "2026年10月27日", "title": "A", "url": "/news/2026/a.html" } ] }',
    '[{"title": "A", "url": "/news/2026/a.html"}]',
])
def test_extract_json_text_strips_html_wrapper(raw):
    import json
    assert json.loads(ac._extract_json_text(raw))


def test_extract_json_text_returns_text_as_is_without_json():
    assert ac._extract_json_text("not json") == "not json"


def test_list_json_api_candidates_with_tinyfish_fetch_tier(monkeypatch):
    raw = ('<html> <body>{ "news": [ { "date": "2026年10月27日", "title": "設立", "entry_type": "個別記事タイプ", '
           '"url": "/news/2026/20261027-001275.html" } ] }</body></html>')
    monkeypatch.setattr(ac, "_call_tinyfish_fetch_raw", lambda url, proxies, verify: raw)

    cands, status = ac.list_json_api_candidates(TARGET, {}, True, tier="tinyfish_fetch")

    assert status == 200
    assert [c["url"] for c in cands] == ["https://www.kubota.co.jp/news/2026/20261027-001275.html"]
    assert cands[0]["title"] == "設立"


def test_list_json_api_candidates_with_tinyfish_fetch_returns_empty_for_non_json(monkeypatch):
    monkeypatch.setattr(ac, "_call_tinyfish_fetch_raw", lambda url, proxies, verify: "<html>blocked</html>")
    assert ac.list_json_api_candidates(TARGET, {}, True, tier="tinyfish_fetch")[0] == []
