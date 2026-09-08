"""strategic_question_responses.py の単体テスト。"""
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import strategic_question_responses as sqr  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

RAW_TOKEN = "test-raw-token-value"
TOKEN_HASH = hashlib.sha256(RAW_TOKEN.encode("utf-8")).hexdigest()


def _client(question_status="open", closes_at_offset_days=5):
    closes_at = (datetime.now(timezone.utc) + timedelta(days=closes_at_offset_days)).isoformat()
    return FakeSupabaseClient({
        "sustainability_strategic_questions": [{
            "question_id": "q-1", "question_status": question_status, "closes_at": closes_at,
            "theme": "水", "decision_dimension_key": "cost_vs_sustainability_impact",
            "decision_dimension_label": "対立軸", "title": "t", "question_text": "質問",
        }],
        "sustainability_strategic_question_options": [
            {"option_id": "opt-a", "question_id": "q-1", "option_code": "A", "label": "A案"},
            {"option_id": "opt-b", "question_id": "q-1", "option_code": "B", "label": "B案"},
        ],
        "sustainability_strategic_question_deliveries": [{
            "delivery_id": "d-1", "question_id": "q-1", "recipient_key": "recipient-key-1",
            "response_token_hash": TOKEN_HASH, "send_status": "success",
        }],
        "sustainability_strategic_question_responses": [],
    })


def test_validate_token_success():
    client = _client()
    result = sqr.validate_token(client, "q-1", RAW_TOKEN)
    assert "error" not in result
    assert result["delivery_id"] == "d-1"


def test_validate_token_rejects_wrong_token():
    client = _client()
    result = sqr.validate_token(client, "q-1", "wrong-token")
    assert result["error"] == "invalid_token"


def test_validate_token_rejects_closed_question():
    client = _client(question_status="closed")
    result = sqr.validate_token(client, "q-1", RAW_TOKEN)
    assert result["error"] == "closed"


def test_validate_token_rejects_past_closes_at():
    client = _client(closes_at_offset_days=-1)
    result = sqr.validate_token(client, "q-1", RAW_TOKEN)
    assert result["error"] == "closed"


def test_validate_token_get_marks_opened_without_recording_response():
    client = _client()
    sqr.validate_token(client, "q-1", RAW_TOKEN, is_get=True)
    delivery = client.select("sustainability_strategic_question_deliveries", {"delivery_id": "eq.d-1"})[0]
    assert delivery["opened_response_at"] is not None
    assert client.tables["sustainability_strategic_question_responses"] == []


def test_get_option_or_none_found_and_missing():
    client = _client()
    assert sqr.get_option_or_none(client, "q-1", "A")["option_id"] == "opt-a"
    assert sqr.get_option_or_none(client, "q-1", "Z") is None


def test_record_response_inserts_new_row():
    client = _client()
    delivery = sqr.validate_token(client, "q-1", RAW_TOKEN)
    sqr.record_response(client, delivery, "opt-a", "コメント")
    responses = client.tables["sustainability_strategic_question_responses"]
    assert len(responses) == 1
    assert responses[0]["option_id"] == "opt-a"
    assert responses[0]["respondent_key"] == "recipient-key-1"


def test_record_response_updates_existing_on_resubmit():
    client = _client()
    delivery = sqr.validate_token(client, "q-1", RAW_TOKEN)
    sqr.record_response(client, delivery, "opt-a", "最初のコメント")
    sqr.record_response(client, delivery, "opt-b", "変更後のコメント")
    responses = client.tables["sustainability_strategic_question_responses"]
    assert len(responses) == 1  # 上書きであり新規追加ではない
    assert responses[0]["option_id"] == "opt-b"
    assert responses[0]["comment"] == "変更後のコメント"


def test_aggregate_results_counts_and_rates():
    client = _client()
    client.tables["sustainability_strategic_question_deliveries"].append(
        {"delivery_id": "d-2", "question_id": "q-1", "recipient_key": "recipient-key-2",
         "response_token_hash": "other-hash", "send_status": "success"})
    client.tables["sustainability_strategic_question_responses"].extend([
        {"response_id": "r-1", "question_id": "q-1", "option_id": "opt-a", "respondent_key": "recipient-key-1"},
        {"response_id": "r-2", "question_id": "q-1", "option_id": "opt-a", "respondent_key": "recipient-key-2"},
    ])
    agg = sqr.aggregate_results(client, "q-1")
    assert agg["total_responses"] == 2
    assert agg["delivery_count"] == 2
    assert agg["response_rate"] == 1.0
    by_a = next(o for o in agg["by_option"] if o["option_code"] == "A")
    assert by_a["count"] == 2 and by_a["pct"] == 100.0


def test_aggregate_results_empty_question():
    client = _client()
    agg = sqr.aggregate_results(client, "q-1")
    assert agg["total_responses"] == 0
    assert agg["response_rate"] == 0.0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
