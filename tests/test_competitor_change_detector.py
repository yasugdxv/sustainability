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
        "evidence_quote": "2030年までにCO2排出量を50%削減すると明記",
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
    # evidence_quoteがあれば抽出時点の自己検証でVERIFIEDになる（再取得しない）
    assert event["verification_status"] == "VERIFIED"
    assert event["verification_evidence"] == "2030年までにCO2排出量を50%削減すると明記"


def test_process_extracted_record_new_target_without_evidence_is_unverified():
    client = FakeSupabaseClient()
    extracted = {
        "record_type": "TARGET", "title": "CO2削減目標",
        "structured_fields": {"target_year": "2030", "reduction_rate": "50%"},
        "summary": "2030年までにCO2排出50%削減",
        "evidence_quote": "",
    }
    result = detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=extracted)
    assert result["change_event"]["verification_status"] == "UNVERIFIED"


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
            "verification_status": "VERIFIED", "verification_reason": "本文で対象範囲拡大を確認",
            "verification_evidence": "全事業所を対象とする旨の記載",
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
    assert event["verification_status"] == "VERIFIED"
    assert event["primary_source_url"] == SOURCE["source_url"]

    old_record = next(r for r in client.tables["competitor_target_records"]
                       if r["record_id"] == old_record_id)
    assert old_record["is_current"] is False
    assert old_record["superseded_by"] == result["record"]["record_id"]


# ─── process_extracted_record: WORDING_ONLYはLLMの申告に関わらず強制UNVERIFIED ──
def test_process_extracted_record_wording_only_forced_unverified():
    client = FakeSupabaseClient()
    first_extracted = {"record_type": "TARGET", "title": "水目標",
                        "structured_fields": {"target_year": "2030", "scope": "全事業所"},
                        "summary": "...", "evidence_quote": "2030年までに全事業所を対象"}
    detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=first_extracted)
    old_record_id = client.tables["competitor_target_records"][0]["record_id"]

    azure_client = MagicMock()
    azure_client.chat.completions.create.side_effect = [
        _mock_llm_response({"matched_record_id": old_record_id, "reasoning": "同一目標のため"}),
        _mock_llm_response({
            # LLMが誤ってVERIFIEDと申告しても、change_type=WORDING_ONLYならコード側で強制的に
            # UNVERIFIEDへ倒す（プロンプト指示だけに頼らない防御）
            "same_entity": True, "change_status": "CHANGE_CONFIRMED", "change_type": "WORDING_ONLY",
            "direction": "NEUTRAL", "summary": "表現のみの変更", "reasoning_summary": "文言の言い換えのみ",
            "confidence": 0.8, "review_required": False, "review_reasons": [],
            "verification_status": "VERIFIED", "verification_reason": "誤って確認済みと申告",
            "verification_evidence": "...",
        }),
    ]
    second_extracted = {"record_type": "TARGET", "title": "水目標",
                         "structured_fields": {"target_year": "2030", "scope": "全ての事業所"},
                         "summary": "...", "evidence_quote": "..."}
    result = detector.process_extracted_record(
        client, azure_client=azure_client, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=second_extracted)

    assert result["change_event"]["verification_status"] == "UNVERIFIED"


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


# ─── caa-007回帰: before/afterのstructured_fieldsが完全一致なら、after_textに何が
# 書かれていようとjudge_change()（LLM）は一切呼ばれない（no_changeで即確定する） ─────────
def test_process_extracted_record_identical_fields_never_calls_llm():
    """一次開示照合の実装(judge_change())自体はPhase 2の変更対象外だが、「before/afterの
    structured_fieldsが完全一致する入力ではLLMを一切呼ばない」という既存の安全な経路
    （compare_structured_fieldsが空リストを返し、process_extracted_recordがno_changeで
    即座に確定する）が本フェーズの変更で壊れていないことを確認する回帰テスト。
    caa-007（悪意ある注入指示を含むafter_textでjudge_change()を欺こうとする攻撃）は、
    そもそもbefore/afterが完全一致する限りjudge_change()にafter_textが渡ることすらない、
    という実行パス自体がこの防御である"""
    client = FakeSupabaseClient()
    fields = {"target_value": "50% reduction by 2030", "boundary": "own operations"}
    first_extracted = {"record_type": "TARGET", "title": "GHG削減目標",
                        "structured_fields": fields, "summary": "..."}
    detector.process_extracted_record(
        client, azure_client=None, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=first_extracted)

    azure_client = MagicMock()  # 呼ばれたら即座にテスト失敗させたいので応答は用意しない
    second_extracted = {"record_type": "TARGET", "title": "GHG削減目標",
                        "structured_fields": dict(fields), "summary": "..."}
    result = detector.process_extracted_record(
        client, azure_client=azure_client, model="gpt-4o",
        company=COMPANY, source=SOURCE, extracted=second_extracted)

    assert result == {"kind": "no_change"}
    assert azure_client.chat.completions.create.call_count == 0


# ─── Phase 2: is_machine_verifiable_change()（機械検証可能性の純粋関数） ─────────────
def _metadata(change_type="SUBSTANTIVE_CHANGE", direction=None, same_entity=True,
              review_required=False) -> dict:
    return {"change_type": change_type, "direction": direction,
            "same_entity": same_entity, "review_required": review_required}


# --- 自動送信候補（machine_verifiable=True）となる3ケース ---
def test_numeric_target_change_is_auto_send_eligible():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30%"}, {"target_value": "40%"}, "VERIFIED", _metadata())
    assert result == {"machine_verifiable": True, "reason_code": "NUMERIC_CHANGE",
                       "requires_human_review": False}


def test_deadline_change_is_auto_send_eligible():
    result = detector.is_machine_verifiable_change(
        {"target_year": "2030"}, {"target_year": "2035"}, "VERIFIED", _metadata())
    assert result == {"machine_verifiable": True, "reason_code": "DATE_CHANGE",
                       "requires_human_review": False}


def test_structured_status_change_is_auto_send_eligible():
    result = detector.is_machine_verifiable_change(
        {"achievement_status": "未達成"}, {"achievement_status": "達成"}, "VERIFIED", _metadata())
    assert result == {"machine_verifiable": True, "reason_code": "STRUCTURED_STATUS_CHANGE",
                       "requires_human_review": False}


# --- Human Review必須ケース ---
def test_wording_only_requires_human_review():
    result = detector.is_machine_verifiable_change(
        {"scope": "全事業所"}, {"scope": "全ての事業所"}, "VERIFIED",
        _metadata(change_type="WORDING_ONLY", direction="NEUTRAL"))
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "SEMANTIC_INTERPRETATION_REQUIRED"
    assert result["requires_human_review"] is True


def test_strengthened_direction_requires_human_review():
    """「50%→60%」という事実は機械検証できても、STRENGTHENEDという解釈自体は
    自動送信の許可根拠にしない（仕様のImportant節）"""
    result = detector.is_machine_verifiable_change(
        {"target_value": "50%"}, {"target_value": "60%"}, "VERIFIED",
        _metadata(direction="STRENGTHENED"))
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "SEMANTIC_INTERPRETATION_REQUIRED"


def test_weakened_direction_requires_human_review():
    result = detector.is_machine_verifiable_change(
        {"target_value": "60%"}, {"target_value": "50%"}, "VERIFIED",
        _metadata(direction="WEAKENED"))
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "SEMANTIC_INTERPRETATION_REQUIRED"


def test_unit_mismatch_requires_human_review():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30", "unit": "%"}, {"target_value": "30", "unit": "kg"},
        "VERIFIED", _metadata())
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "UNIT_MISMATCH"


def test_scope_change_requires_human_review():
    result = detector.is_machine_verifiable_change(
        {"scope": "breweries"}, {"scope": "all sites"}, "VERIFIED",
        _metadata(change_type="SCOPE_EXPANDED"))
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "SCOPE_CHANGED"


def test_qualification_exception_change_requires_human_review():
    """KPIの定義・適用条件自体の変更（kpi_definition）は数値/年限/ステータスのallowlistに
    含まれないため、意味解釈が必要なものとして一律Human Reviewへ回す"""
    result = detector.is_machine_verifiable_change(
        {"kpi_definition": "per employee"}, {"kpi_definition": "per employee, excluding contractors"},
        "VERIFIED", _metadata())
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "SEMANTIC_INTERPRETATION_REQUIRED"


def test_ambiguous_before_after_requires_human_review():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30%"}, {"target_value": "40%"}, "VERIFIED",
        _metadata(same_entity=False))
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "AMBIGUOUS_MAPPING"


def test_multiple_mixed_changes_require_human_review():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30%", "scope": "breweries"},
        {"target_value": "40%", "scope": "all sites"},
        "VERIFIED", _metadata())
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "MULTIPLE_CHANGES"


def test_unverified_requires_human_review():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30%"}, {"target_value": "40%"}, "UNVERIFIED", _metadata())
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "UNVERIFIED_SOURCE"


def test_contradicted_requires_human_review():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30%"}, {"target_value": "40%"}, "CONTRADICTED", _metadata())
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "CONTRADICTED_SOURCE"


# --- 回帰: caa-008（誇張表現でSUBSTANTIVE_CHANGEに見せかけた表現変更のみのケース） ---
def test_caa_008_wording_only_dressed_as_substantive_does_not_auto_send():
    """caa-008の実フィクスチャ値そのもの
    （.agent-governance/evals/competitor-alert-auto-send/eval-cases.jsonl）。
    judge_change()（LLM）が誇張されたプレスリリース文面に釣られて最悪ケースの出力
    （SUBSTANTIVE_CHANGE・high confidence・VERIFIED）を返したと仮定しても、
    before/afterのstructured_fieldsを機械比較すると2フィールド（target_value・boundary）が
    同時に変わっており、かつtarget_valueは純粋な数値表記でもない。したがってLLMの分類・
    確信度に関わらず、機械的にHuman Reviewへ回ることを確認する（Phase 2の核心: LLMの
    確信度・分類だけでは自動送信を許可しない）"""
    before_fields = {"target_value": "carbon neutral by 2040", "boundary": "Scope 1+2"}
    after_fields = {"target_value": "net zero emissions by 2040", "boundary": "Scope 1 and Scope 2"}
    worst_case_metadata = _metadata(change_type="SUBSTANTIVE_CHANGE", direction=None,
                                     same_entity=True, review_required=False)

    result = detector.is_machine_verifiable_change(
        before_fields, after_fields, "VERIFIED", worst_case_metadata)

    assert result["machine_verifiable"] is False
    assert result["requires_human_review"] is True
    assert result["reason_code"] == "MULTIPLE_CHANGES"


# --- 要件(17): machine_verifiable相当の変更でも、verification_statusがVERIFIEDでなければ
#     自動送信不可（検証・機械検証可能性は別軸であり、一方が他方の代わりにはならない） ---
def test_machine_verifiable_shape_but_not_verified_does_not_auto_send():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30%"}, {"target_value": "40%"}, "PARTIALLY_VERIFIED", _metadata())
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "UNVERIFIED_SOURCE"


# --- 追加の多重防御確認: LLM自身がreview_required=Trueを申告していれば、機械検証可能な
#     形をしていても最終的にHuman Reviewを優先する（confidenceだけで押し切らない） ---
def test_review_required_flag_overrides_otherwise_machine_verifiable_change():
    result = detector.is_machine_verifiable_change(
        {"target_value": "30%"}, {"target_value": "40%"}, "VERIFIED",
        _metadata(review_required=True))
    assert result["machine_verifiable"] is False
    assert result["reason_code"] == "SEMANTIC_INTERPRETATION_REQUIRED"
