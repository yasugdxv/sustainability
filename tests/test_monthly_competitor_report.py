import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import monthly_competitor_report as monthly  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


def _scored(initiative_id, score, recommended=True, duplicate_of=None):
    return {
        "initiative_id": initiative_id, "selection_score": score,
        "selection_reasons": ["テスト理由"], "recommended_for_monthly_email": recommended,
        "duplicate_of_initiative_id": duplicate_of,
    }


def test_select_top_initiatives_ranks_by_score_desc():
    scored = [_scored("i-1", 60), _scored("i-2", 90), _scored("i-3", 75)]
    result = monthly.select_top_initiatives(scored)
    assert [s["initiative_id"] for s in result] == ["i-2", "i-3", "i-1"]


def test_select_top_initiatives_excludes_not_recommended():
    scored = [_scored("i-1", 95, recommended=False), _scored("i-2", 80)]
    result = monthly.select_top_initiatives(scored)
    assert [s["initiative_id"] for s in result] == ["i-2"]


def test_select_top_initiatives_deduplicates_keeping_higher_score():
    scored = [_scored("i-1", 60, duplicate_of="i-2"), _scored("i-2", 85)]
    result = monthly.select_top_initiatives(scored)
    assert len(result) == 1
    assert result[0]["initiative_id"] == "i-2"


def test_select_top_initiatives_caps_at_max_n_but_does_not_force_minimum():
    scored = [_scored(f"i-{n}", 100 - n) for n in range(8)]
    result = monthly.select_top_initiatives(scored, max_n=5)
    assert len(result) == 5

    empty_result = monthly.select_top_initiatives([], max_n=5)
    assert empty_result == []


def test_resolve_period_handles_normal_month():
    report_month, period_start, period_end, period_end_exclusive = monthly.resolve_period("2026-07")
    assert report_month == "2026-07"
    assert period_start == date(2026, 7, 1)
    assert period_end == date(2026, 7, 31)
    assert period_end_exclusive == date(2026, 8, 1)


def test_resolve_period_handles_december_rollover():
    _, period_start, period_end, period_end_exclusive = monthly.resolve_period("2026-12")
    assert period_start == date(2026, 12, 1)
    assert period_end == date(2026, 12, 31)
    assert period_end_exclusive == date(2027, 1, 1)


def _company(company_id, name, is_own=False, category="ビール", order=1):
    return {"company_id": company_id, "company_name": name, "is_own_company": is_own,
            "industry_category": category, "display_order": order}


def test_get_target_companies_excludes_own_company_by_default():
    client = FakeSupabaseClient({"competitor_companies": [
        _company("c-1", "サントリー", is_own=True),
        _company("c-2", "ABインベブ", order=1),
        _company("c-3", "ハイネケン", order=2),
    ]})
    companies = monthly.get_target_companies(client, config={})
    assert [c["company_name"] for c in companies] == ["ABインベブ", "ハイネケン"]


def test_get_target_companies_includes_own_company_when_configured():
    client = FakeSupabaseClient({"competitor_companies": [
        _company("c-1", "サントリー", is_own=True),
        _company("c-2", "ABインベブ"),
    ]})
    companies = monthly.get_target_companies(
        client, config={"monthly_competitor_digest": {"include_own_company": True}})
    assert len(companies) == 2


def test_render_company_section_shows_no_change_message_when_empty():
    data = {
        "company": _company("c-1", "ABインベブ"),
        "target_changes": [], "actual_updates": [], "selected_initiatives": [],
    }
    html = monthly._render_company_section(data)
    assert "確認されませんでした" in html
    assert "ABインベブ" in html
