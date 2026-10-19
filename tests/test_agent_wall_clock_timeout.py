"""TinyFish Agent呼び出しの、全体の所要時間の上限のテスト（2026-10-19追加）

背景: requestsのtimeout（stream=True）は「次のデータが来るまでの待ち」の上限であり、全体の上限ではない。
Agentが途中経過のイベントを送り続けて完了しないと、呼び出しが終わらず、本番の手動実行が予算(2時間)を
45分以上超えて止まらなかった。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402


class _FakeStream:
    """with文で使える、ストリーミング応答のフェイク。linesを順に返す（ジェネレーターも可）"""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


def _event(**kw):
    return "data: " + json.dumps(kw, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "TINYFISH_AGENT_TIMEOUT", 600)


def test_endless_progress_events_are_cut_off_by_the_wall_clock_limit(monkeypatch):
    """完了イベントが来ず、途中経過だけが延々と届く場合でも、上限を超えたら打ち切る"""
    def endless():
        while True:
            yield _event(type="PROGRESS", message="working")
            yield ""                                    # 空行（ハートビート）も、上限の判定を妨げない

    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _FakeStream(endless()))
    now = [0.0]
    monkeypatch.setattr(ac.time, "monotonic", lambda: now.__setitem__(0, now[0] + 100) or now[0])  # 呼ぶたびに100秒進む

    with pytest.raises(TimeoutError) as e:
        ac._call_tinyfish_agent("https://example.com", {}, True)

    assert "600秒以内に完了しませんでした" in str(e.value)


def test_the_limit_follows_the_configured_timeout(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_TIMEOUT", 180)
    monkeypatch.setattr(ac.requests, "post",
                        lambda *a, **k: _FakeStream(iter([_event(type="PROGRESS")] * 50 + [_event(type="COMPLETE", status="COMPLETED", result="本文")])))
    now = [0.0]
    monkeypatch.setattr(ac.time, "monotonic", lambda: now.__setitem__(0, now[0] + 10) or now[0])  # 10秒ずつ進む → 約18回で180秒超

    with pytest.raises(TimeoutError) as e:
        ac._call_tinyfish_agent("https://example.com", {}, True)

    assert "180秒以内" in str(e.value)


def test_normal_completion_still_works_and_returns_the_text(monkeypatch):
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _FakeStream(iter([
        "", _event(type="PROGRESS", message="x"), _event(type="COMPLETE", status="COMPLETED", result="記事の本文です"),
        _event(type="PROGRESS", message="完了後は読まない")])))

    html, final_url, status = ac._call_tinyfish_agent("https://example.com/a", {}, True)

    assert "記事の本文です" in html and final_url == "https://example.com/a" and status == 200


def test_error_event_still_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _FakeStream(iter([_event(type="ERROR", message="blocked")])))
    with pytest.raises(RuntimeError, match="blocked"):
        ac._call_tinyfish_agent("https://example.com", {}, True)


def test_request_uses_a_connect_and_read_gap_timeout_not_the_whole_duration(monkeypatch):
    seen = {}

    def fake_post(*a, **k):
        seen.update(k)
        return _FakeStream(iter([_event(type="COMPLETE", status="COMPLETED", result="ok")]))

    monkeypatch.setattr(ac.requests, "post", fake_post)

    ac._call_tinyfish_agent("https://example.com", {}, True)

    assert seen["timeout"] == (10, 180) and seen["stream"] is True      # 全体の上限はtimeoutでは掛からない


def test_read_gap_never_exceeds_the_overall_limit(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_TIMEOUT", 60)
    seen = {}
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: seen.update(k) or _FakeStream(iter([_event(type="COMPLETE", result="ok")])))
    ac._call_tinyfish_agent("https://example.com", {}, True)
    assert seen["timeout"] == (10, 60)
