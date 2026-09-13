import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import send_state_machine as ssm  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


def _row(row_id="x-1", status="review_required", send_error_message=None):
    return {"id": row_id, "status": status, "send_error_message": send_error_message}


# ─── guarded_transition（低レベルのCASプリミティブ） ─────────────────
def test_guarded_transition_applies_patch_when_guard_matches():
    client = FakeSupabaseClient({"t": [_row()]})
    updated = ssm.guarded_transition(
        client, "t", "id", "x-1", {"status": "eq.review_required"}, {"status": "approved"},
    )
    assert len(updated) == 1
    assert updated[0]["status"] == "approved"
    assert client.tables["t"][0]["status"] == "approved"


def test_guarded_transition_returns_empty_when_guard_does_not_match():
    """guardの値が既に変わっていた（他の遷移と競合した）場合は0件で、
    patchも一切適用されない"""
    client = FakeSupabaseClient({"t": [_row(status="approved")]})
    updated = ssm.guarded_transition(
        client, "t", "id", "x-1", {"status": "eq.review_required"}, {"status": "sending_should_not_apply"},
    )
    assert updated == []
    assert client.tables["t"][0]["status"] == "approved"  # 変化していない


def test_guarded_transition_is_null_guard_matches_none():
    client = FakeSupabaseClient({"t": [_row(send_error_message=None)]})
    updated = ssm.guarded_transition(
        client, "t", "id", "x-1", {"send_error_message": "is.null"}, {"send_error_message": "marker"},
    )
    assert len(updated) == 1
    assert client.tables["t"][0]["send_error_message"] == "marker"


# ─── is_fresh_in_flight_marker ──────────────────────────────────────
def test_fresh_marker_is_recognized_as_in_flight():
    marker = ssm._make_marker(datetime.now(timezone.utc))
    assert ssm.is_fresh_in_flight_marker(marker) is True


def test_stale_marker_is_not_in_flight():
    old = datetime.now(timezone.utc) - ssm.STALE_CLAIM_TIMEOUT - timedelta(minutes=1)
    marker = ssm._make_marker(old)
    assert ssm.is_fresh_in_flight_marker(marker) is False


def test_none_and_plain_error_text_are_not_markers():
    assert ssm.is_fresh_in_flight_marker(None) is False
    assert ssm.is_fresh_in_flight_marker("SMTP connection refused") is False


# ─── SendStateMachine.claim_for_sending ─────────────────────────────
def test_claim_for_sending_succeeds_from_clean_state():
    client = FakeSupabaseClient({"t": [_row()]})
    row = client.tables["t"][0]
    machine = ssm.SendStateMachine(client, "t", "id")

    result = machine.claim_for_sending(
        row, status_field="status", blocked_statuses=("rejected", "sent"),
        extra_patch={"status": "approved"},
    )

    assert result["ok"] is True
    assert result["row"]["status"] == "approved"
    assert client.tables["t"][0]["send_error_message"].startswith("__SENDING__:")
    assert machine.audit_log[-1]["action"] == "claim_for_sending"
    assert machine.audit_log[-1]["ok"] is True


def test_claim_for_sending_refuses_blocked_status_without_touching_row():
    client = FakeSupabaseClient({"t": [_row(status="rejected")]})
    row = client.tables["t"][0]
    machine = ssm.SendStateMachine(client, "t", "id")

    result = machine.claim_for_sending(
        row, status_field="status", blocked_statuses=("rejected", "sent"),
        extra_patch={"status": "approved"},
    )

    assert result["ok"] is False
    assert client.tables["t"][0]["status"] == "rejected"  # 変化していない
    assert client.updated == []  # UPDATEが一度も発行されていない（早期リターン）


def test_claim_for_sending_second_concurrent_call_conflicts():
    """2回連続でapprove_and_send相当のclaimを試みる（二重クリックのシミュレーション）。
    1回目は成功し、同じ（claim前の）行スナップショットを使った2回目は
    conflictとして失敗する＝send_email()相当の処理は1回しか走らない、という
    pmo-009（二重送信）に対する原子性の直接的な検証"""
    client = FakeSupabaseClient({"t": [_row()]})
    row_snapshot = dict(client.tables["t"][0])  # 2回とも同じ「読み取り直後」の状態を使う
    machine = ssm.SendStateMachine(client, "t", "id")

    first = machine.claim_for_sending(
        row_snapshot, status_field="status", blocked_statuses=("rejected", "sent"),
        extra_patch={"status": "approved"},
    )
    second = machine.claim_for_sending(
        row_snapshot, status_field="status", blocked_statuses=("rejected", "sent"),
        extra_patch={"status": "approved"},
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert machine.audit_log[-1]["detail"] == "conflict"


def test_claim_for_sending_refuses_when_fresh_marker_already_present():
    client = FakeSupabaseClient({"t": [_row(send_error_message=ssm._make_marker())]})
    row = client.tables["t"][0]
    machine = ssm.SendStateMachine(client, "t", "id")

    result = machine.claim_for_sending(
        row, status_field="status", blocked_statuses=("rejected", "sent"),
        extra_patch={"status": "approved"},
    )

    assert result["ok"] is False
    assert client.updated == []


def test_claim_for_sending_allows_reclaim_when_marker_is_stale():
    """クラッシュ等でマーカーが残ったままになったケース。STALE_CLAIM_TIMEOUTを
    超えていれば「放棄されたclaim」とみなして再claimできる"""
    old_marker = ssm._make_marker(datetime.now(timezone.utc) - ssm.STALE_CLAIM_TIMEOUT - timedelta(minutes=1))
    client = FakeSupabaseClient({"t": [_row(send_error_message=old_marker)]})
    row = client.tables["t"][0]
    machine = ssm.SendStateMachine(client, "t", "id")

    result = machine.claim_for_sending(
        row, status_field="status", blocked_statuses=("rejected", "sent"),
        extra_patch={"status": "approved"},
    )

    assert result["ok"] is True
    assert client.tables["t"][0]["status"] == "approved"


# ─── SendStateMachine.guard_reject ───────────────────────────────────
def test_guard_reject_succeeds_when_not_blocked():
    client = FakeSupabaseClient({"t": [_row(status="approved")]})
    row = client.tables["t"][0]
    machine = ssm.SendStateMachine(client, "t", "id")

    result = machine.guard_reject(row, status_field="status", blocked_statuses=("sent",),
                                   patch={"status": "rejected"})

    assert result["ok"] is True
    assert client.tables["t"][0]["status"] == "rejected"


def test_guard_reject_refuses_when_already_sent():
    client = FakeSupabaseClient({"t": [_row(status="sent")]})
    row = client.tables["t"][0]
    machine = ssm.SendStateMachine(client, "t", "id")

    result = machine.guard_reject(row, status_field="status", blocked_statuses=("sent",),
                                   patch={"status": "rejected"})

    assert result["ok"] is False
    assert client.tables["t"][0]["status"] == "sent"  # 変化していない
