# -*- coding: utf-8 -*-
"""Weekly Strategic Question: シグナル収集〜戦略分析〜質問生成〜ランキング〜配信管理。

思考フロー（禁止: News→Questionの直接生成）:
    当社戦略ファクト + 外部記事 + 競合変更イベント/取組事例
    → (テーマ別) Strategic Analysis: Signal→示唆→当社戦略との関係→競合比較→対立軸
    → (上位3テーマのみ) Question Generation
    → 2段階ランキング（重複除外はdecision_dimension_keyの完全一致）
    → 1問選定・保存（review_statusは持たない。承認は週次ダイジェストの承認にバンドルする）

回答収集は strategic_question_responses.py、Decision Insight抽出は decision_insight_service.py。
"""
import base64
import hashlib
import hmac
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import sustainability_expert_common as common
from expand_sustainability_knowledge import THEMES, compute_coverage

PROMPT_VERSION = "v1"

DECISION_DIMENSION_KEYS = [
    "ambition_vs_achievability", "short_term_vs_long_term", "global_vs_local",
    "compliance_vs_leadership", "cost_vs_sustainability_impact",
    "target_vs_structural_transformation", "certification_vs_direct_intervention",
    "risk_mitigation_vs_opportunity", "internal_action_vs_rule_making",
    "speed_vs_certainty", "other",
]

COVERAGE_SCORE = {"sufficient": 100, "partial": 50, "insufficient": 10}


# ─── 機能フラグ ───────────────────────────────────────────────────
def is_strategic_question_enabled(config: dict) -> bool:
    if not common.is_enabled(config):
        return False
    cfg = (config or {}).get("strategic_question") or {}
    return bool(cfg.get("enabled"))


def _sq_config(config: dict) -> dict:
    return (config or {}).get("strategic_question") or {}


# ─── シグナル収集 ─────────────────────────────────────────────────
def collect_signals(client, config: dict, since_days_signals: int = 14,
                     since_days_competitor: int = 14, article_pool_size: int = 40) -> dict:
    """記事は既存のselector.list_weekly_picks()（重要度判定済み）を再利用する。
    競合シグナルはcompetitor_change_events（確定・検証済みのみ）とcompetitor_initiativesを
    直近since_days_competitor日から取得する（monthly_competitor_report.pyと同じクエリ形状）"""
    import sustainability_article_selector as selector
    import weekly_email_report as wer

    picks = selector.list_weekly_picks(client, since_days=since_days_signals, target_max=article_pool_size)
    articles = wer.build_articles_from_picks(client, picks)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days_competitor)).isoformat()
    changes = client.select("competitor_change_events", {
        "select": "change_event_id,company_id,record_type,after_record_id,before_record_id,"
                  "change_type,direction,summary,reasoning_summary,confidence,created_at",
        "change_status": "eq.CHANGE_CONFIRMED",
        "verification_status": "eq.VERIFIED",
        "created_at": f"gte.{cutoff}",
    })
    record_ids = list({c.get("after_record_id") or c.get("before_record_id")
                        for c in changes if (c.get("after_record_id") or c.get("before_record_id"))})
    records_by_id = {}
    if record_ids:
        records = common._select_in_chunks(client, "competitor_target_records",
                                            {"select": "record_id,themes,company_id"},
                                            "record_id", record_ids)
        records_by_id = {r["record_id"]: r for r in records}
    company_names = {}
    company_ids = list({c["company_id"] for c in changes} | {c.get("company_id") for c in []})
    if company_ids:
        companies = common._select_in_chunks(client, "competitor_companies",
                                              {"select": "company_id,company_name"},
                                              "company_id", company_ids)
        company_names = {c["company_id"]: c["company_name"] for c in companies}
    for c in changes:
        rec = records_by_id.get(c.get("after_record_id") or c.get("before_record_id")) or {}
        c["themes"] = rec.get("themes") or []
        c["company_name"] = company_names.get(c["company_id"], "")

    initiatives = client.select("competitor_initiatives", {
        "select": "initiative_id,company_id,title,summary,themes,detected_at",
        "detected_at": f"gte.{cutoff}",
    })
    for i in initiatives:
        i["company_name"] = company_names.get(i["company_id"], "") if i["company_id"] in company_names else ""

    facts = client.select("sustainability_strategy_facts", {"select": "*"})

    return {"articles": articles, "competitor_changes": changes,
            "competitor_initiatives": initiatives, "strategy_facts": facts}


def get_strategy_coverage(client) -> dict:
    return compute_coverage(client)


def cluster_candidate_signals(signals: dict, coverage: dict) -> list:
    """9テーマ別にシグナルをグルーピングする（LLM呼び出し無し）"""
    by_theme = {theme: {"theme": theme, "coverage": coverage.get(theme, "insufficient"),
                         "articles": [], "competitor_changes": [], "competitor_initiatives": [],
                         "strategy_facts": []} for theme in THEMES}

    for a in signals["articles"]:
        for theme in a.get("themes", []):
            if theme in by_theme:
                by_theme[theme]["articles"].append(a)

    for c in signals["competitor_changes"]:
        for theme in c.get("themes", []):
            if theme in by_theme:
                by_theme[theme]["competitor_changes"].append(c)

    for i in signals["competitor_initiatives"]:
        for theme in i.get("themes", []):
            if theme in by_theme:
                by_theme[theme]["competitor_initiatives"].append(i)

    for f in signals["strategy_facts"]:
        if f["theme"] in by_theme:
            by_theme[f["theme"]]["strategy_facts"].append(f)

    return list(by_theme.values())


# ─── Strategic Analysis（1テーマ1回のLLM呼び出し） ────────────────────
STRATEGIC_ANALYSIS_SYSTEM_PROMPT = """あなたは当社サステナビリティ専門家です。
今週のシグナル（外部記事・競合企業の目標変更・競合企業の取組事例）と、当社の戦略ファクト
（構造化データ。無い場合や乏しい場合はその旨を考慮すること）を1テーマ分まとめて渡します。

これはニュースの要約ではなく、当社の戦略判断に関わる論点分析です。
分析は必ず以下の順で行うこと（この順序を飛ばして直接「問い」を作ってはならない）:
1. signals: シグナルそれぞれの要点を1〜2文で要約する
2. strategic_implications: それらのシグナルが当社にとって方向性として何を意味するか（2〜4個）
3. suntory_strategy_relation: 渡された戦略ファクトのどれと、どう関係するか。
   関係するファクトが乏しい/無い場合は relation_summary にその旨を明記し、無理に関連付けない
4. competitor_comparison: 競合企業の目標・実績・取組と比較して当社はどの位置にいるか
   （提供されたデータの範囲でのみ記述し、提供されていない企業について推測しない）。
   competitor evidenceが1社・1事例しかない場合は「競合の一部では」「確認できた事例では」
   のように限定して記述し、「競合各社は〜へ移行している」のように複数社の共通傾向として
   一般化しないこと
5. strategic_tension: 当社が実際に迷い得る「対立軸」を1つ言語化する。
   decision_dimension_key は次のリストから最も近いものを1つ選ぶこと（無ければ"other"）:
   {dimension_keys}
   対立軸が無い場合（片方に明らかに分がある、単なる規制対応で選択の余地が無い等）は
   has_real_tradeoff を false にする
   tension_strengthは**0〜100の整数**で、対立の強さを表す（0.8のような0〜1の小数では
   ない）。目安: 0〜20=対立軸がほぼ無い、30〜50=一応の対立はあるが弱い、
   60〜80=実際に組織内で意見が割れる程度の対立、90〜100=非常に強い対立で
   即座に経営判断が必要なレベル。同じ値を機械的に繰り返さず、テーマごとの
   実際のシグナルの強さ・緊急性に応じて明確に差をつけること

出力はJSON1個のみ。説明文やMarkdown装飾は不要:
{{
  "signals": [{{"signal_summary": "...", "source_type": "article|competitor_change|competitor_initiative", "source_ref": "...", "theme": "..."}}],
  "strategic_implications": ["..."],
  "suntory_strategy_relation": {{"related_fact_ids": [], "relation_summary": ""}},
  "competitor_comparison": {{"summary": "", "related_company_names": [], "evidence_change_event_ids": [], "evidence_initiative_ids": []}},
  "strategic_tension": {{"tension_summary": "", "decision_dimension_key": "", "decision_dimension_label": "", "tension_strength": 0}},
  "has_real_tradeoff": true
}}
"""

STRATEGIC_ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["signals", "strategic_implications", "suntory_strategy_relation",
                 "competitor_comparison", "strategic_tension", "has_real_tradeoff"],
    "properties": {
        "signals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["signal_summary", "source_type", "source_ref", "theme"],
            "properties": {
                "signal_summary": {"type": "string"},
                "source_type": {"type": "string",
                                 "enum": ["article", "competitor_change", "competitor_initiative"]},
                "source_ref": {"type": "string"},
                "theme": {"type": "string"},
            },
        }},
        "strategic_implications": {"type": "array", "items": {"type": "string"}},
        "suntory_strategy_relation": {
            "type": "object", "additionalProperties": False,
            "required": ["related_fact_ids", "relation_summary"],
            "properties": {
                "related_fact_ids": {"type": "array", "items": {"type": "string"}},
                "relation_summary": {"type": "string"},
            },
        },
        "competitor_comparison": {
            "type": "object", "additionalProperties": False,
            "required": ["summary", "related_company_names", "evidence_change_event_ids",
                         "evidence_initiative_ids"],
            "properties": {
                "summary": {"type": "string"},
                "related_company_names": {"type": "array", "items": {"type": "string"}},
                "evidence_change_event_ids": {"type": "array", "items": {"type": "string"}},
                "evidence_initiative_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
        "strategic_tension": {
            "type": "object", "additionalProperties": False,
            "required": ["tension_summary", "decision_dimension_key", "decision_dimension_label",
                         "tension_strength"],
            "properties": {
                "tension_summary": {"type": "string"},
                "decision_dimension_key": {"type": "string", "enum": DECISION_DIMENSION_KEYS},
                "decision_dimension_label": {"type": "string"},
                "tension_strength": {"type": "integer", "minimum": 0, "maximum": 100},
            },
        },
        "has_real_tradeoff": {"type": "boolean"},
    },
}


def _build_analysis_user_prompt(bundle: dict) -> str:
    lines = [f"テーマ: {bundle['theme']}（当社戦略ファクトのカバレッジ: {bundle['coverage']}）", ""]
    lines.append(f"## 外部記事（{len(bundle['articles'])}件）")
    for a in bundle["articles"][:12]:
        lines.append(f"- [{a['article_cluster_id']}] {a['title']}: {a.get('summary_short') or a.get('importance_reason') or ''}")
    lines.append(f"\n## 競合企業の目標変更イベント（{len(bundle['competitor_changes'])}件）")
    for c in bundle["competitor_changes"][:10]:
        lines.append(f"- [{c['change_event_id']}] {c.get('company_name','')}: {c.get('change_type')}"
                     f"（{c.get('direction')}） {c.get('summary','')}")
    lines.append(f"\n## 競合企業の取組事例（{len(bundle['competitor_initiatives'])}件）")
    for i in bundle["competitor_initiatives"][:10]:
        lines.append(f"- [{i['initiative_id']}] {i.get('company_name','')}: {i['title']} - {i.get('summary','')}")
    lines.append(f"\n## 当社戦略ファクト（{len(bundle['strategy_facts'])}件）")
    for f in bundle["strategy_facts"][:20]:
        detail = f.get('strategic_direction') or f.get('policy_or_commitment') or ""
        target = f" [{f.get('target_metric')}: {f.get('target_value')} ({f.get('target_year')})]" if f.get("target_value") else ""
        lines.append(f"- [{f['fact_id']}] ({f['strategy_element_type']}) {detail}{target}")
    return "\n".join(lines)


def run_strategic_analysis(azure_client, model: str, bundle: dict) -> dict:
    user_prompt = _build_analysis_user_prompt(bundle)
    system_prompt = STRATEGIC_ANALYSIS_SYSTEM_PROMPT.format(dimension_keys="、".join(DECISION_DIMENSION_KEYS))
    result = common.call_llm_structured(azure_client, model, system_prompt, user_prompt,
                                         STRATEGIC_ANALYSIS_SCHEMA, "strategic_analysis")
    return result["data"]


# ─── Stage1スコア（LLM呼び出し前、9テーマ→上位3テーマ選定用） ─────────
def _normalize_0_100(value: float, max_value: float) -> float:
    if max_value <= 0:
        return 0.0
    return max(0.0, min(100.0, (value / max_value) * 100))


def _signal_strength_raw(bundle: dict) -> float:
    article_scores = [a.get("selector_total_score") or 0 for a in bundle["articles"]]
    change_conf = [(c.get("confidence") or 0) * 100 for c in bundle["competitor_changes"]]
    all_scores = article_scores + change_conf
    return sum(all_scores) / len(all_scores) if all_scores else 0.0


def score_stage1(analysis: dict, coverage_label: str, signal_strength_raw: float) -> float:
    tension = analysis["strategic_tension"]["tension_strength"]
    signal = _normalize_0_100(signal_strength_raw, 100)
    strategy_relevance = COVERAGE_SCORE[coverage_label]
    has_competitor_evidence = bool(analysis["competitor_comparison"]["evidence_change_event_ids"]
                                    or analysis["competitor_comparison"]["evidence_initiative_ids"])
    competitor_grounding = 100 if has_competitor_evidence else 0
    return 0.35 * tension + 0.25 * signal + 0.25 * strategy_relevance + 0.15 * competitor_grounding


# ─── Question Generation（上位3テーマのみ） ──────────────────────────
QUESTION_GENERATION_SYSTEM_PROMPT = """あなたは当社サステナビリティ専門家です。
これは社員エンゲージメント調査ではありません。組織のサステナビリティ判断が、新たに
浮上した戦略的トレードオフをどう捉えるかを明らかにするための質問を作っています。

直前の戦略分析結果（strategic_tension）をもとに、社員向けの週次戦略質問を1つ作成してください。
回答は10〜20秒で終わる単一クリックを想定します。

守るべきこと:
- 質問は当社戦略に接続していること（一般的なESG意識調査は禁止。「環境問題は重要だと
  思いますか」のような質問は不可）
- 質問文は1文、平易な言葉で。専門用語には簡単な補足を添える
- 選択肢は2〜4個。最後の1つは「どちらとも言えない/情報不足」を推奨するが必須ではない
- 各選択肢は「選んだらどうなるか」が一読でわかる短い説明を付ける
- 結論や誘導を避け、両論に配慮した中立的な書き方にする
- 分析結果に無い前提を追加しない
- 会社の正式方針や意思決定であるかのような書き方をしない
- 知識テストにしない（「PPWRについて知っていますか」等は不可）
- 質問文・選択肢では、専門用語・規制略称だけで意味を伝えないこと。専門用語が必要な場合は、
  一般社員が理解できる短い日本語説明を先に置き、略称・制度名は括弧内の補足として使う
  （例:「SBTNに基づく流域ターゲット」ではなく「水ストレスの高い流域ごとの目標（SBTN等）」、
  「PPWR」ではなく「EUの包装・包装廃棄物規則（PPWR）」）

加えて、以下の3つを0-100で自己評価すること:
- trade_off_quality: 提示した選択肢が実際に意見の割れる、意味のあるトレードオフになっているか
- neutrality: 質問文・選択肢が特定の回答へ誘導していないか（誘導が無いほど高スコア）
- answerability: 専門知識が無い社員でも10〜20秒で直感的に回答できるか

出力はJSON1個のみ:
{"title": "20文字程度の見出し", "question_text": "社員に問う1文",
 "options": [{"option_code": "A", "label": "10文字程度", "description": "選んだ場合の含意(1文)", "is_status_quo": false}],
 "framing_notes": "PMOレビュー向け: この質問の書き方で配慮した点・注意点",
 "trade_off_quality": 0, "neutrality": 0, "answerability": 0}
"""

QUESTION_GENERATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "question_text", "options", "framing_notes",
                 "trade_off_quality", "neutrality", "answerability"],
    "properties": {
        "title": {"type": "string"},
        "question_text": {"type": "string"},
        "options": {
            "type": "array", "minItems": 2, "maxItems": 4,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["option_code", "label", "description", "is_status_quo"],
                "properties": {
                    "option_code": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "is_status_quo": {"type": "boolean"},
                },
            },
        },
        "framing_notes": {"type": "string"},
        "trade_off_quality": {"type": "number", "minimum": 0, "maximum": 100},
        "neutrality": {"type": "number", "minimum": 0, "maximum": 100},
        "answerability": {"type": "number", "minimum": 0, "maximum": 100},
    },
}


def generate_question_from_analysis(azure_client, model: str, analysis: dict) -> dict | None:
    if not analysis.get("has_real_tradeoff"):
        return None
    user_prompt = "戦略分析結果:\n" + json.dumps(analysis, ensure_ascii=False, indent=2)
    result = common.call_llm_structured(azure_client, model, QUESTION_GENERATION_SYSTEM_PROMPT,
                                         user_prompt, QUESTION_GENERATION_SCHEMA, "strategic_question_generation")
    return result["data"]


# ─── 重複回避（decision_dimension_keyの完全一致） ─────────────────────
def fetch_recent_dimension_keys(client, lookback_weeks: int = 10) -> set:
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=lookback_weeks)).date().isoformat()
    try:
        rows = client.select("sustainability_strategic_questions", {
            "select": "theme,decision_dimension_key", "period_start": f"gte.{cutoff}",
        })
    except Exception:
        # テーブル未作成（migration未適用）でもevaluateのdry-runは動かせるようにする
        return set()
    return {(r["theme"], r["decision_dimension_key"]) for r in rows}


def score_stage2(theme: str, decision_dimension_key: str, stage1_score: float,
                  question: dict, recent_keys: set) -> dict:
    if (theme, decision_dimension_key) in recent_keys:
        return {"score": 0.0, "is_duplicate": True}
    score = (0.5 * stage1_score + 0.2 * question["trade_off_quality"]
             + 0.15 * question["neutrality"] + 0.15 * question["answerability"])
    return {"score": score, "is_duplicate": False}


def select_best_candidate(candidates: list) -> dict | None:
    survivors = [c for c in candidates if not c["stage2"]["is_duplicate"]]
    if not survivors:
        return None
    return max(survivors, key=lambda c: c["stage2"]["score"])


# ─── トークン導出（HMAC、サーバー秘密鍵） ─────────────────────────────
def _hmac_secret(config: dict) -> bytes:
    secret = _sq_config(config).get("hmac_secret")
    if not secret:
        raise RuntimeError("config.strategic_question.hmac_secret が未設定です")
    return secret.encode("utf-8")


def derive_recipient_key(config: dict, question_id: str, email: str) -> str:
    msg = f"recipient:{question_id}:{email.strip().lower()}".encode("utf-8")
    return hmac.new(_hmac_secret(config), msg, hashlib.sha256).hexdigest()


def derive_response_token(config: dict, delivery_id: str) -> str:
    msg = f"response:{delivery_id}".encode("utf-8")
    digest = hmac.new(_hmac_secret(config), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ─── 質問の保存・編集（review_statusは持たない。承認は週次ダイジェストにバンドル） ──
def save_draft_question(client, *, period_start, period_end, selected: dict, all_candidates: list,
                         response_window_days: int, model_deployment: str, token_usage: dict,
                         latency_ms: int) -> str:
    """selected: {"theme","analysis","question","stage1_score","stage2_score",...}
    all_candidates: evaluate相当の全候補（採用されなかった分も含む、監査用）"""
    analysis = selected["analysis"]
    question = selected["question"]
    tension = analysis["strategic_tension"]

    rows = client.insert("sustainability_strategic_questions", [{
        "period_start": period_start, "period_end": period_end,
        "theme": selected["theme"],
        "decision_dimension_key": tension["decision_dimension_key"],
        "decision_dimension_label": tension["decision_dimension_label"],
        "title": question["title"], "question_text": question["question_text"],
        "analysis_json": analysis, "candidates_json": all_candidates,
        "ranking_score": selected["stage2_score"],
        "ranking_reasons": [f"stage1={selected['stage1_score']:.1f}", f"stage2={selected['stage2_score']:.1f}"],
        "evidence_change_event_ids": analysis["competitor_comparison"]["evidence_change_event_ids"],
        "evidence_initiative_ids": analysis["competitor_comparison"]["evidence_initiative_ids"],
        "evidence_strategy_fact_ids": analysis["suntory_strategy_relation"]["related_fact_ids"],
        "model_deployment": model_deployment, "prompt_version": PROMPT_VERSION,
        "token_usage": token_usage, "latency_ms": latency_ms,
        "status": "success", "question_status": "draft", "insight_status": "pending",
        "response_window_days": response_window_days,
    }], prefer="return=representation")
    question_id = rows[0]["question_id"]

    option_rows = [{
        "question_id": question_id, "option_code": o["option_code"], "display_order": i,
        "label": o["label"], "description": o.get("description"), "is_status_quo": o.get("is_status_quo", False),
    } for i, o in enumerate(question["options"])]
    client.insert("sustainability_strategic_question_options", option_rows, prefer="return=minimal")
    return question_id


def get_question(client, question_id: str) -> dict | None:
    rows = client.select("sustainability_strategic_questions",
                          {"select": "*", "question_id": f"eq.{question_id}"})
    if not rows:
        return None
    question = rows[0]
    question["_options"] = client.select("sustainability_strategic_question_options", {
        "select": "*", "question_id": f"eq.{question_id}", "order": "display_order.asc",
    })
    return question


def list_questions(client, question_status: str = None) -> list:
    params = {"select": "*", "order": "period_start.desc"}
    if question_status:
        params["question_status"] = f"eq.{question_status}"
    questions = client.select("sustainability_strategic_questions", params)
    for q in questions:
        q["_options"] = client.select("sustainability_strategic_question_options", {
            "select": "*", "question_id": f"eq.{q['question_id']}", "order": "display_order.asc",
        })
    return questions


def save_question_edits(client, question_id: str, patch: dict) -> None:
    """PMOがweekly_email_reportの承認画面で戦略質問部分を編集した内容を反映する。
    optionsキーがあれば子テーブルも上書きする（既存を全削除して入れ直す。単純さ優先）"""
    options = patch.pop("options", None)
    if patch:
        client.update("sustainability_strategic_questions", {"question_id": f"eq.{question_id}"}, patch)
    if options is not None:
        client.delete("sustainability_strategic_question_options", {"question_id": f"eq.{question_id}"})
        option_rows = [{
            "question_id": question_id, "option_code": o["option_code"], "display_order": i,
            "label": o["label"], "description": o.get("description"), "is_status_quo": o.get("is_status_quo", False),
        } for i, o in enumerate(options)]
        client.insert("sustainability_strategic_question_options", option_rows, prefer="return=minimal")


# ─── 週次ダイジェストへの埋め込み ──────────────────────────────────
def get_draft_question_for_digest_embed(client, period_start, period_end) -> dict | None:
    """LLM呼び出し無し。question_status='draft'かつ期間一致の質問を返す"""
    rows = client.select("sustainability_strategic_questions", {
        "select": "question_id", "question_status": "eq.draft",
        "period_start": f"eq.{period_start}",
    })
    if not rows:
        return None
    return get_question(client, rows[0]["question_id"])


def mark_embedded(client, question_id: str, report_id: str) -> None:
    client.update("sustainability_strategic_questions", {"question_id": f"eq.{question_id}"},
                  {"question_status": "embedded", "embedded_in_report_id": report_id})


# ─── 配信発行（冪等）・送信管理 ────────────────────────────────────
def activate_on_digest_approval(client, config: dict, question_id: str, recipient_emails: list) -> dict:
    """冪等: 既にdeliveryがあるrecipientは既存行を使う。question_statusを'open'にし、
    opens_at/closes_atを確定する。戻り値: {email: {"delivery_id","token","send_status","links"}}"""
    question = get_question(client, question_id)
    existing = client.select("sustainability_strategic_question_deliveries", {
        "select": "*", "question_id": f"eq.{question_id}",
    })
    existing_by_key = {d["recipient_key"]: d for d in existing}

    result = {}
    for email in recipient_emails:
        recipient_key = derive_recipient_key(config, question_id, email)
        existing_delivery = existing_by_key.get(recipient_key)
        if existing_delivery:
            delivery_id = existing_delivery["delivery_id"]
            send_status = existing_delivery["send_status"]
        else:
            delivery_id = str(uuid.uuid4())
            raw_token = derive_response_token(config, delivery_id)
            client.insert("sustainability_strategic_question_deliveries", [{
                "delivery_id": delivery_id, "question_id": question_id,
                "recipient_key": recipient_key, "recipient_email": email,
                "response_token_hash": _hash_token(raw_token), "send_status": "pending",
            }], prefer="return=minimal")
            send_status = "pending"
        raw_token = derive_response_token(config, delivery_id)
        app_base_url = _sq_config(config).get("app_base_url", "").rstrip("/")
        links = [{"option_code": o["option_code"],
                  "url": f"{app_base_url}/api/strategic-questions/respond?q={question_id}&o={o['option_code']}&t={raw_token}"}
                 for o in question["_options"]]
        result[email] = {"delivery_id": delivery_id, "token": raw_token, "send_status": send_status, "links": links}

    response_window_days = question.get("response_window_days", 5)
    now = datetime.now(timezone.utc)
    client.update("sustainability_strategic_questions", {"question_id": f"eq.{question_id}"}, {
        "question_status": "open",
        "opens_at": now.isoformat(),
        "closes_at": (now + timedelta(days=response_window_days)).isoformat(),
    })
    return result


def mark_delivery_sent(client, delivery_id: str, *, success: bool, error_message: str = None) -> None:
    patch = {"send_status": "success" if success else "error"}
    if success:
        patch["sent_at"] = datetime.now(timezone.utc).isoformat()
    else:
        patch["send_error_message"] = error_message
    client.update("sustainability_strategic_question_deliveries", {"delivery_id": f"eq.{delivery_id}"}, patch)


def resend_pending_or_failed_deliveries(client, config: dict, question_id: str) -> dict:
    """send_status in ('pending','error')のdeliveryへ、同一トークンを再計算して返す
    （decision_insight_service.pyやapprove_and_send()の再実行から呼ばれる運用フォールバック）"""
    deliveries = client.select("sustainability_strategic_question_deliveries", {
        "select": "*", "question_id": f"eq.{question_id}",
    })
    question = get_question(client, question_id)
    app_base_url = _sq_config(config).get("app_base_url", "").rstrip("/")
    result = {}
    for d in deliveries:
        if d["send_status"] not in ("pending", "error"):
            continue
        raw_token = derive_response_token(config, d["delivery_id"])
        links = [{"option_code": o["option_code"],
                  "url": f"{app_base_url}/api/strategic-questions/respond?q={question_id}&o={o['option_code']}&t={raw_token}"}
                 for o in question["_options"]]
        result[d["recipient_email"]] = {"delivery_id": d["delivery_id"], "token": raw_token, "links": links}
    return result


# ─── close（時間経過、LLM無し）とInsight生成（別ライフサイクル） ────────
def close_due_questions(client) -> list:
    now = datetime.now(timezone.utc).isoformat()
    due = client.select("sustainability_strategic_questions", {
        "select": "question_id", "question_status": "eq.open", "closes_at": f"lt.{now}",
    })
    closed_ids = []
    for q in due:
        client.update("sustainability_strategic_questions", {"question_id": f"eq.{q['question_id']}"},
                      {"question_status": "closed"})
        client.update("sustainability_strategic_question_deliveries",
                      {"question_id": f"eq.{q['question_id']}"}, {"recipient_email": None})
        closed_ids.append(q["question_id"])
    return closed_ids


def generate_pending_insights(client, azure_client, model: str, config: dict) -> list:
    import decision_insight_service as insight_service
    import strategic_question_responses as sqr

    targets = client.select("sustainability_strategic_questions", {
        "select": "question_id", "question_status": "eq.closed",
        "insight_status": "in.(pending,error)",
    })
    processed = []
    for t in targets:
        question_id = t["question_id"]
        question = get_question(client, question_id)
        try:
            aggregation = sqr.aggregate_results(client, question_id)
            insight_service.distill_and_save(client, azure_client, model, question, aggregation)
            client.update("sustainability_strategic_questions", {"question_id": f"eq.{question_id}"},
                          {"insight_status": "success", "insight_error_message": None})
        except Exception as e:
            client.update("sustainability_strategic_questions", {"question_id": f"eq.{question_id}"},
                          {"insight_status": "error", "insight_error_message": f"{type(e).__name__}: {e}"})
        processed.append(question_id)
    return processed


def get_last_unincluded_insight_for_digest(client, config: dict) -> dict | None:
    import decision_insight_service as insight_service
    rows = client.select("sustainability_decision_insights", {
        "select": "*", "included_in_report_id": "is.null", "order": "observed_at.desc",
    })
    for r in rows:
        if insight_service.publishable(r, config):
            return r
    return None


def mark_insight_included(client, insight_id: str, report_id: str) -> None:
    client.update("sustainability_decision_insights", {"insight_id": f"eq.{insight_id}"},
                  {"included_in_report_id": report_id, "included_at": datetime.now(timezone.utc).isoformat()})
