"""
競合サステナビリティモニタリング: 変更検知モジュール

competitor_classifier.py が抽出した1レコード(TARGET/KPI/ACTUAL/ESG_RATING/INITIATIVE)を
過去DB(competitor_target_records / competitor_initiatives)と突き合わせ、以下を行う。

  1. 原文ハッシュによる同一性の一次判定（ハッシュ一致なら即NO_CHANGEとしてLLM呼び出しをスキップ）
  2. 構造化項目の機械比較（compare_structured_fields、LLM不要の純粋関数）
  3. 差分がある場合のみLLMによる意味的変更判定（judge_change、要件6.2のJSONを返す）
  4. competitor_target_records の新規保存・旧レコードのis_current更新
  5. competitor_change_events への保存
  6. INITIATIVE型は変更判定を行わず、新規/更新判定のみでcompetitor_initiativesへ登録

保存が必要な各処理は competitor_audit.log_action で監査ログに記録する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
import competitor_classifier as classifier  # noqa: E402
import competitor_audit as audit  # noqa: E402

TARGET_RECORD_TYPES = ("TARGET", "KPI", "ACTUAL", "ESG_RATING")


# ─── 機械比較（LLM不要） ─────────────────────────────────────────────
def compare_structured_fields(before: dict, after: dict) -> list:
    """before/afterのstructured_fieldsをフィールド単位で比較し、変更のあった項目一覧を返す。
    純粋関数（DB・LLM呼び出し無し）なのでユニットテストしやすい"""
    keys = sorted(set((before or {}).keys()) | set((after or {}).keys()))
    changed = []
    for key in keys:
        before_value = (before or {}).get(key)
        after_value = (after or {}).get(key)
        if before_value != after_value:
            changed.append({"field": key, "before": before_value, "after": after_value})
    return changed


# ─── LLMによる意味的変更判定 ─────────────────────────────────────────
CHANGE_JUDGE_SYSTEM_PROMPT = """あなたは競合企業のサステナビリティ目標・KPI・ESG評価の変更を
判定する専門家です。同一企業・同一種別の「変更前」「変更後」レコードと、機械比較で検出された
差分フィールドを踏まえ、実質的な変更かどうかを判定してください。

判定基準:
- 表現の言い換えだけで意味が変わらない場合はWORDING_ONLY
- 対象範囲・基準年・目標値等が実質的に変わっていればSUBSTANTIVE_CHANGE
- 変更前に存在しなかった新規の目標ならNEW_TARGET
- 既存目標を置き換える後継目標ならSUCCESSOR_TARGET
- 同一企業内の別目標（同時に併存しうる）ならSEPARATE_TARGET
- 目標自体が撤回・削除されたと読み取れる場合はTARGET_WITHDRAWN
- 公式ページ上の記載が単純に消えただけならREMOVED_FROM_PAGE
- 対象範囲が拡大/縮小しただけならSCOPE_EXPANDED/SCOPE_NARROWED
- 内容は同一で単に再掲載されただけならSIMPLE_REPUBLISH

directionは、変更後の内容が変更前より野心的/後退/中立のいずれかを判定する
（STRENGTHENED/WEAKENED/NEUTRAL）。数値・年限・対象範囲の変化から機械的に読み取れない場合や
判断が難しい場合は review_required=true とし、review_reasonsに理由を書くこと。

summary/reasoning_summaryの書き方（重要、経営層・サステナ担当者向けメールにそのまま使う文章）:
- target_value/boundary/scope等のデータ項目名（英語のフィールド名）は絶対に使わず、
  すべて平易な日本語の文章にする
- summaryは「(何が)、(変更前)から(変更後)へ、(どう変わったか)」を1〜2文で具体的に書く。
  例:「水資源に関する目標の対象範囲が、自社直接契約の農家からモルト供給業者とその契約農家まで
  拡大された」のように、変更前後の内容を対比させて説明すること。数値・年限が変わった場合は
  その具体的な値も文中に含める
- reasoning_summaryは、なぜその判定（同一目標か別目標か等）に至ったかの根拠を1文で

出力はJSON1個のみ:
{
  "same_entity": true,
  "change_status": "CHANGE_CONFIRMED",
  "change_type": "SCOPE_EXPANDED",
  "direction": "STRENGTHENED",
  "summary": "変更点の要約(1〜2文)",
  "reasoning_summary": "判定根拠の要約(1〜2文)",
  "confidence": 0.9,
  "review_required": false,
  "review_reasons": []
}
"""

CHANGE_JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["same_entity", "change_status", "change_type", "direction", "summary",
                 "reasoning_summary", "confidence", "review_required", "review_reasons"],
    "properties": {
        "same_entity": {"type": "boolean"},
        "change_status": {"type": "string", "enum": ["NO_CHANGE", "CHANGE_CONFIRMED"]},
        "change_type": {
            "type": "string",
            "enum": ["WORDING_ONLY", "SUBSTANTIVE_CHANGE", "NEW_TARGET", "SUCCESSOR_TARGET",
                     "SEPARATE_TARGET", "TARGET_WITHDRAWN", "REMOVED_FROM_PAGE",
                     "SCOPE_EXPANDED", "SCOPE_NARROWED", "SIMPLE_REPUBLISH"],
        },
        "direction": {"type": ["string", "null"], "enum": ["STRENGTHENED", "WEAKENED", "NEUTRAL", None]},
        "summary": {"type": "string"},
        "reasoning_summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "review_required": {"type": "boolean"},
        "review_reasons": {"type": "array", "items": {"type": "string"}},
    },
}


def judge_change(azure_client, model: str, company_name: str, record_type: str,
                  before_fields: dict, after_fields: dict, changed_fields: list) -> dict:
    user_prompt = (
        f"# 対象企業\n{company_name}\n\n# レコード種別\n{record_type}\n\n"
        f"# 変更前\n{before_fields}\n\n# 変更後\n{after_fields}\n\n"
        f"# 機械比較で検出された差分フィールド\n{changed_fields}\n"
    )
    result = common.call_llm_structured(
        azure_client, model, CHANGE_JUDGE_SYSTEM_PROMPT, user_prompt,
        CHANGE_JUDGE_SCHEMA, "CompetitorChangeJudgement")
    return result["data"]


# ─── 既存レコードとの同一性マッチング ─────────────────────────────────
# 1社が同一record_type（例:TARGET）で複数の異なるテーマの目標を同時に持つのは通常のこと
# （水・エネルギー・GHG・農業支援等）。そのため新規抽出レコードは「直近の現行版1件」とだけ
# 比較するのではなく、その企業×種別の現行版すべてを候補としてLLMに提示し、同一目標とみなせる
# ものがあるかをまず判定する。一致が無ければ、他の現行版とは無関係に新規として登録する
MATCH_EXISTING_SYSTEM_PROMPT = """あなたは競合企業のサステナビリティ目標・KPI・実績・ESG評価を
管理する専門家です。新しく取得したレコードが、その企業の既存の現行レコード一覧のいずれかと
同一の目標・指標を指しているか（表現や数値が更新されただけの同一エンティティか）を判定してください。

同一とみなす基準:
- テーマ・対象範囲・KPIの定義が本質的に同じで、数値・年限・対象範囲の広さ等が
  更新されただけとみなせる場合は同一とする
- テーマ・対象・指標の種類が異なる場合は別物とする
  （例: 水使用効率目標とGHG排出削減目標は別物、女性管理職比率とLTIRは別物）
- 迷う場合は「別物（matched_record_id=null）」を選ぶこと
  （無理に同一とみなして無関係な比較をするより、新規として登録するほうが安全）

出力はJSON1個のみ:
{"matched_record_id": "同一とみなす既存レコードのid、無ければnull", "reasoning": "判定理由(1文)"}
"""

MATCH_EXISTING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["matched_record_id", "reasoning"],
    "properties": {
        "matched_record_id": {"type": ["string", "null"]},
        "reasoning": {"type": "string"},
    },
}


def match_existing_record(azure_client, model: str, company_name: str, record_type: str,
                           candidates: list, new_extraction: dict) -> str:
    """新規抽出レコードが既存の現行版候補のいずれかと同一目標かをLLMに判定させ、
    一致するrecord_idを返す（一致なしはNone）"""
    candidates_text = "\n".join(
        f"- id={c['record_id']}: title={c.get('title') or '(不明)'} / "
        f"structured_fields={c.get('structured_fields')}"
        for c in candidates
    )
    user_prompt = (
        f"# 対象企業\n{company_name}\n\n# レコード種別\n{record_type}\n\n"
        f"# 既存の現行レコード一覧\n{candidates_text}\n\n"
        f"# 新しく取得したレコード\ntitle={new_extraction.get('title')} / "
        f"structured_fields={new_extraction.get('structured_fields')}\n"
    )
    result = common.call_llm_structured(
        azure_client, model, MATCH_EXISTING_SYSTEM_PROMPT, user_prompt,
        MATCH_EXISTING_SCHEMA, "CompetitorRecordMatch")
    return result["data"].get("matched_record_id")


# ─── DB読み書き ─────────────────────────────────────────────────────
def list_current_records(client: SupabaseClient, company_id: str, record_type: str) -> list:
    """企業×レコード種別の現行版(is_current=true)をすべて取得する。
    同一種別でも異なるテーマの目標が複数同時に存在しうるため、1件に絞らない"""
    return client.select("competitor_target_records", {
        "select": "*", "company_id": f"eq.{company_id}", "record_type": f"eq.{record_type}",
        "is_current": "eq.true", "order": "extracted_at.desc",
    })


def save_new_record(client: SupabaseClient, *, company_id: str, source_id: str, record_type: str,
                     structured_fields: dict, source_url: str, source_updated_at=None,
                     title: str = None, themes: list = None) -> dict:
    raw_hash = classifier.text_hash(str(sorted(structured_fields.items())))
    rows = client.insert("competitor_target_records", [{
        "company_id": company_id, "source_id": source_id, "record_type": record_type,
        "structured_fields": structured_fields, "raw_text_hash": raw_hash,
        "source_url": source_url, "is_current": True, "title": title, "themes": themes or [],
        "source_updated_at": source_updated_at.isoformat() if source_updated_at else None,
    }])
    return rows[0]


def supersede_record(client: SupabaseClient, old_record_id: str, new_record_id: str) -> None:
    client.update("competitor_target_records", {"record_id": f"eq.{old_record_id}"}, {
        "is_current": False, "superseded_by": new_record_id,
    })


def save_change_event(client: SupabaseClient, *, company_id: str, record_type: str,
                       before_record_id: str, after_record_id: str, judgement: dict) -> dict:
    rows = client.insert("competitor_change_events", [{
        "company_id": company_id, "record_type": record_type,
        "before_record_id": before_record_id, "after_record_id": after_record_id,
        "change_status": judgement["change_status"], "change_type": judgement["change_type"],
        "changed_fields": judgement.get("changed_fields_input", []),
        "direction": judgement.get("direction"), "summary": judgement.get("summary"),
        "reasoning_summary": judgement.get("reasoning_summary"),
        "confidence": judgement.get("confidence"),
        "review_required": judgement.get("review_required", False),
        "review_reasons": judgement.get("review_reasons", []),
        "llm_raw_output": judgement,
    }])
    return rows[0]


def save_initiative(client: SupabaseClient, *, company_id: str, source_id: str,
                     title: str, summary: str, source_url: str, themes: list = None) -> dict:
    """既存タイトルと完全一致すれば更新扱い(is_new=False)、それ以外は新規として登録する
    （骨組み段階の簡易判定。表記ゆれの類似判定は今後の拡張余地）"""
    existing = client.select("competitor_initiatives", {
        "select": "initiative_id,title", "company_id": f"eq.{company_id}",
    })
    is_new = title.strip().casefold() not in {e["title"].strip().casefold() for e in existing}
    rows = client.insert("competitor_initiatives", [{
        "company_id": company_id, "source_id": source_id, "is_new": is_new,
        "title": title, "summary": summary, "source_url": source_url, "themes": themes or [],
    }])
    return rows[0]


# ─── 1レコード分の処理（オーケストレーション） ─────────────────────────
def process_extracted_record(client: SupabaseClient, azure_client, model: str, *,
                              company: dict, source: dict, extracted: dict,
                              source_updated_at=None) -> dict:
    """competitor_classifier.classify_and_extract()が返した1レコード分を処理する。
    戻り値のkindで結果種別(no_change/new_record/change_event/initiative/skipped)を判別する"""
    record_type = extracted["record_type"]
    company_id = company["company_id"]

    if record_type == "OTHER":
        return {"kind": "skipped"}

    if record_type == "INITIATIVE":
        initiative = save_initiative(
            client, company_id=company_id, source_id=source["source_id"],
            title=extracted["title"], summary=extracted["summary"],
            source_url=source["source_url"], themes=extracted.get("themes"),
        )
        audit.log_action(client, "initiative", initiative["initiative_id"],
                          "initiative_saved", "system", {"is_new": initiative["is_new"]})
        return {"kind": "initiative", "initiative": initiative}

    after_fields = extracted["structured_fields"]
    company_name = company.get("company_name", "")
    new_hash = classifier.text_hash(str(sorted(after_fields.items())))

    # 同一種別でも異なるテーマの目標が複数同時に存在しうるため、直近1件だけでなく
    # 現行版すべてを候補にする
    candidates = list_current_records(client, company_id, record_type)

    # 既存候補のいずれかと原文ハッシュが一致すれば、その時点で「変更なし」と確定できる
    # （LLM呼び出し不要。同一内容の再掲載を毎回マッチング判定にかけるコストを避ける）
    if any(c.get("raw_text_hash") == new_hash for c in candidates):
        return {"kind": "no_change"}

    # ハッシュが一致する候補が無い場合のみ、LLMで「同一目標とみなせるものがあるか」を判定する
    before = None
    if candidates:
        matched_id = match_existing_record(azure_client, model, company_name, record_type,
                                            candidates, extracted)
        if matched_id:
            before = next((c for c in candidates if c["record_id"] == matched_id), None)

    if before is None:
        new_record = save_new_record(
            client, company_id=company_id, source_id=source["source_id"],
            record_type=record_type, structured_fields=after_fields,
            source_url=source["source_url"], source_updated_at=source_updated_at,
            title=extracted.get("title"), themes=extracted.get("themes"),
        )
        # extracted["summary"]はcompetitor_classifier.classify_and_extractがLLMで生成した
        # このレコード自体の内容要約（1〜2文、平易な日本語）。新規登録時はこれをそのまま
        # 変更内容の説明として使う（タイトルだけの空疎な定型文にしない）。既存の他テーマの
        # 現行版とは無関係な新規登録なので、それらとの比較・置き換えは一切行わない
        event = save_change_event(client, company_id=company_id, record_type=record_type,
                                   before_record_id=None, after_record_id=new_record["record_id"],
                                   judgement={
                                       "change_status": "CHANGE_CONFIRMED", "change_type": "NEW_TARGET",
                                       "direction": None,
                                       "summary": extracted.get("summary") or extracted["title"],
                                       "reasoning_summary": "既存の現行版とは同一目標とみなせなかったため新規として登録",
                                       "confidence": 1.0, "review_required": False, "review_reasons": [],
                                   })
        audit.log_action(client, "change_event", event["change_event_id"], "new_record", "system")
        return {"kind": "change_event", "record": new_record, "change_event": event}

    changed_fields = compare_structured_fields(before.get("structured_fields", {}), after_fields)
    if not changed_fields:
        return {"kind": "no_change"}

    new_record = save_new_record(
        client, company_id=company_id, source_id=source["source_id"],
        record_type=record_type, structured_fields=after_fields,
        source_url=source["source_url"], source_updated_at=source_updated_at,
        title=extracted.get("title"), themes=extracted.get("themes"),
    )

    judgement = judge_change(azure_client, model, company_name, record_type,
                              before.get("structured_fields", {}), after_fields, changed_fields)
    judgement["changed_fields_input"] = changed_fields

    if judgement.get("same_entity", True):
        supersede_record(client, before["record_id"], new_record["record_id"])

    event = save_change_event(client, company_id=company_id, record_type=record_type,
                               before_record_id=before["record_id"],
                               after_record_id=new_record["record_id"], judgement=judgement)
    audit.log_action(client, "change_event", event["change_event_id"], "change_detected", "llm",
                      {"change_type": judgement["change_type"], "confidence": judgement["confidence"]})
    return {"kind": "change_event", "record": new_record, "change_event": event}
