"""
sustainability_dashboard_core.py の Phase B（Department Intelligence AI）関連追加分の単体テスト。
- build_chat_system_prompt() の適応的Cross-domain統合指示（geo_context_blockが非空の場合のみ追記）
- _to_cross_domain_item()（Search用CrossDomainIntelligenceItemへの整形）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sustainability_dashboard_core as core  # noqa: E402
from cross_domain_intelligence_service import CrossDomainIntelligenceSource  # noqa: E402


def _prompt(geo_context_block=""):
    return core.build_chat_system_prompt(
        context_articles=[], expert_base={"company_context": {}}, retrieved_docs=[],
        geo_context_block=geo_context_block)


def test_build_chat_system_prompt_without_geo_context_has_no_synthesis_instruction():
    prompt = _prompt(geo_context_block="")
    assert "Sustainability評価" not in prompt
    assert "他部門Intelligenceを使った回答の構成" not in prompt


def test_build_chat_system_prompt_with_geo_context_includes_synthesis_instruction():
    prompt = _prompt(geo_context_block="\n# 他部門Intelligence\nダミー本文\n")
    assert "他部門Intelligenceを使った回答の構成" in prompt
    assert "ダミー本文" in prompt


def test_build_chat_system_prompt_synthesis_instruction_placed_after_geo_block():
    block = "\n# 他部門Intelligence\nダミー本文\n"
    prompt = _prompt(geo_context_block=block)
    assert prompt.index(block) < prompt.index("他部門Intelligenceを使った回答の構成")


def _source(**overrides):
    base = dict(source_department="geopolitics", source_agent="geo_intelligence",
                source_type="department_intelligence", title="政治的緊張の高まり",
                assessment="評価文", why_relevant="関連理由", political_dynamics=None,
                outlook=None, confidence="medium", as_of="2026-09-01T00:00:00+00:00",
                references=[{"title": "参考資料"}], origin="retrieved",
                source_item_id="item-1", external_call_id=None)
    base.update(overrides)
    return CrossDomainIntelligenceSource(**base)


def test_to_cross_domain_item_includes_content_and_provenance():
    item = core._to_cross_domain_item(_source())
    assert item["id"] == "item-1"
    assert item["title"] == "政治的緊張の高まり"
    assert item["assessment"] == "評価文"
    assert item["whyRelevant"] == "関連理由"
    assert item["asOf"] == "2026-09-01T00:00:00+00:00"
    assert item["provenance"]["sourceDepartment"] == "geopolitics"
    assert item["provenance"]["reuseType"] == "retrieved"


def test_to_cross_domain_item_falls_back_to_external_call_id_when_no_item_id():
    item = core._to_cross_domain_item(_source(source_item_id=None, external_call_id="call-1",
                                                origin="fresh"))
    assert item["id"] == "call-1"
    assert item["provenance"]["reuseType"] == "none"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
