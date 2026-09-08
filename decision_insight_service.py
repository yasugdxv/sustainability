# -*- coding: utf-8 -*-
"""Weekly Strategic Question の回答結果を「組織内の判断傾向」に蒸留し、
Sustainability Expert AIチャットから参照可能にする。

重要なガードレール: ここで生成する内容は必ず organizational_decision_signal
（観測された傾向）として扱い、official_strategy/approved_policyと混同しない。
回答結果を「正式な会社方針」として提示してはならない。
"""
import json
from datetime import datetime, timedelta, timezone

import sustainability_expert_common as common

PROMPT_VERSION = "v1"

DISTILL_SYSTEM_PROMPT = """あなたは社内アンケート結果を要約する専門家です。
先週の戦略質問への回答（選択肢の分布と自由記述コメント）を、社内の「観測された傾向」として
構造化してください。これは会社の正式な意思決定や方針ではなく、あくまで参考情報です。

- observed_tendencyは集計結果として書く（「〜すべきである」という規範的な書き方をしない）
- representative_reasoningは、コメント群から読み取れる主な理由づけのパターンを2〜4個、
  **原文を引用せず、意味だけを抽出して一般化した文で書く**こと。
  例: 原文「欧州だけなら対応すべきだが、ASEANまで一律にやるのは投資効率が悪いと思う」
      → 「規制・リスクの高い地域への重点対応を支持し、グローバル一律対応には
         投資効率上の懸念がある」
  特定個人の言い回し・部署名・エピソードが残る書き方をしない
- 回答数が10件未満、または回答率が15%未満の場合はconfidenceを0.5以下にする

出力はJSON1個のみ:
{"observed_tendency": "...", "reasoning_summary": "...",
 "representative_reasoning": ["..."], "confidence": 0.0}
"""

DISTILL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["observed_tendency", "reasoning_summary", "representative_reasoning", "confidence"],
    "properties": {
        "observed_tendency": {"type": "string"},
        "reasoning_summary": {"type": "string"},
        "representative_reasoning": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def _build_distill_user_prompt(question: dict, aggregation: dict) -> str:
    lines = [
        f"テーマ: {question['theme']}", f"対立軸: {question['decision_dimension_label']}",
        f"質問: {question['question_text']}", "",
        f"回答数: {aggregation['total_responses']} / 配信数: {aggregation['delivery_count']}"
        f" / 回答率: {aggregation['response_rate']*100:.1f}%", "",
        "選択肢分布:",
    ]
    for o in aggregation["by_option"]:
        lines.append(f"  {o['option_code']} {o['label']}: {o['count']}件（{o['pct']}%）")
    lines.append("\nコメント:")
    for c in aggregation["comments"]:
        lines.append(f"  - {c}")
    return "\n".join(lines)


def distill_and_save(client, azure_client, model: str, question: dict, aggregation: dict) -> str:
    """MVPでは自動supersedeしない（既存is_current行の更新は行わず、単純にINSERTする）"""
    user_prompt = _build_distill_user_prompt(question, aggregation)
    result = common.call_llm_structured(azure_client, model, DISTILL_SYSTEM_PROMPT, user_prompt,
                                         DISTILL_SCHEMA, "decision_insight_extraction")
    data = result["data"]

    rows = client.insert("sustainability_decision_insights", [{
        "question_id": question["question_id"],
        "decision_dimension_key": question["decision_dimension_key"],
        "decision_dimension_label": question["decision_dimension_label"],
        "theme": question["theme"],
        "observed_tendency": data["observed_tendency"],
        "distribution_json": {"by_option": aggregation["by_option"], "response_rate": aggregation["response_rate"]},
        "response_count": aggregation["total_responses"],
        "delivery_count": aggregation["delivery_count"],
        "reasoning_summary": data["reasoning_summary"],
        "representative_reasoning": data["representative_reasoning"],
        "confidence": data["confidence"],
        "is_current": True, "superseded_by": None,  # MVPでは自動supersedeしない（明示的にデフォルト値を書く）
        "guardrail_label": "社内の回答者から観測された傾向であり、会社の正式方針ではありません",
        "model_deployment": model, "prompt_version": PROMPT_VERSION,
        "token_usage": result.get("token_usage"), "latency_ms": result.get("latency_ms"),
    }], prefer="return=representation")
    return rows[0]["insight_id"]


def publishable(insight: dict, config: dict) -> bool:
    """件数(既定10)と回答率(既定0.15)のAND条件。どちらも満たさなければ
    「先週の結果」掲載・チャット参照のどちらからも除外する"""
    cfg = (config or {}).get("strategic_question") or {}
    min_count = cfg.get("min_responses_for_publish", 10)
    min_rate = cfg.get("min_response_rate_for_publish", 0.15)
    response_count = insight.get("response_count", 0)
    delivery_count = insight.get("delivery_count", 0)
    rate = (response_count / delivery_count) if delivery_count else 0.0
    return response_count >= min_count and rate >= min_rate


def get_recent_insights_for_chat(client, config: dict, themes: list = None) -> list:
    cfg = (config or {}).get("strategic_question") or {}
    max_age_days = cfg.get("chat_max_age_days", 180)
    max_items = cfg.get("chat_max_items", 3)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()

    params = {"select": "*", "is_current": "eq.true", "observed_at": f"gte.{cutoff}",
              "order": "observed_at.desc"}
    rows = client.select("sustainability_decision_insights", params)
    if themes:
        rows = [r for r in rows if r["theme"] in themes]
    rows = [r for r in rows if publishable(r, config)]
    return rows[:max_items]


def build_decision_insight_chat_block(client, config: dict, themes: list = None) -> str:
    """既存のcompetitor_block/geo_context_blockと同じ「空なら何もしない」規約。
    build_chat_system_prompt()の末尾に追加する"""
    insights = get_recent_insights_for_chat(client, config, themes=themes)
    if not insights:
        return ""

    lines = ["# 組織内の判断傾向（参考情報。正式な会社方針ではありません）",
             "以下は、社内アンケートで観測された傾向であり、会社としての決定事項ではありません。"
             "参考情報として扱い、断定的に引用しないでください。", ""]
    for ins in insights:
        observed_date = (ins.get("observed_at") or "")[:10]
        lines.append(f"- [{ins['theme']}] {ins['decision_dimension_label']}（{observed_date}観測）: "
                     f"{ins['observed_tendency']}（{ins['guardrail_label']}）")
        if ins.get("reasoning_summary"):
            lines.append(f"  理由の要約: {ins['reasoning_summary']}")
    return "\n".join(lines)
