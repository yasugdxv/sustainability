"""article_filter._compile: 英語キーワードの複数形(s/es)も一致すること、略称・数字終わりは対象外のままであること。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_filter  # noqa: E402


def _hit(kw, text):
    return bool(article_filter._compile(kw).search(text))


def test_plural_forms_match():
    assert _hit("ESG rating", "Top ESG ratings in 2026")
    assert _hit("pesticide", "New pesticides rules")
    assert _hit("tax", "carbon taxes rise")  # es


def test_no_false_extension():
    assert not _hit("ESG rating", "ESG ratingsx")  # 単語の続きは不一致
    assert not _hit("war", "warehouse")


def test_acronyms_and_digits_unchanged():
    assert not _hit("WHO", "WHOs list")        # 短い略称は複数形にしない
    assert not _hit("SB 253", "SB 2530")
    assert _hit("SB 253", "California SB 253 applies")
