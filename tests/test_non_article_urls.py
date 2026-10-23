"""記事ではないURLの除外と、PDFのメタデータ不備への対応のテスト（2026-10-23追加）

背景: 2026-10-23の最終失敗170記事のうち、13件は問い合わせ・登録・求人・API等の「記事ではないページ」で、
有料のAgentまで使って失敗していた。また、伊藤忠のPDFは、メタデータの日付が壊れていて、本文が読めるのに
「文書抽出エラー: Can not convert date」で落ちていた。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402


def _list(monkeypatch, links, include_paths="/", domain="example.com"):
    html = "<html><body>" + "".join(f'<a href="{h}">x</a>' for h in links) + "</body></html>"
    monkeypatch.setattr(ac, "_fetch_with_escalation",
                        MagicMock(return_value=(html, f"https://{domain}/news/", 200, None)))
    target = {"target_url": f"https://{domain}/news/", "domain": domain,
              "include_paths": include_paths, "exclude_paths": "", "search_conditions": ""}
    cands, _ = ac.list_html_candidates(target, {}, True)
    return {c["url"] for c in cands}


# 2026-10-23に、最終失敗した記事ではないURLの実例
@pytest.mark.parametrize("url", [
    "https://www.gea.com/ja/contact",
    "https://www.gea.com/ja/en/forms/media-contact-form",
    "https://globallivingwage.org/subscribe",
    "https://globallivingwage.org/wp-content/themes/glwc",
    "https://www.generalmills.com/api/download",
    "https://www.alpla.com/en/regional-contacts",
    "https://www.meti.go.jp/press/sitemap/index.html",
    "https://www.unesco.org/en/search/artificial-intelligence",
    "https://www.starbucks.co.jp/recruit",
    "https://jobs.yum.com/",
    "https://idp.nature.com/auth/personal/springernature",
])
def test_non_article_urls_are_not_candidates(monkeypatch, url):
    host = urlparse(url).netloc
    root_domain = ".".join(host.split(".")[-2:]) if not host.endswith((".co.jp", ".go.jp")) else ".".join(host.split(".")[-3:])
    real = f"https://www.{root_domain}/news/2026/real-article-1"     # 本体ドメインの記事（jobs.やidp.などのホストは、ホストごと除外する仕様）
    got = _list(monkeypatch, [url, real], domain=root_domain)
    assert url.rstrip("/") not in {g.rstrip("/") for g in got}
    assert real in got                                               # 本当の記事は、残る


def test_slugs_that_merely_contain_the_words_are_kept(monkeypatch):
    """区切りごとの完全一致なので、記事のスラッグに「contact」「search」が含まれていても、巻き込まない"""
    got = _list(monkeypatch, ["https://example.com/news/contact-center-trends-2026",
                              "https://example.com/news/research-and-search-engines"])
    assert len(got) == 2


def test_explicit_include_paths_win_over_the_denylist(monkeypatch):
    """include_pathsで「/search/」のように具体的に指定している対象は、その指定を優先する"""
    got = _list(monkeypatch, ["https://example.com/search/report-1", "https://example.com/other/report-2"],
                include_paths="/search/")
    assert got == {"https://example.com/search/report-1"}


# ─── PDFのメタデータ ───
class _BrokenMeta:
    title = "題名"

    @property
    def creation_date(self):
        raise ValueError("Can not convert date: 㩄〲㘲㤰")

    @property
    def modification_date(self):
        raise ValueError("Can not convert date")


def test_pdf_with_broken_date_metadata_still_yields_the_text(monkeypatch):
    page = MagicMock()
    page.extract_text.return_value = "本文です。" * 20
    reader = MagicMock(metadata=_BrokenMeta(), pages=[page])
    monkeypatch.setattr(ac, "PdfReader", lambda *_: reader)

    parsed = ac._extract_pdf(b"%PDF-dummy")

    assert parsed["text"].startswith("本文です。") and parsed["title"] == "題名"
    assert parsed["published_at"] is None and parsed["updated_at"] is None
