"""Zyteの取得で、HTTPモードでは足りないとき、Browser Renderingへ切り替えるテスト（2026-11-13追加）

背景: 欧州委 presscorner(HTML 2.2万字のうち読める文字は40字)・consilium・oecd.org(HTTPは520)は、
HTTPモードでは本文が取れず、Browser Renderingなら取れる。実APIは呼ばず、_call_zyteをモックして検証する。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402
import requests  # noqa: E402

import article_crawler as ac  # noqa: E402

URL = "https://example.com/news/1"
# スクリプトが大半で、読める文字がほとんど無いページ(JS描画)
SPA = "<html><head>" + "<script>var a = 1;</script>" * 800 + "</head><body><div id='app'></div></body></html>"
BROWSER_HTML = "<html><body><p>" + "本文です。" * 100 + "</p></body></html>"
GOOD_HTML = "<html><body><p>" + "普通の記事の本文です。" * 100 + "</p></body></html>"


def _http_error(code):
    resp = MagicMock()
    resp.status_code = code
    return requests.exceptions.HTTPError(response=resp)


def _fake_zyte(calls, http_result=None, http_exc=None, browser_html=BROWSER_HTML):
    def fake(url, proxies, verify, browser, wait_sec=5):
        calls.append("browser" if browser else "http")
        if browser:
            return browser_html, url, 200, None
        if http_exc:
            raise http_exc
        return http_result[0], url, 200, http_result[1]
    return fake


def test_thin_visible_text_escalates_to_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(ac, "_call_zyte", _fake_zyte(calls, http_result=(SPA, SPA.encode("utf-8"))))

    html, final_url, status, raw = ac._fetch_with_zyte(URL, {}, True)

    assert calls == ["http", "browser"]
    assert "本文です" in html and raw is None


@pytest.mark.parametrize("code", [520, 521, 524, 529])
def test_site_side_failure_status_retries_with_browser(monkeypatch, code):
    calls = []
    monkeypatch.setattr(ac, "_call_zyte", _fake_zyte(calls, http_exc=_http_error(code)))

    html, final_url, status, raw = ac._fetch_with_zyte(URL, {}, True)

    assert calls == ["http", "browser"]
    assert "本文です" in html


@pytest.mark.parametrize("code", [401, 402, 403, 404, 429, 500])
def test_auth_billing_and_other_errors_do_not_retry_with_browser(monkeypatch, code):
    calls = []
    monkeypatch.setattr(ac, "_call_zyte", _fake_zyte(calls, http_exc=_http_error(code)))

    with pytest.raises(requests.exceptions.HTTPError):
        ac._fetch_with_zyte(URL, {}, True)

    assert calls == ["http"]                  # アカウント停止(403)等は、Browserでも直らず費用だけかかる


def test_normal_page_does_not_use_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(ac, "_call_zyte", _fake_zyte(calls, http_result=(GOOD_HTML, GOOD_HTML.encode("utf-8"))))

    html, final_url, status, raw = ac._fetch_with_zyte(URL, {}, True)

    assert calls == ["http"] and raw is not None


def test_pdf_with_no_visible_text_does_not_use_browser(monkeypatch):
    calls = []
    pdf_bytes = b"%PDF-1.7 " + b"\x00" * 3000
    monkeypatch.setattr(ac, "_call_zyte", _fake_zyte(calls, http_result=("\x00" * 3000, pdf_bytes)))

    html, final_url, status, raw = ac._fetch_with_zyte("https://example.com/doc.pdf", {}, True)

    assert calls == ["http"] and raw == pdf_bytes


@pytest.mark.parametrize("body", ["<?xml version='1.0'?><rss><channel></channel></rss>" + " " * 600,
                                  "<rss><channel><item><title>a</title></item></channel></rss>" + " " * 600,
                                  '{"items": []}' + " " * 600])
def test_xml_and_json_do_not_use_browser(monkeypatch, body):
    calls = []
    monkeypatch.setattr(ac, "_call_zyte", _fake_zyte(calls, http_result=(body, body.encode("utf-8"))))

    ac._fetch_with_zyte("https://example.com/feed", {}, True)

    assert calls == ["http"]


def test_force_browser_still_skips_http(monkeypatch):
    calls = []
    monkeypatch.setattr(ac, "_call_zyte", _fake_zyte(calls))

    ac._fetch_with_zyte(URL, {}, True, force_browser=True)

    assert calls == ["browser"]


def test_visible_text_len_ignores_scripts_styles_and_tags():
    assert ac._visible_text_len("<html><script>alert(1)</script><style>a{}</style><p>あ い  う</p></html>") == 5
