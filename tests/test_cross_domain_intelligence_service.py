"""
cross_domain_intelligence_service.py（Phase B: Cross-domain Intelligence Gateway）の単体テスト。
既存のtests/_fakes.py（FakeSupabaseClient）とtests/_geo_intelligence_fixtures.py
（Geo Response実例）をそのまま再利用する。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import cross_domain_intelligence_service as gw  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402
from tests._geo_intelligence_fixtures import sufficient_body  # noqa: E402


def _config(**overrides):
    chat = {"enabled": True, "reuse_enabled": True, "reuse_max_age_hours": 24,
            "reuse_max_age_hours_high_freshness": 2}
    chat.update(overrides.pop("chat", {}))
    search = {"enabled": True}
    search.update(overrides.pop("search", {}))
    geo = {"chat": chat, "search": search}
    geo.update(overrides)
    return {"geo_intelligence": geo}


# ─── _is_geo_domain_enabled() ------------------------------------------------------
def test_is_geo_domain_enabled_parent_false_forces_both_off():
    config = _config(enabled=False)
    assert gw._is_geo_domain_enabled(config, "chat") is False
    assert gw._is_geo_domain_enabled(config, "search") is False


def test_is_geo_domain_enabled_parent_true_child_mixed():
    """親がtrueでも子を個別評価すること（chat ON / search OFF になり得る）"""
    config = _config(enabled=True, chat={"enabled": True}, search={"enabled": False})
    assert gw._is_geo_domain_enabled(config, "chat") is True
    assert gw._is_geo_domain_enabled(config, "search") is False


def test_is_geo_domain_enabled_child_explicit_false_ignores_env(monkeypatch):
    """子が明示的にfalseなら、環境変数がtrue相当でも上書きされないこと"""
    monkeypatch.setenv("SUSTAINABILITY_SEARCH_GEO_ENABLED", "true")
    config = _config(search={"enabled": False})
    assert gw._is_geo_domain_enabled(config, "search") is False


def test_is_geo_domain_enabled_child_unset_follows_env(monkeypatch):
    monkeypatch.setenv("SUSTAINABILITY_SEARCH_GEO_ENABLED", "true")
    config = {"geo_intelligence": {}}
    assert gw._is_geo_domain_enabled(config, "search") is True
    monkeypatch.setenv("SUSTAINABILITY_SEARCH_GEO_ENABLED", "")
    assert gw._is_geo_domain_enabled(config, "search") is False


# ─── find_exact_cache() -------------------------------------------------------------
def _cached_row(fingerprint="hash-1", requested_at="2026-09-03T10:00:00+00:00"):
    return {"id": "call-1", "target_service": "geo_intelligence", "request_type": "user_question",
            "status": "success", "request_fingerprint_hash": fingerprint,
            "requested_at": requested_at, "response_payload": sufficient_body("req-1")}


def test_find_exact_cache_kill_switch_off_returns_none_without_query():
    client = FakeSupabaseClient({"external_intelligence_calls": [_cached_row()]})
    client.select = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not query when disabled"))
    result = gw.find_exact_cache(client, _config(chat={"enabled": False}), domain="geopolitics",
                                  request_type="user_question", request_fingerprint_hash="hash-1",
                                  capability="chat")
    assert result is None


def test_find_exact_cache_hit_within_window():
    client = FakeSupabaseClient({"external_intelligence_calls": [_cached_row()]})
    now = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    result = gw.find_exact_cache(client, _config(), domain="geopolitics", request_type="user_question",
                                  request_fingerprint_hash="hash-1", capability="chat", now=now)
    assert result is not None
    assert result.origin == "exact_cache"
    assert result.source_department == "geopolitics"
    assert result.source_agent == "geo_intelligence"
    assert "半導体供給網" in result.assessment
    assert result.external_call_id == "call-1"


def test_find_exact_cache_high_freshness_tighter_threshold_misses():
    client = FakeSupabaseClient({"external_intelligence_calls": [_cached_row()]})
    now = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)  # 3時間経過（既定high閾値2時間を超過）
    result = gw.find_exact_cache(client, _config(), domain="geopolitics", request_type="user_question",
                                  request_fingerprint_hash="hash-1", freshness_requirement="high",
                                  capability="chat", now=now)
    assert result is None


def test_find_exact_cache_no_matching_hash_returns_none():
    client = FakeSupabaseClient({"external_intelligence_calls": [_cached_row(fingerprint="other-hash")]})
    result = gw.find_exact_cache(client, _config(), domain="geopolitics", request_type="user_question",
                                  request_fingerprint_hash="hash-1", capability="chat")
    assert result is None


def test_find_exact_cache_unsupported_domain_returns_none():
    client = FakeSupabaseClient({"external_intelligence_calls": [_cached_row()]})
    result = gw.find_exact_cache(client, _config(), domain="scm", request_type="user_question",
                                  request_fingerprint_hash="hash-1", capability="chat")
    assert result is None


def test_find_exact_cache_db_exception_returns_none():
    class BoomClient:
        def select(self, *a, **k):
            raise RuntimeError("db down")
    result = gw.find_exact_cache(BoomClient(), _config(), domain="geopolitics",
                                  request_type="user_question", request_fingerprint_hash="hash-1",
                                  capability="chat")
    assert result is None


# ─── retrieve_existing_intelligence() -----------------------------------------------
def _geo_item(item_id="item-1", title="タイトル", geo_assessment="評価文", why_relevant="関連理由",
              themes=None, regions=None, include_in_weekly=True):
    return {"id": item_id, "title": title, "geo_assessment": geo_assessment,
            "why_relevant": why_relevant, "political_dynamics": None, "outlook": None,
            "confidence": "medium", "as_of": "2026-09-01T00:00:00+00:00", "event_date": None,
            "references": [], "sustainability_themes": themes or [], "country_region": regions or [],
            "include_in_weekly": include_in_weekly}


def test_retrieve_existing_intelligence_kill_switch_off_returns_empty():
    client = FakeSupabaseClient({"weekly_geo_intelligence_items": [_geo_item()]})
    result = gw.retrieve_existing_intelligence(client, _config(search={"enabled": False}),
                                                domains=["geopolitics"], keywords=["タイトル"],
                                                capability="search")
    assert result == []


def test_retrieve_existing_intelligence_keyword_match():
    client = FakeSupabaseClient({"weekly_geo_intelligence_items": [
        _geo_item(item_id="match", geo_assessment="メキシコの水資源を巡る政治的対立"),
        _geo_item(item_id="nomatch", geo_assessment="無関係な話題"),
    ]})
    result = gw.retrieve_existing_intelligence(client, _config(), domains=["geopolitics"],
                                                keywords=["メキシコ", "水資源"], capability="chat")
    assert [s.source_item_id for s in result] == ["match"]
    assert result[0].origin == "retrieved"


def test_retrieve_existing_intelligence_excludes_not_included_in_weekly():
    client = FakeSupabaseClient({"weekly_geo_intelligence_items": [
        _geo_item(item_id="excluded", geo_assessment="メキシコの水資源問題", include_in_weekly=False),
    ]})
    result = gw.retrieve_existing_intelligence(client, _config(), domains=["geopolitics"],
                                                keywords=["メキシコ"], capability="chat")
    assert result == []


def test_retrieve_existing_intelligence_unsupported_domain_ignored():
    client = FakeSupabaseClient({"weekly_geo_intelligence_items": [_geo_item()]})
    result = gw.retrieve_existing_intelligence(client, _config(), domains=["scm"],
                                                keywords=["タイトル"], capability="chat")
    assert result == []


def test_retrieve_existing_intelligence_respects_limit():
    items = [_geo_item(item_id=f"item-{i}", geo_assessment="共通キーワード一致") for i in range(10)]
    client = FakeSupabaseClient({"weekly_geo_intelligence_items": items})
    result = gw.retrieve_existing_intelligence(client, _config(), domains=["geopolitics"],
                                                keywords=["共通キーワード"], limit=3, capability="chat")
    assert len(result) == 3


def test_retrieve_existing_intelligence_db_exception_returns_empty():
    class BoomClient:
        def select(self, *a, **k):
            raise RuntimeError("db down")
    result = gw.retrieve_existing_intelligence(BoomClient(), _config(), domains=["geopolitics"],
                                                keywords=["x"], capability="chat")
    assert result == []


# ─── query_fresh() -------------------------------------------------------------------
def test_query_fresh_kill_switch_off_returns_none_without_calling_geo():
    with patch("cross_domain_intelligence_service.query_geo_intelligence") as mock_q:
        result = gw.query_fresh(FakeSupabaseClient(), _config(chat={"enabled": False}),
                                 domain="geopolitics", request_type="user_question",
                                 question="質問", capability="chat")
    assert result is None
    mock_q.assert_not_called()


def test_query_fresh_success_normalizes_to_source():
    from geo_intelligence_schema import parse_response
    response = parse_response(sufficient_body("req-1"))
    with patch("cross_domain_intelligence_service.query_geo_intelligence",
               return_value={"success": True, "response": response, "external_call_id": "call-9"}):
        result = gw.query_fresh(FakeSupabaseClient(), _config(), domain="geopolitics",
                                 request_type="user_question", question="台湾情勢は？",
                                 capability="chat")
    assert result is not None
    assert result.origin == "fresh"
    assert result.external_call_id == "call-9"
    assert "半導体供給網" in result.assessment


def test_query_fresh_failure_returns_none():
    with patch("cross_domain_intelligence_service.query_geo_intelligence",
               return_value={"success": False, "response": None}):
        result = gw.query_fresh(FakeSupabaseClient(), _config(), domain="geopolitics",
                                 request_type="user_question", question="質問", capability="chat")
    assert result is None


def test_query_fresh_exception_returns_none():
    with patch("cross_domain_intelligence_service.query_geo_intelligence",
               side_effect=RuntimeError("network down")):
        result = gw.query_fresh(FakeSupabaseClient(), _config(), domain="geopolitics",
                                 request_type="user_question", question="質問", capability="chat")
    assert result is None


def test_query_fresh_unsupported_domain_returns_none():
    result = gw.query_fresh(FakeSupabaseClient(), _config(), domain="scm",
                             request_type="user_question", question="質問", capability="chat")
    assert result is None


# ─── アダプタの欠損フィールド保持 ------------------------------------------------------
def test_source_from_geo_item_keeps_missing_fields_as_none():
    item = {"id": "item-x", "title": None, "geo_assessment": None, "why_relevant": None,
            "political_dynamics": None, "outlook": None, "confidence": None, "as_of": None,
            "event_date": None, "references": None}
    source = gw._source_from_geo_item(item)
    assert source.title is None
    assert source.assessment is None
    assert source.as_of is None
    assert source.references == []


def test_source_from_cached_row_malformed_payload_returns_none():
    row = {"id": "call-1", "response_payload": {"not": "a valid geo response"}}
    assert gw._source_from_cached_row(row) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
