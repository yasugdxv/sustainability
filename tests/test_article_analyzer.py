"""article_analyzer.py の Secondary Redundancy Classification / Representative Source
Priority 関連ロジックのテスト。既存の7項目スコアリング・タグ付与・primary source判定
自体は変更していないため、それらの回帰テストは対象外（既存挙動を壊していないことは
save_analysisの新規カラム以外の出力が従来通りであることで間接的に確認する）。
"""
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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
