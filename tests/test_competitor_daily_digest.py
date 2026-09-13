import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import competitor_daily_digest as digest  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

CONFIG = {"competitor_monitoring": {"auto_send_confidence_threshold": 0.85}}


def _iso(dt) -> str:
    return dt.isoformat()


def _event(event_id, created_at, *, review_required=False, confidence=0.95, company_id="c-1",
           verification_status="VERIFIED"):
    return {
        "change_event_id": event_id, "company_id": company_id, "record_type": "TARGET",
        "change_type": "SCOPE_EXPANDED", "direction": "STRENGTHENED",
        "summary": "テスト変更", "reasoning_summary": "テスト根拠",
        "confidence": confidence, "review_required": review_required,
        "created_at": created_at, "verification_status": verification_status,
    }


def _company(company_id="c-1", name="A飲料（ダミー）"):
    return {"company_id": company_id, "company_name": name}


def test_collect_today_change_events_filters_by_date():
    now = datetime.now(timezone.utc)
    today_event = _event("e-today", _iso(now))
    yesterday_event = _event("e-yesterday", _iso(now - timedelta(days=1)))
    client = FakeSupabaseClient({
        "competitor_change_events": [today_event, yesterday_event],
    })
    result = digest.collect_today_change_events(client)
    assert [e["change_event_id"] for e in result] == ["e-today"]


def test_build_digest_returns_none_when_no_events_today():
    client = FakeSupabaseClient({"competitor_change_events": [], "competitor_companies": [_company()]})
    assert digest.build_digest(client, CONFIG) is None


def test_build_digest_queues_review_when_not_auto_eligible(monkeypatch):
    now = datetime.now(timezone.utc)
    client = FakeSupabaseClient({
        "competitor_change_events": [_event("e-1", _iso(now), review_required=True)],
        "competitor_companies": [_company()],
        "email_recipients": [],
    })

    called = {}

    def fake_send_email(subject, html_body, config, to_addresses):
        called["sent"] = True
        return {"ok": True, "mode": "preview"}

    monkeypatch.setattr(digest, "send_email", fake_send_email)

    result = digest.build_digest(client, CONFIG)
    assert result["review_status"] == "review_required"
    assert result["auto_send_eligible"] is False
    assert "sent" not in called


def _target_record(record_id, structured_fields, title="CO2削減目標", themes=None):
    return {"record_id": record_id, "title": title, "themes": themes or ["GHG"],
            "structured_fields": structured_fields}


def _machine_verifiable_event(event_id, created_at, *, before_record_id, after_record_id,
                               company_id="c-1", confidence=0.6, verification_status="VERIFIED",
                               summary="テスト用の解釈を含む要約文（自動送信メールには出てはならない）"):
    """Phase 2ゲートを実際に通す想定のイベント。あえて低いconfidence(0.6 < 旧閾値0.85)を
    与えることで、機械検証可能性のみが自動送信可否を決めることを示す"""
    return {
        "change_event_id": event_id, "company_id": company_id, "record_type": "TARGET",
        "change_type": "SUBSTANTIVE_CHANGE", "direction": None,
        "summary": summary, "reasoning_summary": "数値の変化から判断",
        "confidence": confidence, "review_required": False,
        "created_at": created_at, "verification_status": verification_status,
        "before_record_id": before_record_id, "after_record_id": after_record_id,
        "changed_fields": [{"field": "target_value", "before": "30%", "after": "40%"}],
        "llm_raw_output": {"same_entity": True},
        "primary_source_url": "https://example.com/report",
    }


def test_build_digest_auto_sends_when_all_eligible(monkeypatch):
    """Phase 2: 機械検証可能な数値変更（30%→40%）は、confidenceが旧閾値(0.85)未満でも
    自動送信される（confidenceだけで自動送信可否を決めない、という核心原則の確認）。
    かつ、実際に送信されるメール本文にはLLMの要約文(summary)が含まれず、生の事実のみが
    含まれる（自動送信テンプレートはbuild_auto_send_email()、解釈文を含まない）"""
    now = datetime.now(timezone.utc)
    before_record = _target_record("r-before", {"target_value": "30%"})
    after_record = _target_record("r-after", {"target_value": "40%"})
    client = FakeSupabaseClient({
        "competitor_change_events": [_machine_verifiable_event(
            "e-1", _iso(now), before_record_id="r-before", after_record_id="r-after")],
        "competitor_companies": [_company()],
        "competitor_target_records": [before_record, after_record],
        "email_recipients": [],
    })

    sent = {}

    def fake_send_email(subject, html_body, config, to_addresses):
        sent["subject"] = subject
        sent["html_body"] = html_body
        return {"ok": True, "mode": "preview"}

    monkeypatch.setattr(digest, "send_email", fake_send_email)

    result = digest.build_digest(client, CONFIG)
    assert result["auto_send_eligible"] is True
    assert result["review_status"] == "sent"

    saved = client.tables["competitor_daily_alert_digests"][0]
    assert saved["send_status"] == "success"

    # 自動送信メールは機械検証可能な生の事実（30%→40%）のみで、LLMの要約文は含めない
    assert "自動送信メールには出てはならない" not in sent["html_body"]
    assert "30%" in sent["html_body"] and "40%" in sent["html_body"]
    # ダイジェスト自体（レビュー画面・監査用に保存される内容）には従来通り要約文が残る
    assert "自動送信メールには出てはならない" in result["html_body"]


def test_build_digest_queues_for_review_despite_high_confidence_when_not_machine_verifiable(monkeypatch):
    """Phase 2の回帰テスト(要件16): verification_status=VERIFIED・confidence高・
    review_required=Falseという「旧ロジックなら自動送信されていた」条件が揃っていても、
    実際の変更が機械検証可能allowlist外（scope変更）であれば自動送信してはならない。
    LLMの確信度単体を自動送信の許可根拠にしないというPhase 2の核心原則そのものの確認"""
    now = datetime.now(timezone.utc)
    before_record = _target_record("r-before", {"scope": "breweries"})
    after_record = _target_record("r-after", {"scope": "all sites"})
    event = {
        "change_event_id": "e-1", "company_id": "c-1", "record_type": "TARGET",
        "change_type": "SCOPE_EXPANDED", "direction": None,
        "summary": "対象範囲が拡大された", "reasoning_summary": "...",
        "confidence": 0.99, "review_required": False, "created_at": _iso(now),
        "verification_status": "VERIFIED",
        "before_record_id": "r-before", "after_record_id": "r-after",
        "changed_fields": [{"field": "scope", "before": "breweries", "after": "all sites"}],
        "llm_raw_output": {"same_entity": True},
    }
    client = FakeSupabaseClient({
        "competitor_change_events": [event],
        "competitor_companies": [_company()],
        "competitor_target_records": [before_record, after_record],
    })

    called = {}
    monkeypatch.setattr(
        digest, "send_email",
        lambda *a, **k: called.setdefault("sent", True) or {"ok": True, "mode": "preview"})

    result = digest.build_digest(client, CONFIG)
    assert result["auto_send_eligible"] is False
    assert result["review_status"] == "review_required"
    assert "sent" not in called
    # 自動送信対象外でも、イベント自体はレビューキューから削除されない（既存動作の維持）
    assert result["change_event_ids"] == ["e-1"]


def test_build_digest_all_or_nothing_when_mixed_eligibility(monkeypatch):
    """1日のうち1件でも機械検証不能な変更が混在する場合、機械検証可能なイベントだけを
    間引いて自動送信するのではなく、ダイジェスト全体をHuman Reviewへ回す（安全側の設計）。
    どちらのイベントもレビューキューにそのまま残ることを確認する"""
    now = datetime.now(timezone.utc)
    numeric_before = _target_record("r-b1", {"target_value": "30%"})
    numeric_after = _target_record("r-a1", {"target_value": "40%"})
    scope_before = _target_record("r-b2", {"scope": "breweries"})
    scope_after = _target_record("r-a2", {"scope": "all sites"})
    events = [
        _machine_verifiable_event("e-1", _iso(now), before_record_id="r-b1", after_record_id="r-a1"),
        {
            "change_event_id": "e-2", "company_id": "c-1", "record_type": "TARGET",
            "change_type": "SCOPE_EXPANDED", "direction": None,
            "summary": "...", "reasoning_summary": "...",
            "confidence": 0.9, "review_required": False, "created_at": _iso(now),
            "verification_status": "VERIFIED",
            "before_record_id": "r-b2", "after_record_id": "r-a2",
            "changed_fields": [{"field": "scope", "before": "breweries", "after": "all sites"}],
            "llm_raw_output": {"same_entity": True},
        },
    ]
    client = FakeSupabaseClient({
        "competitor_change_events": events,
        "competitor_companies": [_company()],
        "competitor_target_records": [numeric_before, numeric_after, scope_before, scope_after],
    })
    called = {}
    monkeypatch.setattr(
        digest, "send_email",
        lambda *a, **k: called.setdefault("sent", True) or {"ok": True, "mode": "preview"})

    result = digest.build_digest(client, CONFIG)
    assert result["auto_send_eligible"] is False
    assert "sent" not in called
    assert set(result["change_event_ids"]) == {"e-1", "e-2"}


def test_build_digest_skips_rebuild_when_already_sent(monkeypatch):
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    existing = {
        "digest_id": "d-1", "digest_date": today, "review_status": "sent",
        "change_event_ids": ["e-old"], "subject": "既存", "html_body": "<p>既存</p>",
        "auto_send_eligible": True,
    }
    client = FakeSupabaseClient({
        "competitor_change_events": [_event("e-new", _iso(now))],
        "competitor_companies": [_company()],
        "competitor_daily_alert_digests": [existing],
    })

    def fail_send_email(*args, **kwargs):
        raise AssertionError("送信済みの日は再送してはならない")

    monkeypatch.setattr(digest, "send_email", fail_send_email)

    result = digest.build_digest(client, CONFIG)
    assert result["digest_id"] == "d-1"
    assert result["change_event_ids"] == ["e-old"]


def _digest_row(digest_id="d-1", review_status="review_required"):
    return {
        "digest_id": digest_id, "digest_date": "2026-09-13", "change_event_ids": ["e-1"],
        "subject": "件名", "html_body": "<p>本文</p>", "auto_send_eligible": False,
        "review_status": review_status, "send_mode": None, "recipients": [], "send_status": None,
    }


def test_approve_and_send_success_marks_sent(monkeypatch):
    client = FakeSupabaseClient({"competitor_daily_alert_digests": [_digest_row()]})
    monkeypatch.setattr(digest, "send_email",
                         lambda subject, html_body, config, to_addresses: {"ok": True, "mode": "preview"})
    monkeypatch.setattr(digest, "list_recipients", lambda client, test_mode=False: [])

    result = digest.approve_and_send(client, CONFIG, "d-1", reviewer_id="reviewer-a")

    assert result["ok"] is True
    assert result["review_status"] == "sent"
    row = client.tables["competitor_daily_alert_digests"][0]
    assert row["review_status"] == "sent"
    assert row["send_status"] == "success"
    assert row["send_error_message"] is None


def test_approve_and_send_refuses_rejected_digest():
    client = FakeSupabaseClient({"competitor_daily_alert_digests": [_digest_row(review_status="rejected")]})
    result = digest.approve_and_send(client, CONFIG, "d-1", reviewer_id="reviewer-a")
    assert result["ok"] is False
    row = client.tables["competitor_daily_alert_digests"][0]
    assert row["review_status"] == "rejected"  # 変化していない


def test_approve_and_send_second_concurrent_call_does_not_double_send(monkeypatch):
    """同じdigestに対してapprove_and_send()が2回連続で呼ばれても（二重クリック相当）、
    send_email()は1回しか呼ばれない（pmo-009の直接的な回帰テスト）"""
    client = FakeSupabaseClient({"competitor_daily_alert_digests": [_digest_row()]})
    send_count = {"n": 0}

    def counting_send_email(subject, html_body, config, to_addresses):
        send_count["n"] += 1
        return {"ok": True, "mode": "preview"}

    monkeypatch.setattr(digest, "send_email", counting_send_email)
    monkeypatch.setattr(digest, "list_recipients", lambda client, test_mode=False: [])

    # 2回とも「承認ボタンを押した瞬間」に相当する呼び出し（DBの最新行はまだ変わっていない前提）
    first = digest.approve_and_send(client, CONFIG, "d-1", reviewer_id="reviewer-a")
    row_after_first = client.tables["competitor_daily_alert_digests"][0]
    assert first["ok"] is True
    assert send_count["n"] == 1
    # 1回目が既に'sent'まで進めているため、2回目はブロック（早期リターン、send_email再呼び出しなし）
    second = digest.approve_and_send(client, CONFIG, "d-1", reviewer_id="reviewer-b")
    assert second["ok"] is False
    assert send_count["n"] == 1
    assert row_after_first["review_status"] == "sent"


def test_reject_digest_blocks_when_already_sent():
    client = FakeSupabaseClient({"competitor_daily_alert_digests": [_digest_row(review_status="sent")]})
    result = digest.reject_digest(client, "d-1", reviewer_id="reviewer-a")
    assert result["ok"] is False
    row = client.tables["competitor_daily_alert_digests"][0]
    assert row["review_status"] == "sent"  # 変化していない


def test_reject_digest_succeeds_when_review_required():
    client = FakeSupabaseClient({"competitor_daily_alert_digests": [_digest_row()]})
    result = digest.reject_digest(client, "d-1", reviewer_id="reviewer-a")
    assert result["ok"] is True
    row = client.tables["competitor_daily_alert_digests"][0]
    assert row["review_status"] == "rejected"
