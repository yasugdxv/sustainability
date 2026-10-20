"""TinyFish Agentに、サービス側の実行上限(agent_config)を渡す修正のテスト（2026-10-20追加）

根本原因: TinyFishの仕様で agent_config.max_duration_seconds のデフォルトは「無制限」、max_steps は150。
従来は両方を指定せずに呼んでおり、ブロックされたページでAgentが延々と操作を試し続けた
（実測: 失敗は約512秒）。クライアント側で待つのをやめるだけでは、サーバー側の実行・課金が続く恐れがある。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402


class _FakeStream:
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


OK_RESULT = {"status": "success", "title": "題", "body": "これは記事の本文です。" * 30}


def _ev(**kw):
    return "data: " + json.dumps(kw, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "TINYFISH_AGENT_TIMEOUT", 240)
    monkeypatch.setattr(ac, "TINYFISH_AGENT_MAX_DURATION_SEC", 180)
    monkeypatch.setattr(ac, "TINYFISH_AGENT_MAX_STEPS", 0)       # 既定: ベータ機能のため指定しない


class _Recorder:
    """requests.postを記録するフェイク。取消(/cancel)の呼び出しと、本体の呼び出しを区別して持つ"""

    def __init__(self, lines):
        self.lines, self.calls, self.cancels = lines, [], []

    def __call__(self, url, *a, **k):
        if "/cancel" in url:
            self.cancels.append(url)
            return _FakeStream([])
        self.calls.append((url, k))
        return _FakeStream(self.lines)


def test_request_passes_the_agents_own_limits_to_the_service(monkeypatch):
    rec = _Recorder([_ev(type="STARTED", run_id="r1"), _ev(type="COMPLETE", status="COMPLETED", result=OK_RESULT)])
    monkeypatch.setattr(ac.requests, "post", rec)

    ac._call_tinyfish_agent("https://example.com/a", {}, True)

    body = rec.calls[0][1]["json"]
    assert body["url"] == "https://example.com/a"
    assert body["agent_config"] == {"max_duration_seconds": 180}     # 「無制限」のまま呼ばない。max_stepsはベータなので送らない


def test_max_steps_is_sent_only_when_explicitly_configured(monkeypatch):
    """max_stepsはベータ機能（未登録のアカウントでは403）。ベータ登録後に環境変数で指定したときだけ送る"""
    monkeypatch.setattr(ac, "TINYFISH_AGENT_MAX_STEPS", 60)
    rec = _Recorder([_ev(type="COMPLETE", status="COMPLETED", result=OK_RESULT)])
    monkeypatch.setattr(ac.requests, "post", rec)

    ac._call_tinyfish_agent("https://example.com/a", {}, True)

    assert rec.calls[0][1]["json"]["agent_config"] == {"max_duration_seconds": 180, "max_steps": 60}


def test_defaults_are_bounded_and_the_client_guard_is_longer_than_the_service_limit():
    assert ac.TINYFISH_AGENT_MAX_DURATION_SEC > 0 and ac.TINYFISH_AGENT_MAX_STEPS >= 0   # 0=未指定(ベータ機能)
    assert ac.TINYFISH_AGENT_TIMEOUT >= ac.TINYFISH_AGENT_MAX_DURATION_SEC       # 保険は、サービス側の上限より後に効く


def test_goal_not_achieved_is_an_error_with_the_reason_not_a_fake_article(monkeypatch):
    """仕様: COMPLETEは「インフラが動いた」ことしか保証しない。目標未達は result={"status":"failure",...} で来る。
    従来はこのJSON文字列を、記事本文として扱っていた"""
    monkeypatch.setattr(ac.requests, "post", _Recorder([
        _ev(type="COMPLETE", status="COMPLETED", result={"status": "failure", "reason": "CAPTCHAで先へ進めなかった"})]))

    with pytest.raises(RuntimeError, match="目標未達.*CAPTCHA"):
        ac._call_tinyfish_agent("https://example.com", {}, True)


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_non_completed_status_is_an_error(monkeypatch, status):
    monkeypatch.setattr(ac.requests, "post", _Recorder([_ev(type="COMPLETE", status=status, result=None)]))
    with pytest.raises(RuntimeError, match=status):
        ac._call_tinyfish_agent("https://example.com", {}, True)


def test_normal_success_does_not_cancel(monkeypatch):
    rec = _Recorder([_ev(type="STARTED", run_id="r1"), _ev(type="PROGRESS"),
                     _ev(type="COMPLETE", status="COMPLETED", result=OK_RESULT)])
    monkeypatch.setattr(ac.requests, "post", rec)

    html, _, status = ac._call_tinyfish_agent("https://example.com/a", {}, True)

    assert "これは記事の本文です。" in html and status == 200 and rec.cancels == []


def test_client_side_timeout_also_cancels_the_run_on_the_service(monkeypatch):
    """こちらが待つのをやめても、サーバー側の実行(と課金)が続かないよう、取り消す"""
    def endless():
        yield _ev(type="STARTED", run_id="run-123")
        while True:
            yield _ev(type="PROGRESS")

    rec = _Recorder(None)
    rec.lines = endless()
    monkeypatch.setattr(ac.requests, "post", rec)
    now = [0.0]
    monkeypatch.setattr(ac.time, "monotonic", lambda: now.__setitem__(0, now[0] + 100) or now[0])

    with pytest.raises(TimeoutError):
        ac._call_tinyfish_agent("https://example.com", {}, True)

    assert rec.cancels == ["https://agent.tinyfish.ai/v1/runs/run-123/cancel"]


def test_error_event_cancels_and_a_failing_cancel_does_not_hide_the_original_error(monkeypatch):
    calls = {"n": 0}

    def post(url, *a, **k):
        if "/cancel" in url:
            raise ConnectionError("cancel失敗")
        calls["n"] += 1
        return _FakeStream([_ev(type="STARTED", run_id="r9"), _ev(type="ERROR", message="blocked")])

    monkeypatch.setattr(ac.requests, "post", post)

    with pytest.raises(RuntimeError, match="blocked"):                    # 元のエラーが、そのまま上がる
        ac._call_tinyfish_agent("https://example.com", {}, True)


def test_no_cancel_without_a_run_id(monkeypatch):
    rec = _Recorder([_ev(type="ERROR", message="x")])                    # STARTEDが来る前に失敗
    monkeypatch.setattr(ac.requests, "post", rec)
    with pytest.raises(RuntimeError):
        ac._call_tinyfish_agent("https://example.com", {}, True)
    assert rec.cancels == []
