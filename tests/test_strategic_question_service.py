"""strategic_question_service.py の単体テスト。LLM呼び出しはモック、DBはFakeSupabaseClient。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import strategic_question_service as sqs  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

CONFIG = {"strategic_question": {"hmac_secret": "test-secret-key-for-unit-tests"}}


def _analysis(tension_strength=70, dimension_key="cost_vs_sustainability_impact",
              has_real_tradeoff=True, evidence=True):
    return {
        "signals": [], "strategic_implications": ["示唆A"],
        "suntory_strategy_relation": {"related_fact_ids": [], "relation_summary": "関連あり"},
        "competitor_comparison": {
            "summary": "", "related_company_names": [],
            "evidence_change_event_ids": ["ce-1"] if evidence else [],
            "evidence_initiative_ids": [],
        },
        "strategic_tension": {
            "tension_summary": "対立あり", "decision_dimension_key": dimension_key,
            "decision_dimension_label": "対立軸ラベル", "tension_strength": tension_strength,
        },
        "has_real_tradeoff": has_real_tradeoff,
    }


def _question(trade_off_quality=85, neutrality=85, answerability=85):
    return {
        "title": "見出し", "question_text": "質問文",
        "options": [{"option_code": "A", "label": "A案", "description": "d", "is_status_quo": False},
                    {"option_code": "B", "label": "B案", "description": "d", "is_status_quo": True}],
        "framing_notes": "", "trade_off_quality": trade_off_quality,
        "neutrality": neutrality, "answerability": answerability,
    }


# ─── スコアリング ─────────────────────────────────────────────────
def test_score_stage1_higher_tension_yields_higher_score():
    low = sqs.score_stage1(_analysis(tension_strength=20), "sufficient", 50)
    high = sqs.score_stage1(_analysis(tension_strength=90), "sufficient", 50)
    assert high > low


def test_score_stage1_coverage_affects_score():
    sufficient = sqs.score_stage1(_analysis(), "sufficient", 50)
    insufficient = sqs.score_stage1(_analysis(), "insufficient", 50)
    assert sufficient > insufficient


def test_score_stage2_hard_excludes_exact_dimension_match():
    recent = {("水", "cost_vs_sustainability_impact")}
    result = sqs.score_stage2("水", "cost_vs_sustainability_impact", 80.0, _question(), recent)
    assert result["is_duplicate"] is True
    assert result["score"] == 0.0


def test_score_stage2_allows_different_theme_same_dimension():
    recent = {("水", "cost_vs_sustainability_impact")}
    result = sqs.score_stage2("気候変動・GHG", "cost_vs_sustainability_impact", 80.0, _question(), recent)
    assert result["is_duplicate"] is False
    assert result["score"] > 0


def test_select_best_candidate_picks_highest_stage2_score():
    candidates = [
        {"theme": "水", "stage2": {"score": 50.0, "is_duplicate": False}},
        {"theme": "容器包装", "stage2": {"score": 90.0, "is_duplicate": False}},
    ]
    best = sqs.select_best_candidate(candidates)
    assert best["theme"] == "容器包装"


def test_select_best_candidate_returns_none_when_all_duplicates():
    candidates = [{"theme": "水", "stage2": {"score": 0.0, "is_duplicate": True}}]
    assert sqs.select_best_candidate(candidates) is None


# ─── トークン導出 ─────────────────────────────────────────────────
def test_derive_recipient_key_differs_by_question_id():
    k1 = sqs.derive_recipient_key(CONFIG, "question-1", "taro@example.com")
    k2 = sqs.derive_recipient_key(CONFIG, "question-2", "taro@example.com")
    assert k1 != k2


def test_derive_recipient_key_same_for_same_question_and_email():
    k1 = sqs.derive_recipient_key(CONFIG, "question-1", "Taro@Example.com")
    k2 = sqs.derive_recipient_key(CONFIG, "question-1", "taro@example.com")
    assert k1 == k2  # 大小文字を正規化して同一人物として扱う


def test_derive_response_token_deterministic_for_same_delivery_id():
    t1 = sqs.derive_response_token(CONFIG, "delivery-abc")
    t2 = sqs.derive_response_token(CONFIG, "delivery-abc")
    assert t1 == t2


def test_derive_response_token_differs_by_delivery_id():
    t1 = sqs.derive_response_token(CONFIG, "delivery-abc")
    t2 = sqs.derive_response_token(CONFIG, "delivery-xyz")
    assert t1 != t2


# ─── 質問生成のスキップ（has_real_tradeoff） ─────────────────────────
def test_generate_question_from_analysis_skipped_when_no_tradeoff(monkeypatch):
    call_count = {"n": 0}
    monkeypatch.setattr(sqs.common, "call_llm_structured", lambda *a, **k: call_count.__setitem__("n", call_count["n"] + 1))
    result = sqs.generate_question_from_analysis(None, "model", _analysis(has_real_tradeoff=False))
    assert result is None
    assert call_count["n"] == 0


def test_generate_question_from_analysis_calls_llm_when_tradeoff_exists(monkeypatch):
    monkeypatch.setattr(sqs.common, "call_llm_structured",
                         lambda *a, **k: {"data": _question(), "token_usage": {}, "latency_ms": 100})
    result = sqs.generate_question_from_analysis(None, "model", _analysis(has_real_tradeoff=True))
    assert result["title"] == "見出し"


# ─── 保存・取得・埋め込み ─────────────────────────────────────────
def _fake_client():
    return FakeSupabaseClient({
        "sustainability_strategic_questions": [], "sustainability_strategic_question_options": [],
        "sustainability_strategic_question_deliveries": [], "sustainability_strategic_question_responses": [],
    })


def test_save_draft_question_creates_question_and_options():
    client = _fake_client()
    selected = {"theme": "水", "analysis": _analysis(), "question": _question(),
                "stage1_score": 80.0, "stage2_score": 85.0}
    question_id = sqs.save_draft_question(
        client, period_start="2026-09-01", period_end="2026-09-08", selected=selected,
        all_candidates=[], response_window_days=5, model_deployment="gpt-4o",
        token_usage=None, latency_ms=None)
    question = sqs.get_question(client, question_id)
    assert question["question_status"] == "draft"
    assert question["theme"] == "水"
    assert len(question["_options"]) == 2


def test_get_draft_question_for_digest_embed_only_returns_draft():
    client = _fake_client()
    selected = {"theme": "水", "analysis": _analysis(), "question": _question(),
                "stage1_score": 80.0, "stage2_score": 85.0}
    question_id = sqs.save_draft_question(
        client, period_start="2026-09-01", period_end="2026-09-08", selected=selected,
        all_candidates=[], response_window_days=5, model_deployment="m", token_usage=None, latency_ms=None)

    found = sqs.get_draft_question_for_digest_embed(client, "2026-09-01", "2026-09-08")
    assert found["question_id"] == question_id

    sqs.mark_embedded(client, question_id, "report-1")
    assert sqs.get_draft_question_for_digest_embed(client, "2026-09-01", "2026-09-08") is None


# ─── 配信発行の冪等性 ─────────────────────────────────────────────
def test_activate_on_digest_approval_is_idempotent():
    client = _fake_client()
    selected = {"theme": "水", "analysis": _analysis(), "question": _question(),
                "stage1_score": 80.0, "stage2_score": 85.0}
    question_id = sqs.save_draft_question(
        client, period_start="2026-09-01", period_end="2026-09-08", selected=selected,
        all_candidates=[], response_window_days=5, model_deployment="m", token_usage=None, latency_ms=None)

    result1 = sqs.activate_on_digest_approval(client, CONFIG, question_id, ["a@example.com", "b@example.com"])
    deliveries_after_first = list(client.tables["sustainability_strategic_question_deliveries"])
    assert len(deliveries_after_first) == 2

    result2 = sqs.activate_on_digest_approval(client, CONFIG, question_id, ["a@example.com", "b@example.com"])
    deliveries_after_second = client.tables["sustainability_strategic_question_deliveries"]
    assert len(deliveries_after_second) == 2  # 重複発行されない
    assert result1["a@example.com"]["delivery_id"] == result2["a@example.com"]["delivery_id"]
    assert result1["a@example.com"]["token"] == result2["a@example.com"]["token"]  # トークンも同一


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
