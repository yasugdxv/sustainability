import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import competitor_daily_digest as digest  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

CONFIG = {"competitor_monitoring": {"auto_send_confidence_threshold": 0.85}}


def _iso(dt) -> str:
    return dt.isoformat()


def _event(event_id, created_at, *, review_required=False, confidence=0.95, company_id="c-1"):
    return {
        "change_event_id": event_id, "company_id": company_id, "record_type": "TARGET",
        "change_type": "SCOPE_EXPANDED", "direction": "STRENGTHENED",
        "summary": "テスト変更", "reasoning_summary": "テスト根拠",
        "confidence": confidence, "review_required": review_required,
        "created_at": created_at,
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
        "competitor_recipients": [],
    })

    called = {}

    def fake_send_email(subject, html_body, config):
        called["sent"] = True
        return {"ok": True, "mode": "preview"}

    monkeypatch.setattr(digest, "send_email", fake_send_email)

    result = digest.build_digest(client, CONFIG)
    assert result["review_status"] == "review_required"
    assert result["auto_send_eligible"] is False
    assert "sent" not in called


def test_build_digest_auto_sends_when_all_eligible(monkeypatch):
    now = datetime.now(timezone.utc)
    client = FakeSupabaseClient({
        "competitor_change_events": [_event("e-1", _iso(now), confidence=0.95)],
        "competitor_companies": [_company()],
        "competitor_recipients": [],
    })

    monkeypatch.setattr(digest, "send_email", lambda subject, html_body, config: {"ok": True, "mode": "preview"})

    result = digest.build_digest(client, CONFIG)
    assert result["auto_send_eligible"] is True
    assert result["review_status"] == "sent"

    saved = client.tables["competitor_daily_alert_digests"][0]
    assert saved["send_status"] == "success"


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
