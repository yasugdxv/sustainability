"""
expand_sustainability_knowledge.py の単体テスト。
LLM呼び出し・実ファイル取得を伴うcmd_fetch/cmd_apply/cmd_summarizeのCLI本体ではなく、
そこから呼ばれる純粋ロジック（fact_fingerprint、coverage判定、jsonlのupsert）を対象にする。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import expand_sustainability_knowledge as esk  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


def _fact(theme="水", strategy_element_type="numeric_target", **overrides):
    base = {
        "theme": theme, "strategy_element_type": strategy_element_type,
        "strategic_direction": "水使用量削減", "target_metric": "工場水使用量原単位",
        "target_value": "35%削減", "baseline": "2019年比", "target_year": 2030,
        "scope": "自社工場", "source_url": "https://example.com/water",
    }
    base.update(overrides)
    return base


def test_fact_fingerprint_deterministic_for_identical_facts():
    f1 = esk._fact_fingerprint(_fact())
    f2 = esk._fact_fingerprint(_fact())
    assert f1 == f2


def test_fact_fingerprint_differs_when_key_field_changes():
    f1 = esk._fact_fingerprint(_fact())
    f2 = esk._fact_fingerprint(_fact(target_year=2050))
    assert f1 != f2


def test_fact_fingerprint_ignores_non_key_fields():
    # source_title, effective_dateはfingerprint対象外（同じ戦略内容なら同一とみなす）
    f1 = esk._fact_fingerprint({**_fact(), "source_title": "A", "effective_date": "2026-01"})
    f2 = esk._fact_fingerprint({**_fact(), "source_title": "B", "effective_date": "2026-06"})
    assert f1 == f2


def test_compute_coverage_sufficient_requires_no_numeric_target():
    """カバレッジ判定はnumeric_target必須ではない。direction/policy_commitmentのみでもsufficient"""
    client = FakeSupabaseClient({
        "sustainability_strategy_facts": [
            {"theme": "人権", "strategy_element_type": "policy_commitment"},
        ],
    })
    coverage = esk.compute_coverage(client)
    assert coverage["人権"] == "sufficient"


def test_compute_coverage_partial_when_only_major_action():
    client = FakeSupabaseClient({
        "sustainability_strategy_facts": [
            {"theme": "健康", "strategy_element_type": "major_action"},
        ],
    })
    coverage = esk.compute_coverage(client)
    assert coverage["健康"] == "partial"


def test_compute_coverage_insufficient_when_no_facts():
    client = FakeSupabaseClient({"sustainability_strategy_facts": []})
    coverage = esk.compute_coverage(client)
    assert coverage["原料調達"] == "insufficient"


def test_compute_coverage_sufficient_with_numeric_target():
    client = FakeSupabaseClient({
        "sustainability_strategy_facts": [
            {"theme": "水", "strategy_element_type": "numeric_target"},
        ],
    })
    coverage = esk.compute_coverage(client)
    assert coverage["水"] == "sufficient"


def test_upsert_jsonl_doc_replaces_existing_id_not_append(tmp_path, monkeypatch):
    jsonl_path = tmp_path / "knowledge_documents.jsonl"
    monkeypatch.setattr(esk, "KNOWLEDGE_DOCUMENTS_PATH", jsonl_path)

    doc_v1 = {"id": "STRATEGY-THEME-WATER", "title": "v1", "content": "old"}
    esk._upsert_jsonl_doc(doc_v1)
    doc_v2 = {"id": "STRATEGY-THEME-WATER", "title": "v2", "content": "new"}
    esk._upsert_jsonl_doc(doc_v2)

    docs = esk._load_jsonl_docs()
    matching = [d for d in docs if d["id"] == "STRATEGY-THEME-WATER"]
    assert len(matching) == 1
    assert matching[0]["content"] == "new"


def test_upsert_jsonl_doc_keeps_other_ids_untouched(tmp_path, monkeypatch):
    jsonl_path = tmp_path / "knowledge_documents.jsonl"
    monkeypatch.setattr(esk, "KNOWLEDGE_DOCUMENTS_PATH", jsonl_path)

    esk._upsert_jsonl_doc({"id": "STRATEGY-THEME-WATER", "content": "water"})
    esk._upsert_jsonl_doc({"id": "STRATEGY-THEME-CLIMATE", "content": "climate"})
    esk._upsert_jsonl_doc({"id": "STRATEGY-THEME-WATER", "content": "water-updated"})

    docs = esk._load_jsonl_docs()
    assert len(docs) == 2
    by_id = {d["id"]: d["content"] for d in docs}
    assert by_id["STRATEGY-THEME-WATER"] == "water-updated"
    assert by_id["STRATEGY-THEME-CLIMATE"] == "climate"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
