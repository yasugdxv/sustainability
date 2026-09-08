"""
geo_intelligence_service.query_geo_intelligence() の単体テスト。
FakeSupabaseClient（db_client）と実GeoIntelligenceClient（requests.postのみモック）を
明示的に別々に注入し、DI分離（geo_client / db_client）とDB監査ログ保存を検証する。
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import geo_intelligence_client  # noqa: E402
from geo_intelligence_client import GeoIntelligenceClient  # noqa: E402
from geo_intelligence_service import query_geo_intelligence  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402
from tests._geo_intelligence_fixtures import (  # noqa: E402
    TEST_GEO_CONFIG, mock_response, sufficient_body, partial_body, insufficient_body,
)


def _geo_client(max_retries=2):
    config = {"geo_intelligence": {**TEST_GEO_CONFIG["geo_intelligence"], "max_retries": max_retries}}
    return GeoIntelligenceClient(config, proxies={}, verify=True)


def _echoing_response(body_builder, status_code=200):
    """query_geo_intelligence()はrequest_idを内部で自動生成するため、テストでは
    固定request_idのモックを使えない。送信されたpayloadのrequest_idをそのまま
    レスポンスへ反映するside_effectを返す（Geo側の実際のエコーバック挙動を模倣）。"""
    def _respond(*args, **kwargs):
        sent_payload = kwargs.get("json") or {}
        body = body_builder(sent_payload.get("request_id"))
        return mock_response(status_code, body)
    return _respond


def _call(geo_client=None, db_client=None, **kwargs):
    db_client = db_client or FakeSupabaseClient()
    geo_client = geo_client or _geo_client()
    kwargs.setdefault("request_type", "user_question")
    kwargs.setdefault("question", "台湾情勢が半導体供給網に与える影響は？")
    return db_client, query_geo_intelligence(geo_client=geo_client, db_client=db_client, **kwargs)


def _last_row(db_client):
    return db_client.inserted["external_intelligence_calls"][-1]


# --- Test1: 正常通信（sufficient） -------------------------------------------------
def test_sufficient_response_recorded_to_db():
    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(sufficient_body)):
        db_client, result = _call(geo_client=geo_client, source_type="article", source_id="art-123")

    assert result["success"] is True
    row = _last_row(db_client)
    assert row["status"] == "success"
    assert row["remote_status"] == "completed"
    assert row["knowledge_sufficiency"] == "sufficient"
    assert row["confidence"] == "high"
    assert row["source_type"] == "article"
    assert row["source_id"] == "art-123"
    assert row["local_request_id"] == result["local_request_id"]
    assert row["remote_request_id"] == result["local_request_id"]
    assert row["latency_ms"] >= 0


# --- Test2: partial ----------------------------------------------------------------
def test_partial_response_values_preserved_in_db():
    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(partial_body)):
        db_client, result = _call(geo_client=geo_client)

    row = _last_row(db_client)
    assert row["knowledge_sufficiency"] == "partial"
    assert row["response_payload"]["knowledge_gap"]["gap_id"] == "GAP-1"


# --- Test3: insufficient（追加Gapを生成しない） --------------------------------------
def test_insufficient_response_not_treated_as_error_no_extra_gap_generated():
    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(insufficient_body)):
        db_client, result = _call(geo_client=geo_client)

    assert result["success"] is True
    row = _last_row(db_client)
    assert row["status"] == "success"
    assert row["knowledge_sufficiency"] == "insufficient"
    # Client/Serviceのどちらも新しいgapを作らず、Geoが返した値のみが保存されている
    assert row["response_payload"]["knowledge_gap"]["gap_id"] == "GAP-2"
    assert row["response_payload"]["references"] is None


# --- Test4: Timeout ------------------------------------------------------------------
def test_timeout_recorded_and_does_not_raise():
    geo_client = _geo_client(max_retries=0)
    with patch("geo_intelligence_client.requests.post",
               side_effect=requests.exceptions.Timeout("timed out")):
        db_client, result = _call(geo_client=geo_client)  # 例外を投げないこと自体がアサーション

    assert result["success"] is False
    row = _last_row(db_client)
    assert row["status"] == "unavailable"
    assert row["error_type"] == "timeout"
    assert row["completed_at"] is not None


# --- Test5: 503 + Retry ---------------------------------------------------------------
def test_503_retries_then_records_unavailable(monkeypatch):
    monkeypatch.setattr(geo_intelligence_client.time, "sleep", lambda s: None)
    geo_client = _geo_client(max_retries=2)
    responses = [mock_response(503), mock_response(503), mock_response(503)]
    with patch("geo_intelligence_client.requests.post", side_effect=responses) as mock_post:
        db_client, result = _call(geo_client=geo_client)

    assert mock_post.call_count == 3
    row = _last_row(db_client)
    assert row["status"] == "unavailable"
    assert row["http_status_code"] == 503


# --- Test6: 400 → リトライしない --------------------------------------------------------
def test_400_not_retried_and_recorded_as_failed():
    geo_client = _geo_client(max_retries=2)
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(400, text="Bad Request")) as mock_post:
        db_client, result = _call(geo_client=geo_client)

    assert mock_post.call_count == 1
    row = _last_row(db_client)
    assert row["status"] == "failed"


# --- Test7: Invalid Response -----------------------------------------------------------
def test_invalid_response_not_passed_downstream():
    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(200, {"foo": "bar"})):
        db_client, result = _call(geo_client=geo_client)

    assert result["response"] is None
    row = _last_row(db_client)
    assert row["status"] == "invalid_response"
    assert row["error_type"] == "schema_validation"


# --- Test8: Secret（DB・標準出力いずれにも含まれない） -----------------------------------
def test_api_key_not_leaked_to_db_or_stdout(capsys):
    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(sufficient_body)):
        db_client, result = _call(geo_client=geo_client)

    row = _last_row(db_client)
    dumped = json.dumps(row, default=str)
    assert "sk-test-secret-XYZ" not in dumped
    assert "Authorization" not in row.get("request_payload", {})
    assert "api_key" not in row.get("request_payload", {})

    captured = capsys.readouterr()
    assert "sk-test-secret-XYZ" not in captured.out
    assert "sk-test-secret-XYZ" not in captured.err


def test_scrub_masks_sensitive_keys_directly():
    from geo_intelligence_service import _scrub
    scrubbed = _scrub({"foo": "bar", "Authorization": "Bearer xxx", "nested": {"api_key": "secret"}})
    assert scrubbed["Authorization"] == "***REDACTED***"
    assert scrubbed["nested"]["api_key"] == "***REDACTED***"
    assert scrubbed["foo"] == "bar"


# --- DI分離: geo_clientとdb_clientが独立していること ------------------------------------
def test_geo_client_and_db_client_are_independently_injectable():
    """geo_clientの障害はdb_client実装に一切依存せず、db_clientの実装差し替えのみで
    Geo呼び出しロジック（GeoIntelligenceClient）に影響が無いことを確認する。"""
    geo_client_a = _geo_client()
    db_client_1 = FakeSupabaseClient()
    db_client_2 = FakeSupabaseClient()

    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(sufficient_body)):
        result1 = query_geo_intelligence(geo_client=geo_client_a, db_client=db_client_1,
                                          request_type="user_question", question="Q1")
        result2 = query_geo_intelligence(geo_client=geo_client_a, db_client=db_client_2,
                                          request_type="user_question", question="Q2")

    # 同一geo_clientを使い回しても、DB書き込み先(db_client)は完全に独立している
    assert len(db_client_1.inserted["external_intelligence_calls"]) == 1
    assert len(db_client_2.inserted["external_intelligence_calls"]) == 1
    assert result1["success"] is True and result2["success"] is True


def test_db_write_failure_does_not_affect_geo_call_result():
    """DB書き込み失敗（db_client異常）が、Geo呼び出し自体の戻り値に影響しないこと。"""
    class BrokenDbClient:
        def insert(self, *a, **kw):
            raise RuntimeError("DB接続エラー")

    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(sufficient_body)):
        result = query_geo_intelligence(geo_client=geo_client, db_client=BrokenDbClient(),
                                         request_type="user_question", question="Q1")

    assert result["success"] is True
    assert result["response"].knowledge_sufficiency == "sufficient"


# --- Phase B: request_fingerprint_hash ------------------------------------------------
def test_request_fingerprint_hash_stored_when_provided():
    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(sufficient_body)):
        db_client, result = _call(geo_client=geo_client, request_fingerprint_hash="fp-abc")
    row = _last_row(db_client)
    assert row["request_fingerprint_hash"] == "fp-abc"


def test_request_fingerprint_hash_defaults_to_none_when_omitted():
    """weekly呼び出し相当（fingerprintを渡さない）でも従来通り動作し、列はNULLのままであること"""
    geo_client = _geo_client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_response(sufficient_body)):
        db_client, result = _call(geo_client=geo_client)
    row = _last_row(db_client)
    assert row["request_fingerprint_hash"] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
