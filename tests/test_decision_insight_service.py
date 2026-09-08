"""decision_insight_service.py の単体テスト。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import decision_insight_service as insight_service  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

CONFIG = {"strategic_question": {"min_responses_for_publish": 10, "min_response_rate_for_publish": 0.15,
                                  "chat_max_age_days": 180, "chat_max_items": 3}}


def _question(theme="水", dimension_key="cost_vs_sustainability_impact"):
    return {"question_id": "q-1", "theme": theme, "decision_dimension_key": dimension_key,
            "decision_dimension_label": "対立軸ラベル", "question_text": "質問文"}


def _aggregation(total=15, delivery=50):
    return {"total_responses": total, "delivery_count": delivery, "response_rate": total / delivery,
            "by_option": [{"option_code": "A", "label": "A案", "count": total, "pct": 100.0}],
            "comments": ["理由コメント1", "理由コメント2"]}


def _llm_data(confidence=0.8):
    return {"observed_tendency": "傾向の要約", "reasoning_summary": "理由の要約",
            "representative_reasoning": ["一般化された理由1"], "confidence": confidence}


def _fake_client():
    return FakeSupabaseClient({"sustainability_decision_insights": []})


def test_distill_and_save_creates_insight_row(monkeypatch):
    client = _fake_client()
    monkeypatch.setattr(insight_service.common, "call_llm_structured",
                         lambda *a, **k: {"data": _llm_data(), "token_usage": {}, "latency_ms": 100})
    insight_id = insight_service.distill_and_save(client, None, "model", _question(), _aggregation())
    rows = client.tables["sustainability_decision_insights"]
    assert len(rows) == 1
    assert rows[0]["insight_id"] == insight_id
    assert rows[0]["distribution_json"]["by_option"][0]["count"] == 15  # LLMではなくaggregationの値


def test_distill_and_save_does_not_supersede_prior_same_dimension(monkeypatch):
    """MVPでは自動supersedeしない: 同一(theme, decision_dimension_key)で2回生成しても
    両方is_current=trueのまま残る"""
    client = _fake_client()
    monkeypatch.setattr(insight_service.common, "call_llm_structured",
                         lambda *a, **k: {"data": _llm_data(), "token_usage": {}, "latency_ms": 100})
    insight_service.distill_and_save(client, None, "model", _question(), _aggregation())
    insight_service.distill_and_save(client, None, "model", _question(), _aggregation())
    rows = client.tables["sustainability_decision_insights"]
    assert len(rows) == 2
    assert all(r["is_current"] for r in rows)
    assert all(r["superseded_by"] is None for r in rows)


def test_publishable_requires_both_count_and_rate():
    ok = {"response_count": 10, "delivery_count": 50}  # rate=0.2
    assert insight_service.publishable(ok, CONFIG) is True

    low_count = {"response_count": 5, "delivery_count": 20}  # rate=0.25だが件数不足
    assert insight_service.publishable(low_count, CONFIG) is False

    low_rate = {"response_count": 12, "delivery_count": 200}  # 件数は足りるが rate=0.06
    assert insight_service.publishable(low_rate, CONFIG) is False


def test_publishable_zero_delivery_count_is_false():
    assert insight_service.publishable({"response_count": 0, "delivery_count": 0}, CONFIG) is False


_insight_counter = [0]


def _insight_row(theme="水", is_current=True, response_count=15, delivery_count=50,
                  observed_at=None, dimension_key="cost_vs_sustainability_impact"):
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    _insight_counter[0] += 1
    return {
        "insight_id": f"ins-{_insight_counter[0]}", "theme": theme, "decision_dimension_key": dimension_key,
        "decision_dimension_label": "対立軸", "is_current": is_current,
        "response_count": response_count, "delivery_count": delivery_count,
        "observed_at": observed_at, "observed_tendency": "傾向", "reasoning_summary": "",
        "guardrail_label": "会社の正式方針ではありません",
    }


def test_get_recent_insights_for_chat_excludes_stale():
    stale_date = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    fresh = _insight_row()
    stale = _insight_row(observed_at=stale_date)
    client = FakeSupabaseClient({"sustainability_decision_insights": [fresh, stale]})
    result = insight_service.get_recent_insights_for_chat(client, CONFIG)
    ids = [r["insight_id"] for r in result]
    assert fresh["insight_id"] in ids
    assert stale["insight_id"] not in ids


def test_get_recent_insights_for_chat_excludes_non_current():
    current = _insight_row(is_current=True)
    not_current = _insight_row(is_current=False)
    client = FakeSupabaseClient({"sustainability_decision_insights": [current, not_current]})
    result = insight_service.get_recent_insights_for_chat(client, CONFIG)
    ids = [r["insight_id"] for r in result]
    assert current["insight_id"] in ids
    assert not_current["insight_id"] not in ids


def test_get_recent_insights_for_chat_excludes_below_threshold():
    low_response = _insight_row(response_count=2, delivery_count=50)
    client = FakeSupabaseClient({"sustainability_decision_insights": [low_response]})
    result = insight_service.get_recent_insights_for_chat(client, CONFIG)
    assert result == []


def test_build_decision_insight_chat_block_contains_guardrail_and_dimension():
    client = FakeSupabaseClient({"sustainability_decision_insights": [_insight_row()]})
    block = insight_service.build_decision_insight_chat_block(client, CONFIG)
    assert "正式な会社方針ではありません" in block
    assert "対立軸" in block


def test_build_decision_insight_chat_block_empty_when_no_matches():
    client = FakeSupabaseClient({"sustainability_decision_insights": []})
    assert insight_service.build_decision_insight_chat_block(client, CONFIG) == ""


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
