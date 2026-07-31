import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import competitor_change_detector as detector  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

COMPANY = {"company_id": "c-1", "company_name": "A飲料（ダミー）"}
SOURCE = {"source_id": "s-1", "source_url": "https://example.com/dummy-sustainability-a"}


def _mock_llm_response(data: dict):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=json.dumps(data, ensure_ascii=False)))]
    resp.usage = MagicMock(prompt_tokens=40, completion_tokens=20, total_tokens=60,
                            completion_tokens_details=MagicMock(reasoning_tokens=0))
    return resp


# ─── compare_structured_fields（純粋関数） ───────────────────────────
def test_compare_structured_fields_detects_changes():
    before = {"target_year": "2030", "scope": "breweries"}
    after = {"target_year": "2030", "scope": "all sites"}
    changed = detector.compare_structured_fields(before, after)
    assert changed == [{"field": "scope", "before": "breweries", "after": "all sites"}]


def test_compare_structured_fields_no_changes():
    fields = {"target_year": "2030", "scope": "breweries"}
    assert detector.compare_structured_fields(fields, dict(fields)) == []


# ─── process_extracted_record: 新規レコード（LLM呼び出し不要） ─────────
def test_process_extracted_record_new_target_creates_record_and_change_event():
    client = FakeSupabaseClient()
    extracted = {
        "record_type": "TARGET", "title": "CO2削減目標",
        "structured_fields": {"target_year": "2030", "reduction_rate": "50%"},
        "summary": "2030年までにCO2排出50%削減",
    }

    result = detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=extracted)

    assert result["kind"] == "change_event"
    assert result["record"]["is_current"] is True
    event = result["change_event"]
    assert event["change_status"] == "CHANGE_CONFIRMED"
    assert event["change_type"] == "NEW_TARGET"
    assert event["before_record_id"] is None
    # classifierが生成した内容要約をそのまま使う（タイトルだけの空疎な定型文にしない）
    assert event["summary"] == "2030年までにCO2排出50%削減"


# ─── process_extracted_record: ハッシュ一致で変更なし ───────────────────
def test_process_extracted_record_no_change_when_hash_matches():
    client = FakeSupabaseClient()
    fields = {"target_year": "2030", "reduction_rate": "50%"}
    extracted = {"record_type": "TARGET", "title": "CO2削減目標",
                 "structured_fields": fields, "summary": "..."}

    first = detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=extracted)
    assert first["kind"] == "change_event"

    second = detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=extracted)
    assert second["kind"] == "no_change"
    assert len(client.tables.get("competitor_target_records", [])) == 1


# ─── process_extracted_record: 実質的な変更（LLM判定を経由） ────────────
def test_process_extracted_record_change_confirmed_calls_llm_and_supersedes():
    client = FakeSupabaseClient()
    first_extracted = {"record_type": "TARGET", "title": "CO2削減目標",
                        "structured_fields": {"target_year": "2030", "scope": "breweries"},
                        "summary": "..."}
    detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=first_extracted)
    old_record_id = client.tables["competitor_target_records"][0]["record_id"]

    # 1回目の呼び出し(match_existing_record)は既存レコードとの同一性判定、
    # 2回目(judge_change)は変更内容の判定。side_effectで呼び出し順に応答を返す
    azure_client = MagicMock()
    azure_client.chat.completions.create.side_effect = [
        _mock_llm_response({"matched_record_id": old_record_id, "reasoning": "同一目標のため"}),
        _mock_llm_response({
            "same_entity": True, "change_status": "CHANGE_CONFIRMED", "change_type": "SCOPE_EXPANDED",
            "direction": "STRENGTHENED", "summary": "対象範囲が拡大された",
            "reasoning_summary": "target_yearは同一だがscopeが拡大", "confidence": 0.92,
            "review_required": False, "review_reasons": [],
        }),
    ]

    second_extracted = {"record_type": "TARGET", "title": "CO2削減目標",
                         "structured_fields": {"target_year": "2030", "scope": "all sites"},
                         "summary": "..."}
    result = detector.process_extracted_record(
        client, azure_client=azure_client, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=second_extracted)

    assert result["kind"] == "change_event"
    event = result["change_event"]
    assert event["change_type"] == "SCOPE_EXPANDED"
    assert event["direction"] == "STRENGTHENED"
    assert event["confidence"] == 0.92

    old_record = next(r for r in client.tables["competitor_target_records"]
                       if r["record_id"] == old_record_id)
    assert old_record["is_current"] is False
    assert old_record["superseded_by"] == result["record"]["record_id"]


# ─── process_extracted_record: 無関係な既存レコードと誤って比較しない ─────
def test_process_extracted_record_unrelated_topic_is_registered_as_new_not_compared():
    """企業が既に別テーマの現行目標(水効率)を持っていても、無関係な新規目標(エネルギー効率)を
    その水効率目標と比較して「別目標」扱いにせず、素直に新規登録する。既存の水効率目標も
    is_current=trueのまま残り、複数テーマの現行目標が共存できることを確認する"""
    client = FakeSupabaseClient()
    water_target = {"record_type": "TARGET", "title": "水使用効率目標",
                     "structured_fields": {"scope": "water efficiency", "target_year": "2030"},
                     "summary": "水使用効率目標"}
    detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=water_target)

    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response(
        {"matched_record_id": None, "reasoning": "テーマが異なるため別物"})

    energy_target = {"record_type": "TARGET", "title": "エネルギー効率目標",
                      "structured_fields": {"scope": "energy efficiency", "target_year": "2030"},
                      "summary": "エネルギー効率目標"}
    result = detector.process_extracted_record(
        client, azure_client=azure_client, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=energy_target)

    assert result["kind"] == "change_event"
    event = result["change_event"]
    assert event["change_type"] == "NEW_TARGET"
    assert event["before_record_id"] is None

    water_record = next(r for r in client.tables["competitor_target_records"]
                         if r["title"] == "水使用効率目標")
    assert water_record["is_current"] is True
    assert len(client.tables["competitor_target_records"]) == 2


# ─── process_extracted_record: 取組事例の新規/更新判定 ──────────────────
def test_process_extracted_record_initiative_new_then_update():
    client = FakeSupabaseClient()
    extracted = {"record_type": "INITIATIVE", "title": "再生可能エネルギー導入実証",
                 "structured_fields": {}, "summary": "..."}

    first = detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=extracted)
    assert first["kind"] == "initiative"
    assert first["initiative"]["is_new"] is True

    second = detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=extracted)
    assert second["initiative"]["is_new"] is False


def test_process_extracted_record_skips_other():
    client = FakeSupabaseClient()
    extracted = {"record_type": "OTHER", "title": "", "structured_fields": {}, "summary": ""}
    result = detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=extracted)
    assert result == {"kind": "skipped"}
