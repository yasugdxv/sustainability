"""article_crawler.py のBot対策フォールバックチェーン
（通常取得 → Zyte → TinyFish Fetch → TinyFish Agent）のテスト。

2026-10-09追加: Zyte/TinyFish PoCでの検証結果(52件のbot対策失敗targetに対し
チェーンさせると100%解決、うちAgentが必要だったのは1件のみ)を踏まえ、
_fetch_with_escalation()に実装した。実HTTP/実API呼び出しは一切行わず、
requests.post/requests.getや各段の呼び出し関数をモックして検証する。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402
import requests  # noqa: E402

import article_crawler as ac  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_tinyfish_state(monkeypatch):
    """TinyFish APIキーのプロセス内キャッシュ・Agent呼び出し回数カウンタを
    テストごとにリセットする(グローバル状態が別テストへ漏れないように)"""
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy-tinyfish-key")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)


def _mock_response(status_code=200, text="OK", raise_exc=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = text.encode("utf-8")
    resp.url = "https://example.com/final"
    if raise_exc:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.side_effect = None
    return resp


def _http_error(status_code):
    resp = MagicMock()
    resp.status_code = status_code
    return requests.exceptions.HTTPError(response=resp)


# ─── 正常系: 通常取得で成功すれば、Zyte/TinyFishは一切呼ばれない ──────────
def test_normal_fetch_success_skips_escalation(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry", lambda *a, **k: _mock_response(200, "<html>ok</html>"))
    monkeypatch.setattr(ac, "_decoded_html", lambda resp: resp.text)
    zyte_mock = MagicMock(side_effect=AssertionError("Zyteは呼ばれないはず"))
    tf_fetch_mock = MagicMock(side_effect=AssertionError("TinyFish Fetchは呼ばれないはず"))
    monkeypatch.setattr(ac, "_fetch_with_zyte", zyte_mock)
    monkeypatch.setattr(ac, "_call_tinyfish_fetch", tf_fetch_mock)

    html, final_url, status_code, raw_content = ac._fetch_with_escalation(
        "https://example.com/", {}, True)

    assert html == "<html>ok</html>"
    assert status_code == 200
    zyte_mock.assert_not_called()
    tf_fetch_mock.assert_not_called()


# ─── 404は即座に失敗、Zyte/TinyFishへエスカレーションしない ──────────────
def test_404_does_not_escalate(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(_http_error(404)))
    zyte_mock = MagicMock(side_effect=AssertionError("404でZyteは呼ばれないはず"))
    tf_fetch_mock = MagicMock(side_effect=AssertionError("404でTinyFishは呼ばれないはず"))
    monkeypatch.setattr(ac, "_fetch_with_zyte", zyte_mock)
    monkeypatch.setattr(ac, "_call_tinyfish_fetch", tf_fetch_mock)

    with pytest.raises(requests.exceptions.HTTPError):
        ac._fetch_with_escalation("https://example.com/notfound", {}, True)

    zyte_mock.assert_not_called()
    tf_fetch_mock.assert_not_called()


# ─── 通常取得が403等(404以外)で失敗 → Zyteへエスカレーション ──────────────
def test_non_404_failure_escalates_to_zyte(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(_http_error(403)))
    monkeypatch.setattr(ac, "_fetch_with_zyte",
                         lambda *a, **k: ("<html>zyte</html>", "https://example.com/final", 200, b"raw"))
    tf_fetch_mock = MagicMock(side_effect=AssertionError("Zyte成功時はTinyFishは呼ばれないはず"))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch", tf_fetch_mock)

    html, final_url, status_code, raw_content = ac._fetch_with_escalation(
        "https://example.com/", {}, True)

    assert html == "<html>zyte</html>"
    assert raw_content == b"raw"
    tf_fetch_mock.assert_not_called()


# ─── 通常取得・Zyte両方失敗 → TinyFish Fetchへエスカレーション ────────────
def test_zyte_failure_escalates_to_tinyfish_fetch(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(_http_error(403)))
    monkeypatch.setattr(ac, "_fetch_with_zyte",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Zyte失敗")))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch",
                         lambda *a, **k: ("<html>tinyfish</html>", "https://example.com/final", 200))

    html, final_url, status_code, raw_content = ac._fetch_with_escalation(
        "https://example.com/", {}, True)

    assert html == "<html>tinyfish</html>"
    assert raw_content is None


# ─── 全段階失敗・allow_tinyfish_agent=False → Agentは呼ばれず例外を送出 ────
def test_all_fail_without_agent_raises(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(_http_error(403)))
    monkeypatch.setattr(ac, "_fetch_with_zyte",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Zyte失敗")))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("TinyFish Fetch失敗")))
    agent_mock = MagicMock(side_effect=AssertionError("allow_tinyfish_agent=FalseならAgentは呼ばれないはず"))
    monkeypatch.setattr(ac, "_call_tinyfish_agent", agent_mock)

    with pytest.raises(RuntimeError, match="TinyFish Fetch失敗"):
        ac._fetch_with_escalation("https://example.com/", {}, True, allow_tinyfish_agent=False)

    agent_mock.assert_not_called()


# ─── 全段階失敗・allow_tinyfish_agent=True → Agentへエスカレーションし、
#     呼び出し回数カウンタが増える ─────────────────────────────────────
def test_all_fail_with_agent_allowed_escalates_to_agent(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(_http_error(403)))
    monkeypatch.setattr(ac, "_fetch_with_zyte",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Zyte失敗")))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("TinyFish Fetch失敗")))
    monkeypatch.setattr(ac, "_call_tinyfish_agent",
                         lambda *a, **k: ("<html>agent</html>", "https://example.com/", 200))

    html, final_url, status_code, raw_content = ac._fetch_with_escalation(
        "https://example.com/", {}, True, allow_tinyfish_agent=True)

    assert html == "<html>agent</html>"
    assert ac._tinyfish_agent_call_count == 1


# ─── Agent呼び出し上限に達している場合はAgentへエスカレーションせず例外を送出 ──
def test_agent_budget_exhausted_does_not_call_agent(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", ac.TINYFISH_AGENT_RUN_CAP)
    monkeypatch.setattr(ac, "_requests_get_with_retry",
                         lambda *a, **k: (_ for _ in ()).throw(_http_error(403)))
    monkeypatch.setattr(ac, "_fetch_with_zyte",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Zyte失敗")))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("TinyFish Fetch失敗")))
    agent_mock = MagicMock(side_effect=AssertionError("上限到達後はAgentは呼ばれないはず"))
    monkeypatch.setattr(ac, "_call_tinyfish_agent", agent_mock)

    with pytest.raises(RuntimeError, match="TinyFish Fetch失敗"):
        ac._fetch_with_escalation("https://example.com/", {}, True, allow_tinyfish_agent=True)

    agent_mock.assert_not_called()


# ─── start_with_zyte=True の場合は通常取得を省略しZyteから開始する ────────
def test_start_with_zyte_skips_normal_fetch(monkeypatch):
    normal_mock = MagicMock(side_effect=AssertionError("start_with_zyte=Trueなら通常取得は呼ばれないはず"))
    monkeypatch.setattr(ac, "_requests_get_with_retry", normal_mock)
    monkeypatch.setattr(ac, "_fetch_with_zyte",
                         lambda *a, **k: ("<html>zyte</html>", "https://example.com/final", 200, None))

    html, final_url, status_code, raw_content = ac._fetch_with_escalation(
        "https://example.com/", {}, True, start_with_zyte=True)

    assert html == "<html>zyte</html>"
    normal_mock.assert_not_called()


# ─── _call_tinyfish_fetch(): want_links=Trueでリンクが<a href>として埋め込まれること ──
def test_call_tinyfish_fetch_embeds_links_when_requested(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = None
    mock_resp.json.return_value = {
        "results": [{
            "final_url": "https://example.com/news",
            "title": "News & <Updates>",
            "text": "本文テキスト",
            "links": ["https://example.com/a", "https://example.com/b?x=1&y=2"],
        }],
    }
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: mock_resp)

    html, final_url, status_code = ac._call_tinyfish_fetch("https://example.com/news", {}, True, want_links=True)

    assert final_url == "https://example.com/news"
    assert status_code == 200
    assert '<a href="https://example.com/a">' in html
    assert '<a href="https://example.com/b?x=1&amp;y=2">' in html
    assert "News &amp; &lt;Updates&gt;" in html  # タイトルがHTMLエスケープされていること


def test_call_tinyfish_fetch_without_links_has_no_anchor_tags(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = None
    mock_resp.json.return_value = {
        "results": [{"final_url": "https://example.com/news", "title": "t", "text": "本文",
                      "links": ["https://example.com/ignored"]}],
    }
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: mock_resp)

    html, final_url, status_code = ac._call_tinyfish_fetch("https://example.com/news", {}, True, want_links=False)

    assert "<a href=" not in html


def test_call_tinyfish_fetch_raises_without_api_key(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "")
    with pytest.raises(RuntimeError, match="APIキーが未設定"):
        ac._call_tinyfish_fetch("https://example.com/", {}, True)


# ─── _call_tinyfish_agent(): SSEストリームのCOMPLETEイベントから本文を取り出す ──
def test_call_tinyfish_agent_parses_complete_event(monkeypatch):
    lines = [
        'data: {"type": "PROGRESS", "message": "working"}',
        'data: {"type": "COMPLETE", "status": "COMPLETED", "result": "抽出された本文"}',
    ]
    cm = MagicMock()
    cm.__enter__.return_value = cm
    cm.raise_for_status.side_effect = None
    cm.iter_lines.return_value = iter(lines)
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: cm)

    html, final_url, status_code = ac._call_tinyfish_agent("https://example.com/", {}, True)

    assert "抽出された本文" in html
    assert final_url == "https://example.com/"


def test_call_tinyfish_agent_raises_on_error_event(monkeypatch):
    lines = ['data: {"type": "ERROR", "message": "failed to navigate"}']
    cm = MagicMock()
    cm.__enter__.return_value = cm
    cm.raise_for_status.side_effect = None
    cm.iter_lines.return_value = iter(lines)
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: cm)

    with pytest.raises(RuntimeError, match="failed to navigate"):
        ac._call_tinyfish_agent("https://example.com/", {}, True)
