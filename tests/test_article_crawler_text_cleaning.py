"""article_crawler.py の本文クリーニング（_clean_text/_truncate_at_paywall）のテスト。
sustainablejapan.jp等、有料会員限定記事で本文プレビューの途中から登録・ログイン誘導の
定型文に切り替わるケースの回帰テスト（実データで確認した実例に基づく）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_crawler as ac  # noqa: E402


def test_truncate_at_paywall_removes_registration_boilerplate():
    text = (
        "SOMPOホールディングスは8月31日、気候移行計画に関するエンゲージメント方針を発表した。\n"
        "この記事は有料会員限定です。\n"
        "無料会員に登録すると、\n"
        "有料記事の「閲覧チケット」を毎月1枚プレゼント。\n"
        "会員の方はこちらからログイン"
    )
    result = ac._truncate_at_paywall(text)
    assert result == "SOMPOホールディングスは8月31日、気候移行計画に関するエンゲージメント方針を発表した。"


def test_truncate_at_paywall_handles_missing_intro_phrase():
    """「この記事は有料会員限定です」の一文が無いケース（実データで確認済み）でも、
    「無料会員に登録すると」だけで検知できること"""
    text = "本文の続き。\n無料会員に登録すると、\n有料記事の「閲覧チケット」を毎月1枚プレゼント。"
    result = ac._truncate_at_paywall(text)
    assert result == "本文の続き。"


def test_truncate_at_paywall_no_match_returns_unchanged():
    text = "通常の記事本文で、有料会員に関する話題は一切含まれない。"
    assert ac._truncate_at_paywall(text) == text


def test_truncate_at_paywall_empty_text():
    assert ac._truncate_at_paywall("") == ""
    assert ac._truncate_at_paywall(None) is None


def test_clean_text_applies_paywall_truncation_before_line_filtering():
    text = (
        "実際の記事内容。\n"
        "この記事は有料会員限定です。\n"
        "シェアする\n"
        "会員の方はこちらからログイン"
    )
    result = ac._clean_text(text)
    assert result == "実際の記事内容。"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
