"""
geo_intelligence_client.GeoIntelligenceClient の単体テスト（HTTP層、DB非依存）。
requests.post を unittest.mock.patch で直接パッチする
（本リポジトリにHTTPモック専用ライブラリの前例が無いため、標準ライブラリのみで完結させる）。
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import geo_intelligence_client  # noqa: E402
from geo_intelligence_client import GeoIntelligenceClient  # noqa: E402
from geo_intelligence_schema import build_request  # noqa: E402
from tests._geo_intelligence_fixtures import (  # noqa: E402
    TEST_GEO_CONFIG, mock_response, sufficient_body, partial_body, insufficient_body,
)


def _client(max_retries=2):
    config = {"geo_intelligence": {**TEST_GEO_CONFIG["geo_intelligence"], "max_retries": max_retries}}
    return GeoIntelligenceClient(config, proxies={}, verify=True)


def _request(request_id="req-1"):
    return build_request(request_type="user_question", question="台湾情勢の影響は？", request_id=request_id)


# --- Test1: 正常通信（sufficient） -------------------------------------------------
def test_query_sufficient_returns_success():
    client = _client()
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(200, sufficient_body("req-1"))):
        result = client.query(request)

    assert result["success"] is True
    assert result["status"] == "success"
    assert result["remote_status"] == "completed"
    assert result["response"].request_id == "req-1"
    assert result["response"].knowledge_sufficiency == "sufficient"
    assert result["response"].confidence == "high"  # 文字列。0〜1の数値ではない
    assert result["local_request_id"] == "req-1"
    assert result["latency_ms"] >= 0


# --- Test2: partial（値を改変せず保持） ----------------------------------------------
def test_query_partial_preserves_values_unmodified():
    client = _client()
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(200, partial_body("req-1"))):
        result = client.query(request)

    assert result["success"] is True
    assert result["remote_status"] == "partial"
    assert result["response"].knowledge_sufficiency == "partial"
    gap = result["response"].knowledge_gap
    assert gap["gap_id"] == "GAP-1"
    assert gap["description"] == "直近1週間の一次情報が不足"


# --- Test3: insufficient（エラー扱いにしない、勝手に補完しない） ------------------------
def test_query_insufficient_is_not_treated_as_error_and_not_backfilled():
    client = _client()
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(200, insufficient_body("req-1"))):
        result = client.query(request)

    assert result["success"] is True
    assert result["status"] == "success"  # 通信としては成功
    response = result["response"]
    assert response.knowledge_sufficiency == "insufficient"
    assert response.references is None  # 架空のreferenceを生成していない
    assert response.geo_assessment is None  # 値を補完していない
    assert response.knowledge_gap["exists"] is True
    assert response.knowledge_gap["gap_id"] == "GAP-2"


# --- Test4: Timeout ---------------------------------------------------------------
def test_query_timeout_returns_structured_failure_without_raising():
    client = _client(max_retries=0)
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               side_effect=requests.exceptions.Timeout("timed out")):
        result = client.query(request)  # 例外を投げないこと自体がアサーション

    assert result["success"] is False
    assert result["status"] == "unavailable"
    assert result["error_type"] == "timeout"
    assert result["response"] is None


# --- Test5: 503連続 → Retry ---------------------------------------------------------
def test_query_503_retries_up_to_max_then_unavailable(monkeypatch):
    monkeypatch.setattr(geo_intelligence_client.time, "sleep", lambda s: None)
    client = _client(max_retries=2)
    request = _request("req-1")
    responses = [mock_response(503), mock_response(503), mock_response(503)]
    with patch("geo_intelligence_client.requests.post", side_effect=responses) as mock_post:
        result = client.query(request)

    assert mock_post.call_count == 3  # 初回 + リトライ2回
    assert result["success"] is False
    assert result["status"] == "unavailable"
    assert result["error_type"] == "http_error"
    assert result["http_status"] == 503


# --- Test6: 400 → リトライしない -----------------------------------------------------
def test_query_400_does_not_retry():
    client = _client(max_retries=2)
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(400, text="Bad Request")) as mock_post:
        result = client.query(request)

    assert mock_post.call_count == 1
    assert result["success"] is False
    assert result["status"] == "failed"  # unavailableではない


# --- Test7: Invalid Response ---------------------------------------------------------
def test_query_invalid_response_missing_required_fields():
    client = _client()
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(200, {"foo": "bar"})):
        result = client.query(request)

    assert result["success"] is False
    assert result["status"] == "invalid_response"
    assert result["error_type"] == "schema_validation"
    assert result["response"] is None  # 壊れたResponseを下流(response)へ渡さない


def test_query_invalid_response_unknown_knowledge_sufficiency_value():
    client = _client()
    request = _request("req-1")
    body = sufficient_body("req-1")
    body["knowledge_sufficiency"] = "maybe"  # enum外
    with patch("geo_intelligence_client.requests.post", return_value=mock_response(200, body)):
        result = client.query(request)

    assert result["status"] == "invalid_response"


def test_query_invalid_response_request_id_mismatch():
    """送信したrequest_idと異なるrequest_idが返ってきた場合はinvalid_response扱いにする
    （別のリクエストへの応答が紛れ込んでいる可能性があり、正常応答として扱えないため）。"""
    client = _client()
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(200, sufficient_body("req-DIFFERENT"))):
        result = client.query(request)

    assert result["success"] is False
    assert result["status"] == "invalid_response"
    assert result["error_type"] == "schema_validation"


# --- Test8: Secret ------------------------------------------------------------------
def test_query_sends_api_key_via_x_api_key_header_not_leaked_in_result():
    client = _client()
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post",
               return_value=mock_response(200, sufficient_body("req-1"))) as mock_post:
        result = client.query(request)

    sent_headers = mock_post.call_args.kwargs["headers"]
    assert sent_headers["X-API-Key"] == "sk-test-secret-XYZ"
    # 戻り値(GeoCallResult)自体にAPIキーが含まれないこと
    import json
    assert "sk-test-secret-XYZ" not in json.dumps(result, default=str)


def test_disabled_config_returns_disabled_without_calling_requests():
    config = {"geo_intelligence": {"enabled": False}}
    client = GeoIntelligenceClient(config, proxies={}, verify=True)
    request = _request("req-1")
    with patch("geo_intelligence_client.requests.post") as mock_post:
        result = client.query(request)

    mock_post.assert_not_called()
    assert result["status"] == "disabled"


def test_missing_base_url_returns_not_configured_failure():
    config = {"geo_intelligence": {"enabled": True, "base_url": ""}}
    client = GeoIntelligenceClient(config, proxies={}, verify=True)
    request = _request("req-1")
    result = client.query(request)

    assert result["status"] == "failed"
    assert result["error_type"] == "not_configured"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
