"""Bot確認の画面(HTTP 200)を、記事として保存しないための検知のテスト（2026-10-23追加）

背景: PEFCのサイトマップから記事を取る試験で、題名「Security check」の、中身のない記事(「短い確認を完了してください」
「JavaScriptが必要です」)が、保存される寸前だった。既存の検知パターンに、この文言がなかった。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402

PEFC_TEXT = ("To keep the PEFC website available for everyone, we occasionally ask visitors to complete a short check. It only takes a moment.\n"
             "Please wait…\nAfter this check you will return to the page you requested.\n"
             "JavaScript is required to continue. Please enable JavaScript and reload this page.")


def test_pefc_security_check_page_is_detected_by_text_and_by_title():
    assert ac._detect_bot_block("https://pefc.org/news/x", PEFC_TEXT) is not None
    assert ac._detect_bot_block("https://pefc.org/news/x", "Please wait…", title="Security check") is not None


@pytest.mark.parametrize("text", [
    "Checking your browser before accessing example.com. This process is automatic.",
    "Just a moment... Verifying your connection, please wait.",
    "Enable JavaScript and cookies to continue",
    "DDoS protection by Cloudflare. Ray ID: 8a1b2c",
    "JavaScript is required to view this page.",
])
def test_common_challenge_wordings_are_detected(text):
    assert ac._detect_bot_block("https://example.com/a", text) is not None


@pytest.mark.parametrize("title", ["Security check", "Just a moment...", "Attention Required! | Cloudflare", "Access denied", "Verifying you are human. This may take a few seconds."])
def test_challenge_titles_are_detected_only_for_short_bodies(title):
    assert ac._detect_bot_block("https://example.com/a", "Please wait.", title=title) is not None
    assert ac._detect_bot_block("https://example.com/a", "本文です。" * 400, title=title) is None       # 長い本文は、題名だけでは判定しない


@pytest.mark.parametrize("title,text", [
    ("Security check policy updated for 2027", "The new security check policy applies to all visitors."),
    ("JavaScript tutorial", "JavaScript is a programming language. Learn it today."),
    ("Government announces a short check on bank stability", "Regulators will complete a review of the sector this year."),
    ("Weekly climate news", "Temperatures were high. " * 20),
])
def test_ordinary_articles_are_not_flagged(title, text):
    assert ac._detect_bot_block("https://example.com/a", text, title=title) is None


def test_long_text_containing_the_phrase_is_not_flagged():
    long_article = "Our analysts say users must complete a short check before filing. " + "本文。" * 500
    assert ac._detect_bot_block("https://example.com/a", long_article) is None


def test_existing_detections_still_work():
    assert ac._detect_bot_block("https://x.perfdrive.com/a", "x") is not None
    assert ac._detect_bot_block("https://example.com/a", "Please verify you are a human.") is not None
    assert ac._detect_bot_block("https://example.com/a", "") is None
