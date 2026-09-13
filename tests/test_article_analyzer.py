"""article_analyzer.py の Secondary Redundancy Classification / Representative Source
Priority 関連ロジックのテスト。既存の7項目スコアリング・タグ付与・primary source判定
自体は変更していないため、それらの回帰テストは対象外（既存挙動を壊していないことは
save_analysisの新規カラム以外の出力が従来通りであることで間接的に確認する）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_analyzer as aa  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

RANKS = [
    {"rank": "S", "min_score": 29, "max_score": 35, "default_publication": "速報"},
    {"rank": "A", "min_score": 23, "max_score": 28, "default_publication": "週次メール"},
    {"rank": "B", "min_score": 17, "max_score": 22, "default_publication": "メール候補"},
    {"rank": "C", "min_score": 10, "max_score": 16, "default_publication": "Notionのみ"},
    {"rank": "D", "min_score": 0, "max_score": 9, "default_publication": "非掲載"},
]


def _article(article_id="sec-1", published_at="2026-08-20T00:00:00+00:00"):
    return {"article_id": article_id, "title": "テスト記事", "extracted_text": "本文",
            "published_at": published_at}


def _base_scores():
    return {"business_relevance": 4, "change_severity": 3, "impact_scope": 3,
            "certainty_stage": 4, "urgency": 2, "novelty": 3, "decision_value": 3}


# ─── find_candidate_primary_articles ──────────────────────────────
def _client_with_primary(primary_published_at="2026-08-19T00:00:00+00:00", primary_tags=("TH-02",)):
    return FakeSupabaseClient({
        "articles": [
            {"article_id": "primary-1", "title": "一次情報", "summary_short": "要約",
             "published_at": primary_published_at, "is_current": True},
        ],
        "article_analysis": [
            {"article_id": "primary-1", "primary_source_status": "一次情報",
             "is_current": True, "analysis_status": "処理済"},
        ],
        "article_tags": [{"article_id": "primary-1", "tag_id": t} for t in primary_tags],
    })


def test_find_candidate_primary_articles_returns_empty_when_no_articles():
    client = FakeSupabaseClient({"articles": [], "article_analysis": [], "article_tags": []})
    result = aa.find_candidate_primary_articles(client, _article(), ["TH-02"])
    assert result == []


def test_find_candidate_primary_articles_finds_tag_overlapping_primary():
    client = _client_with_primary()
    result = aa.find_candidate_primary_articles(client, _article(), ["TH-02", "TH-01"])
    assert [c["article_id"] for c in result] == ["primary-1"]


def test_find_candidate_primary_articles_excludes_outside_date_window():
    client = _client_with_primary(primary_published_at="2026-07-01T00:00:00+00:00")
    result = aa.find_candidate_primary_articles(client, _article(), ["TH-02"])
    assert result == []


def test_find_candidate_primary_articles_excludes_no_tag_overlap():
    client = _client_with_primary(primary_tags=("TH-05",))
    result = aa.find_candidate_primary_articles(client, _article(), ["TH-02"])
    assert result == []


def test_find_candidate_primary_articles_excludes_non_primary_status():
    client = FakeSupabaseClient({
        "articles": [{"article_id": "sec-2", "title": "別の二次記事", "summary_short": "",
                      "published_at": "2026-08-19T00:00:00+00:00", "is_current": True}],
        "article_analysis": [{"article_id": "sec-2", "primary_source_status": "解釈・分析",
                               "is_current": True, "analysis_status": "処理済"}],
        "article_tags": [{"article_id": "sec-2", "tag_id": "TH-02"}],
    })
    result = aa.find_candidate_primary_articles(client, _article(), ["TH-02"])
    assert result == []


# ─── save_analysis: ユーザー仕様書の4例をそのままテストケース化 ──────────
def _save_and_get(result_overrides: dict) -> dict:
    client = FakeSupabaseClient({"article_analysis": [], "article_tags": [], "article_evidence": []})
    result = {
        "scores": _base_scores(), "summary_short": "要約", "importance_reason": "理由",
        "primary_source_status": "解釈・分析", "tags": [],
    }
    result.update(result_overrides)
    aa.save_analysis(client, _article(), result, RANKS, "test-model", set(), {})
    return client.tables["article_analysis"][0]


def test_redundant_summary_applies_correction_and_suppresses():
    # Primary: 「2030年目標を40%→30%へ変更」 / Secondary: 単純な言い換えのみ
    row = _save_and_get({
        "secondary_redundancy_classification": "redundant_summary",
        "matched_primary_article_id": "primary-1",
        "redundancy_reason": "一次情報の言い換えに留まる",
    })
    assert row["importance_scores"]["novelty"] == _base_scores()["novelty"] - 1
    assert row["importance_scores"]["decision_value"] == _base_scores()["decision_value"] - 1
    assert row["importance_scores_raw"] == _base_scores()
    assert row["representative_role"] == "suppressed_duplicate"
    assert row["importance_total_score"] == sum(_base_scores().values()) - 2
    assert "機械補正" in row["importance_reason"]


def test_value_added_analysis_is_not_penalized():
    # Secondary: 設備投資遅延・EU規制変更という背景、競合3社の同様傾向を追加
    row = _save_and_get({
        "secondary_redundancy_classification": "value_added_analysis",
        "matched_primary_article_id": "primary-1",
    })
    assert row["importance_scores"] == _base_scores()
    assert row["importance_scores"] == row["importance_scores_raw"]
    assert row["representative_role"] == "supporting_analysis"
    assert "機械補正" not in row["importance_reason"]


def test_value_added_reporting_is_not_penalized():
    # Secondary: 関係者インタビュー・未公表投資額を追加
    row = _save_and_get({
        "secondary_redundancy_classification": "value_added_reporting",
        "matched_primary_article_id": "primary-1",
    })
    assert row["importance_scores"] == _base_scores()
    assert row["representative_role"] == "additional_reporting"


def test_low_value_primary_gets_no_bonus_from_being_primary():
    # Primary単体（候補探索の対象外）: 一次情報であること自体による加点が無いこと
    row = _save_and_get({
        "primary_source_status": "一次情報",
        "scores": {**_base_scores(), "business_relevance": 1, "change_severity": 0,
                   "impact_scope": 0, "certainty_stage": 5, "urgency": 0, "novelty": 1,
                   "decision_value": 0},
    })
    assert row["secondary_redundancy_classification"] is None
    assert row["representative_role"] == "representative"
    assert row["importance_total_score"] == 7  # 一次情報でも生スコアの合計のまま


def test_redundant_summary_score_does_not_go_below_zero():
    row = _save_and_get({
        "scores": {**_base_scores(), "novelty": 0, "decision_value": 0},
        "secondary_redundancy_classification": "redundant_summary",
        "matched_primary_article_id": "primary-1",
    })
    assert row["importance_scores"]["novelty"] == 0
    assert row["importance_scores"]["decision_value"] == 0


def test_no_classification_keeps_existing_behavior_unchanged():
    """secondary_redundancy_classificationが無い（旧来通りの）記事は、新カラムが
    representative既定値・raw=finalになるだけで、既存の重要度計算に一切影響しないこと"""
    row = _save_and_get({})
    assert row["importance_scores"] == _base_scores()
    assert row["importance_scores_raw"] == _base_scores()
    assert row["representative_role"] == "representative"
    assert row["secondary_redundancy_classification"] is None
    assert row["matched_primary_article_id"] is None


# ─── Phase 4A (atp-002): needs_web_verificationの過剰発火/根拠の薄さ対策 ──
# 2026-09-13: 「不確実・珍しい・重要そう」というだけでは検証を発火させず、
# 誤りなら評価が変わるほど重要な具体的一次事実に限定するプロンプト変更を
# 行った。ここではプロンプト文言そのものの回帰ガードと、call1がfalseと
# 判定した場合にcall2(verify_claim)が発火しないこと（＝コード側のゲート
# ロジック自体は変更していないこと）を確認する。


def test_needs_web_verification_prompt_forbids_vague_triggers():
    """「重要そうだから」等の曖昧な理由だけでのtrue判定を禁止する文言と、
    紋切り型の継続方針表明を検証対象外とする文言がプロンプトにあること"""
    prompt = aa.SYSTEM_PROMPT_TEMPLATE
    assert "重要そうだから" in prompt
    assert "一般論的に言い換えただけの記述" in prompt
    assert "紋切り型の言い回し" in prompt


def test_verify_prompt_distinguishes_unconfirmed_from_contradicted():
    """「見つからない(未確認)」と「矛盾する情報が見つかった(反証)」の区別、
    および何を検証したかを説明文に含める指示がVERIFY_SYSTEM_PROMPTにあること"""
    prompt = aa.VERIFY_SYSTEM_PROMPT
    assert "反証された" in prompt
    assert "検証対象の主張が何であったか" in prompt


class _FakeAzureResponse:
    def __init__(self, output_text):
        self.output_text = output_text
        self.output = []


class _FakeAzureClient:
    """analyze_article()のcall1/call2呼び出し回数・引数を記録するフェイク。
    tool_choice="required"の有無でcall1/call2を判別する（実装と同じ判別軸）"""

    def __init__(self, call1_json, call2_json=None):
        self._call1_json = call1_json
        self._call2_json = call2_json
        self.call_count = 0
        self.saw_tool_choice_required = False
        outer = self

        class _Responses:
            def create(self, **kwargs):
                outer.call_count += 1
                if kwargs.get("tool_choice") == "required":
                    outer.saw_tool_choice_required = True
                    return _FakeAzureResponse(outer._call2_json)
                return _FakeAzureResponse(outer._call1_json)

        self.responses = _Responses()


def _call1_payload(**overrides):
    payload = {
        "tags": [], "primary_source_status": "解釈・分析", "source_status_reason": "理由",
        "needs_web_verification": False, "primary_source_claim": None,
        "scores": _base_scores(), "score_reasons": {k: "理由" for k in aa.SCORE_CRITERIA_ORDER},
        "correction_applied": None, "summary_short": "要約", "importance_reason": "理由",
        "needs_review": False,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_call2_not_invoked_when_call1_finds_no_material_claim():
    """call1がneeds_web_verification=falseと判定した場合、call2(verify_claim)は
    発火しないこと（atp-002が求める「検証は本当に必要な時だけ発火する」の
    コード側ゲートが変更されていないことの回帰確認）"""
    client = _FakeAzureClient(_call1_payload())
    result = aa.analyze_article(
        client, "test-model", "rubric", "tags",
        _article(), {"publisher_name": "p", "domain": "d"},
    )
    assert client.call_count == 1
    assert client.saw_tool_choice_required is False
    assert "_verification" not in result


def test_call2_still_invoked_when_call1_flags_material_claim():
    """atp-001回帰確認: call1が具体的な一次事実をneeds_web_verification=trueで
    挙げた場合は、従来通りcall2が発火し、verified結果がprimary_source_status等
    に反映されること（プロンプト変更でこの正常系を壊していないこと）"""
    call1_json = _call1_payload(
        primary_source_status="一次未確認",
        needs_web_verification=True,
        primary_source_claim="英国統計局(ONS)が2026年5月の月次GDP成長率を0.1%と発表した",
    )
    call2_text = (
        "検証対象の主張についてONS公式サイトを確認し、一致する発表を確認できました。\n"
        "```json\n" + json.dumps({"verified": True, "verification_note": "ONS公式発表と一致"}) + "\n```"
    )
    client = _FakeAzureClient(call1_json, call2_text)
    result = aa.analyze_article(
        client, "test-model", "rubric", "tags",
        _article(), {"publisher_name": "p", "domain": "d"},
    )
    assert client.call_count == 2
    assert client.saw_tool_choice_required is True
    assert result["_verification"]["verified"] is True
    assert result["primary_source_status"] == "一次照合済み"
    assert result["needs_review"] is False


# ─── Phase 4B (atp-003): needs_reviewを「モデルの自信」ではなく「根拠不足等の
# 業務条件」として再定義 ────────────────────────────────────────────
# 2026-09-13: atp-003（ペイウォール定型文のみで実質的内容が無い記事）で、
# needs_review=falseのまま（Baseline v1・Phase 4A後とも）だった問題への対応。
# Phase 4Aのneeds_web_verification発火条件（call1/call2のゲート）には一切
# 触れず、needs_reviewの定義文言のみを「人間の確認が必要な業務上の条件」
# （根拠不足・対応関係の弱さ・矛盾・曖昧さ・後続判断への影響）として明確化した。
# 「短い/情報が少ない/珍しい/自信が無い」というだけではtrueにしないことも
# 明記し、needs_web_verificationとは別軸であることも明示した。


def test_needs_review_prompt_defines_business_condition_not_confidence():
    """needs_reviewが「モデルの自信の低さ」ではなく、根拠不足・対応関係の弱さ・
    矛盾・曖昧さ・後続判断への影響という業務上の条件であることが明記されて
    いること"""
    prompt = aa.SYSTEM_PROMPT_TEMPLATE
    assert "人間による確認が必要な業務上の条件" in prompt
    assert "モデル自身の自信が低いというだけでは該当しない" in prompt
    assert "根拠が本文中に不足している" in prompt
    assert "主張と根拠の対応関係が弱い" in prompt
    assert "矛盾がある" in prompt
    assert "内容に曖昧さがあり複数の解釈が成立し得る" in prompt
    assert "後続の判断" in prompt


def test_needs_review_prompt_forbids_vague_triggers_and_short_article_alone():
    """「記事が短い・情報量が少ない・話題が珍しい」というだけではneeds_review=true
    にしないことが明記されていること（atp-003の裏側にある「短い/情報が少ない=即
    needs_review」という誤った近道の禁止）"""
    prompt = aa.SYSTEM_PROMPT_TEMPLATE
    assert "記事が短い・情報量が少ない・話題が珍しい" in prompt
    assert "ではtrueにしないこと" in prompt


def test_needs_review_prompt_distinguishes_from_web_verification_axis():
    """needs_reviewとneeds_web_verification（外部照合の要否）が別軸であり、
    外部照合が不要でも根拠不足・実質的内容の欠如ならneeds_review=trueになり得る
    ことが明記されていること（両者を混同しないこと自体の回帰ガード）"""
    prompt = aa.SYSTEM_PROMPT_TEMPLATE
    assert "needs_web_verification（外部照合の要否）とは別軸であり" in prompt
    assert "根拠不足・実質的内容の欠如であればneeds_review=trueになり得る" in prompt


def test_needs_review_prompt_covers_no_substantive_content_case():
    """本文から評価の拠り所となる実質的な内容がほとんど・全く得られない場合
    （atp-003: ペイウォール定型文のみ等）について、単に「重要度がゼロだと確定
    した」と断定せず、記事取得自体の失敗の可能性を含め人間の確認が必要な状態
    として扱うことが明示的にカバーされていること"""
    prompt = aa.SYSTEM_PROMPT_TEMPLATE
    assert "本文から評価の拠り所となる実質的な内容がほとんど・全く得られず" in prompt
    assert "重要度がゼロだと確定した" in prompt
    assert "記事取得自体が" in prompt and "失敗している可能性を含め人間の確認が必要な状態として扱うこと" in prompt


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
