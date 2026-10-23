"""ソースコードの衛生チェックと、HTTPコードによる失敗分類の回帰テスト（2026-10-23追加）

背景: 正規表現の \\b(単語の境界)が、編集の途中で、制御文字(バックスペース 0x08)に化けて、_classify_failure の
「402・403・404・429」というHTTPコードによる分類が、効いていなかった(別のキーワードでは分類できていたため、テストをすり抜けた)。
同じ事故を、早く気づけるように、ソースに制御文字が混ざっていないかを、毎回検査する。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402

ROOT = Path(__file__).parent.parent
CONTROL = re.compile(rb"[\x00-\x08\x0b\x0e-\x1f]")        # タブ(0x09)・改行(0x0a)・復帰(0x0d)・改ページ(0x0c)は許可


def test_python_sources_contain_no_stray_control_characters():
    bad = {}
    for p in list(ROOT.glob("*.py")) + list(ROOT.glob("tests/*.py")):
        found = {hex(m.group()[0]) for m in CONTROL.finditer(p.read_bytes())}
        if found:
            bad[p.name] = sorted(found)
    assert not bad, f"制御文字が混ざっています(正規表現の \\b などが化けた可能性): {bad}"


@pytest.mark.parametrize("reason,expected", [
    ("HTTPError: 402 Client Error for url: https://agent.tinyfish.ai/v1/x", "credit_exhausted"),         # 「Payment Required」の文言なし
    ("HTTPError: 429 Client Error", "rate_limited"),
    ("HTTPError: 403 Client Error: Forbidden for url: https://www.sec.gov/files/a.pdf", "bot_blocked"),
    ("HTTPError: 404 Client Error: Not Found for url: https://example.com/gone", "not_article"),
    ("ステータス 404", "not_article"),
])
def test_classify_failure_by_http_code_alone(reason, expected):
    assert ac._classify_failure("tinyfish_fetch", reason) == expected


def test_http_codes_inside_a_url_are_not_mistaken_for_status_codes():
    assert ac._classify_failure("tinyfish_fetch", "RuntimeError: TinyFish Fetch: 取得失敗({'url': 'https://example.com/pr26403-and-404-report'") == "fetch_failed"
    assert ac._classify_failure("tinyfish_fetch", "error code 14030 happened") == "other"          # 数字の一部(14030)は、403として扱わない
