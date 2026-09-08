"""Weekly Strategic Question のE2E統合テスト（FakeSupabaseClient一本、実LLM/実HTTP無し）。

draft保存 → 承認バンドル(deliveries発行) → 回答2件 → close(時間経過) →
generate_pending_insights → チャット取得、までを一気通貫で確認する。
LLM失敗時にquestion_statusはclosedのまま、insight_statusだけerrorになり、
再実行でsuccessに回復することも確認する（close/insight生成の分離が機能しているか）。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import decision_insight_service as insight_service  # noqa: E402
import strategic_question_responses as sqr  # noqa: E402
import strategic_question_service as sqs  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

CONFIG = {"strategic_question": {"hmac_secret": "integration-test-secret",
                                  "min_responses_for_publish": 2, "min_response_rate_for_publish": 0.1,
                                  "chat_max_age_days": 180, "chat_max_items": 3}}


def _analysis():
    return {
        "signals": [], "strategic_implications": ["示唆"],
        "suntory_strategy_relation": {"related_fact_ids": [], "relation_summary": "関連"},
        "competitor_comparison": {"summary": "", "related_company_names": [],
                                   "evidence_change_event_ids": [], "evidence_initiative_ids": []},
        "strategic_tension": {"tension_summary": "対立", "decision_dimension_key": "cost_vs_sustainability_impact",
                               "decision_dimension_label": "コスト vs インパクト", "tension_strength": 75},
        "has_real_tradeoff": True,
    }


def _question_payload():
    return {"title": "見出し", "question_text": "質問文",
            "options": [{"option_code": "A", "label": "A案", "description": "d", "is_status_quo": False},
                        {"option_code": "B", "label": "B案", "description": "d", "is_status_quo": True}],
            "framing_notes": "", "trade_off_quality": 85, "neutrality": 85, "answerability": 85}


def _fake_client():
    return FakeSupabaseClient({
        "sustainability_strategic_questions": [], "sustainability_strategic_question_options": [],
        "sustainability_strategic_question_deliveries": [], "sustainability_strategic_question_responses": [],
        "sustainability_decision_insights": [],
    })


def test_full_lifecycle_draft_to_chat_retrieval(monkeypatch):
    client = _fake_client()

    # 1) draft保存
    selected = {"theme": "水", "analysis": _analysis(), "question": _question_payload(),
                "stage1_score": 80.0, "stage2_score": 85.0}
    question_id = sqs.save_draft_question(
        client, period_start="2026-09-01", period_end="2026-09-08", selected=selected,
        all_candidates=[], response_window_days=5, model_deployment="gpt-4o",
        token_usage=None, latency_ms=None)
    assert sqs.get_question(client, question_id)["question_status"] == "draft"

    # 2) 週次ダイジェスト埋め込み
    embedded = sqs.get_draft_question_for_digest_embed(client, "2026-09-01", "2026-09-08")
    assert embedded["question_id"] == question_id
    sqs.mark_embedded(client, question_id, "report-1")
    assert sqs.get_draft_question_for_digest_embed(client, "2026-09-01", "2026-09-08") is None

    # 3) 承認バンドル: deliveries発行（この時点でquestion_status='open'になる）
    recipients = [f"user{i}@example.com" for i in range(10)]
    per_recipient = sqs.activate_on_digest_approval(client, CONFIG, question_id, recipients)
    assert len(per_recipient) == 10
    question = sqs.get_question(client, question_id)
    assert question["question_status"] == "open"

    # 送信成功を記録（配信率計算の分母に使う）
    for info in per_recipient.values():
        sqs.mark_delivery_sent(client, info["delivery_id"], success=True)

    # 4) 2件回答（respondent_keyで重複排除される設計、メールアドレスは保持しない）
    option_a = sqr.get_option_or_none(client, question_id, "A")
    for email in recipients[:2]:
        token = per_recipient[email]["token"]
        delivery = sqr.validate_token(client, question_id, token)
        assert "error" not in delivery
        sqr.record_response(client, delivery, option_a["option_id"], "理由コメント")
    responses = client.tables["sustainability_strategic_question_responses"]
    assert len(responses) == 2
    assert all("email" not in r for r in responses)  # メールアドレスがどこにも残っていない

    # 5) closeより前は集計できるがまだcloseされない
    agg = sqr.aggregate_results(client, question_id)
    assert agg["total_responses"] == 2
    assert agg["delivery_count"] == 10

    # 6) 時間経過させてclose_due_questions（LLM呼び出し無し）
    client.update("sustainability_strategic_questions", {"question_id": f"eq.{question_id}"},
                  {"closes_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
    closed_ids = sqs.close_due_questions(client)
    assert question_id in closed_ids
    question = sqs.get_question(client, question_id)
    assert question["question_status"] == "closed"
    assert question["insight_status"] == "pending"  # closeとinsight生成は別ライフサイクル

    # recipient_emailがNULL化されている（closed遷移時のプライバシー最小化）
    deliveries = client.tables["sustainability_strategic_question_deliveries"]
    assert all(d["recipient_email"] is None for d in deliveries)

    # 7) LLM失敗ケース: insight_status='error'のまま、question_statusはclosedを維持する
    def _failing_llm(*a, **k):
        raise RuntimeError("LLM呼び出し失敗（テスト用）")
    monkeypatch.setattr(insight_service.common, "call_llm_structured", _failing_llm)
    processed = sqs.generate_pending_insights(client, None, "model", CONFIG)
    assert question_id in processed
    question = sqs.get_question(client, question_id)
    assert question["question_status"] == "closed"  # closeは成否に関わらず確定したまま
    assert question["insight_status"] == "error"
    assert question["insight_error_message"]

    # 8) 再実行で成功に回復する（insight_statusのみ独立して再試行できる）
    def _succeeding_llm(*a, **k):
        return {"data": {"observed_tendency": "傾向あり", "reasoning_summary": "要約",
                          "representative_reasoning": ["一般化した理由"], "confidence": 0.7},
                "token_usage": {}, "latency_ms": 100}
    monkeypatch.setattr(insight_service.common, "call_llm_structured", _succeeding_llm)
    processed2 = sqs.generate_pending_insights(client, None, "model", CONFIG)
    assert question_id in processed2
    question = sqs.get_question(client, question_id)
    assert question["insight_status"] == "success"

    insights = client.tables["sustainability_decision_insights"]
    assert len(insights) == 1
    assert insights[0]["response_count"] == 2
    assert insights[0]["delivery_count"] == 10

    # 9) 「先週の結果」として未掲載 → 掲載可能（閾値2件/10%を満たす）
    unincluded = sqs.get_last_unincluded_insight_for_digest(client, CONFIG)
    assert unincluded is not None
    sqs.mark_insight_included(client, unincluded["insight_id"], "report-2")
    assert sqs.get_last_unincluded_insight_for_digest(client, CONFIG) is None

    # 10) チャットからの参照: guardrail文言付きで取得できる
    block = insight_service.build_decision_insight_chat_block(client, CONFIG, themes=["水"])
    assert "組織内の判断傾向" in block
    assert "正式な会社方針ではありません" in block
    assert "傾向あり" in block


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
