"""
競合サステナビリティモニタリング: 月次メールモジュール（2026-07-27全面リニューアル版）

対象20社（競合企業。サントリー自身を含めるかはconfig.json の
monthly_competitor_digest.include_own_company で切替、既定false）について、当月の
  1. 目標・KPI・ESG評価の変更（competitor_change_events, record_type in TARGET/KPI/ESG_RATING）
  2. 実績・進捗の更新（competitor_change_events, record_type=ACTUAL）
  3. 主な取組事例3〜5件（competitor_initiatives、LLMスコアリングで選定）
を企業別セクションにまとめ、企業横断の「今月の注目動向」をLLMで生成したうえで、
monthly_reports / monthly_report_companies / monthly_report_items の3テーブルに保存する。

weekly_email_report.py / competitor_daily_digest.py と同じ
「集計→（必要ならLLM）→保存→PMOレビュー→承認後に送信」の構成を踏襲する。
メールHTMLはLLMに直接生成させず、本モジュールが構造化データからテンプレート生成する
（要件23章の方針。宛先は既存のcompetitor_recipients.notify_monthly_report=trueを再利用）。

REST API・配信予約・専用監査テーブルは今回のスコープ外（既存のcompetitor_audit_logで代替、
Streamlitからの直接呼び出しパターンを踏襲）。

使い方:
    python monthly_competitor_report.py build [--report-month 2026-07]
    python monthly_competitor_report.py send --report-id <id> [--reviewer-id <id>]
"""
import argparse
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
import competitor_classifier as classifier  # noqa: E402
from weekly_email_report import send_email  # noqa: E402
from competitor_display import (  # noqa: E402
    esc as _esc, event_badge as _event_badge, format_before_after,
)
import competitor_audit as audit  # noqa: E402

DEFAULT_INCLUDE_OWN_COMPANY = False
MIN_INITIATIVES = 3
MAX_INITIATIVES = 5

# カテゴリ自体の並び順（カテゴリ内はcompetitor_companies.display_order）
CATEGORY_ORDER = ["ビール", "蒸留酒", "清涼飲料", "日本・総合", "ボトラー", "FMCG"]

TARGET_LIKE_TYPES = ("TARGET", "KPI", "ESG_RATING")


# ─── 対象企業 ───────────────────────────────────────────────────────
def get_target_companies(client: SupabaseClient, config: dict) -> list:
    include_own = config.get("monthly_competitor_digest", {}).get(
        "include_own_company", DEFAULT_INCLUDE_OWN_COMPANY)
    companies = client.select("competitor_companies", {"select": "*"})
    if not include_own:
        companies = [c for c in companies if not c.get("is_own_company")]

    def sort_key(c):
        idx = CATEGORY_ORDER.index(c["industry_category"]) \
            if c.get("industry_category") in CATEGORY_ORDER else len(CATEGORY_ORDER)
        return (idx, c.get("display_order") or 0, c.get("company_name", ""))

    return sorted(companies, key=sort_key)


# ─── 対象期間 ───────────────────────────────────────────────────────
def resolve_period(report_month: str = None) -> tuple:
    """report_month('YYYY-MM')から(report_month, period_start, period_end, period_end_exclusive)を返す。
    未指定なら当月"""
    if report_month is None:
        report_month = datetime.now(timezone.utc).strftime("%Y-%m")
    year, month = (int(p) for p in report_month.split("-"))
    period_start = date(year, month, 1)
    last_day = monthrange(year, month)[1]
    period_end = date(year, month, last_day)
    period_end_exclusive = period_end + timedelta(days=1)
    return report_month, period_start, period_end, period_end_exclusive


# ─── 当月データの集計（企業ごと） ────────────────────────────────────
def collect_company_month_data(client: SupabaseClient, company: dict,
                                period_start: date, period_end_exclusive: date) -> dict:
    company_id = company["company_id"]
    start_iso = period_start.isoformat()
    end_iso = period_end_exclusive.isoformat()

    events = client.select("competitor_change_events", {
        "select": "*", "company_id": f"eq.{company_id}",
        "created_at": f"gte.{start_iso}T00:00:00+00:00", "order": "created_at.asc",
    })
    events = [e for e in events if e["created_at"] < f"{end_iso}T00:00:00+00:00"]

    initiatives = client.select("competitor_initiatives", {
        "select": "*", "company_id": f"eq.{company_id}",
        "detected_at": f"gte.{start_iso}T00:00:00+00:00",
    })
    initiatives = [i for i in initiatives if i["detected_at"] < f"{end_iso}T00:00:00+00:00"]

    return {
        "company": company,
        "target_changes": [e for e in events if e["record_type"] in TARGET_LIKE_TYPES],
        "actual_updates": [e for e in events if e["record_type"] == "ACTUAL"],
        "pending_reviews": [e for e in events if e.get("review_required")],
        "initiatives": initiatives,
    }


# ─── 取組事例の選定（12章のスコアリング） ────────────────────────────
INITIATIVE_SELECTION_SYSTEM_PROMPT = """あなたは競合企業のサステナビリティ動向を評価する専門家です。
当月確認された取組事例候補について、月次メールに掲載すべきかを判定してください。

評価観点（スコア0〜100）:
新規性・拡張性(対象国/地域/対象者の拡大)・規模(投資額/対象人数/面積)・具体性(数値/期限/範囲)・
戦略性(自社の主要目標との関連)・差別化・外部連携(政府/NGO/大学等)・成果(完了/次フェーズ移行)・
一次情報確度

以下は掲載しない: 過去取組の変更なし再掲、内容重複、啓発イベントのみ、方針紹介のみで活動不明確、
採用広報・ブランド広告中心、一次情報が確認できないもの

同一の取組が複数候補として渡された場合は、duplicate_of_initiative_idに統合先のidを設定すること。

出力はJSON1個のみ:
{
  "scored_initiatives": [
    {
      "initiative_id": "...",
      "selection_score": 87,
      "selection_reasons": ["新規プロジェクト", "具体的な数値目標がある"],
      "recommended_for_monthly_email": true,
      "duplicate_of_initiative_id": null
    }
  ]
}
"""

INITIATIVE_SELECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scored_initiatives"],
    "properties": {
        "scored_initiatives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["initiative_id", "selection_score", "selection_reasons",
                             "recommended_for_monthly_email", "duplicate_of_initiative_id"],
                "properties": {
                    "initiative_id": {"type": "string"},
                    "selection_score": {"type": "number", "minimum": 0, "maximum": 100},
                    "selection_reasons": {"type": "array", "items": {"type": "string"}},
                    "recommended_for_monthly_email": {"type": "boolean"},
                    "duplicate_of_initiative_id": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def score_initiatives(azure_client, model: str, company_name: str, initiatives: list) -> list:
    if not initiatives:
        return []
    items_text = "\n".join(
        f"- id={i['initiative_id']}: {i['title']} / {i.get('summary', '')}"
        for i in initiatives
    )
    user_prompt = f"# 対象企業\n{company_name}\n\n# 当月の取組事例候補\n{items_text}\n"
    result = common.call_llm_structured(
        azure_client, model, INITIATIVE_SELECTION_SYSTEM_PROMPT, user_prompt,
        INITIATIVE_SELECTION_SCHEMA, "CompetitorInitiativeSelection")
    return result["data"]["scored_initiatives"]


def select_top_initiatives(scored: list, max_n: int = MAX_INITIATIVES) -> list:
    """recommended_for_monthly_email=trueのものだけを対象に、重複統合（duplicate_of優先で
    スコアの高い方を残す）→スコア降順→上位max_n件、を行う純粋関数。3件未満でも無理に埋めない"""
    recommended = [s for s in scored if s.get("recommended_for_monthly_email")]
    deduped: dict = {}
    for s in recommended:
        key = s.get("duplicate_of_initiative_id") or s["initiative_id"]
        if key not in deduped or s["selection_score"] > deduped[key]["selection_score"]:
            deduped[key] = s
    ranked = sorted(deduped.values(), key=lambda s: s["selection_score"], reverse=True)
    return ranked[:max_n]


# ─── 企業横断の「今月の注目動向」 ─────────────────────────────────────
TRENDS_SYSTEM_PROMPT = """あなたは飲料業界のサステナビリティ動向を横断分析する専門家です。
複数の競合企業の当月の目標変更・取組事例一覧から、単なる件数集計ではなく、複数企業に共通する
傾向を3件程度抽出してください。1社だけの動きは対象外とし、必ず複数企業にまたがる傾向のみ挙げること。
該当する傾向が無ければ空配列でよい。

出力はJSON1個のみ:
{
  "cross_company_trends": [
    {
      "title": "傾向の見出し(20文字程度)",
      "summary": "傾向の説明(1〜2文)",
      "related_company_names": ["企業名1", "企業名2"],
      "related_themes": ["水"],
      "confidence": 0.9
    }
  ]
}
"""

TRENDS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cross_company_trends"],
    "properties": {
        "cross_company_trends": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "summary", "related_company_names", "related_themes", "confidence"],
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "related_company_names": {"type": "array", "items": {"type": "string"}},
                    "related_themes": {"type": "array", "items": {"type": "string", "enum": classifier.THEMES}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    },
}


def generate_cross_company_trends(azure_client, model: str, companies_data: list) -> list:
    lines = []
    for d in companies_data:
        name = d["company"]["company_name"]
        for e in d["target_changes"]:
            lines.append(f"- [{name}] 目標変更: {e.get('summary', '')}")
        for i in d["selected_initiatives"]:
            lines.append(f"- [{name}] 取組: {i.get('summary') or i.get('title')}")
    if not lines:
        return []
    result = common.call_llm_structured(
        azure_client, model, TRENDS_SYSTEM_PROMPT, "\n".join(lines),
        TRENDS_SCHEMA, "CompetitorCrossCompanyTrends")
    return result["data"]["cross_company_trends"][:3]


# ─── HTML組み立て ───────────────────────────────────────────────────
def _render_target_change(e: dict) -> str:
    badge = _event_badge(e)
    before_text, after_text = format_before_after(e.get("changed_fields") or [])
    diff_html = (
        f'<table style="width:100%;font-size:12.5px;color:#334155;border-collapse:collapse;margin-top:6px;">'
        f'<tr><td style="width:50%;vertical-align:top;padding:4px 6px 4px 0;border-top:1px solid #e2e8f0;">'
        f'<b>変更前</b><br>{_esc(before_text).replace(chr(10), "<br>")}</td>'
        f'<td style="width:50%;vertical-align:top;padding:4px 0 4px 6px;border-top:1px solid #e2e8f0;">'
        f'<b>変更後</b><br>{_esc(after_text).replace(chr(10), "<br>")}</td></tr></table>'
        if before_text else ""
    )
    return f"""
    <div style="background:#fbfdff;border:1px solid #c7d6e6;border-left:4px solid #2f6fa8;
                border-radius:6px;padding:10px 12px;margin:0 0 8px;">
      <div style="font-size:11.5px;font-weight:700;color:#0b3a63;margin-bottom:4px;">{badge}</div>
      <p style="margin:0;font-size:13px;color:#0b1220;line-height:1.6;">{_esc(e.get('summary'))}</p>
      {diff_html}
    </div>"""


def _render_actual_update(e: dict) -> str:
    badge = _event_badge(e)
    return f"""<li style="margin-bottom:4px;"><b>{badge}</b>：{_esc(e.get('summary'))}</li>"""


def _render_initiative(i: dict, no: int) -> str:
    themes = "・".join(i.get("themes") or []) or "—"
    tag = "🆕 新規" if i.get("is_new") else "🔄 更新"
    return f"""
    <div style="background:#fbfdff;border:1px solid #d9e7d3;border-left:4px solid #4a8f4a;
                border-radius:6px;padding:10px 12px;margin:0 0 8px;">
      <p style="margin:0 0 4px;font-size:13px;font-weight:700;">{no}. {_esc(i.get('title'))}　{tag}</p>
      <div style="font-size:11.5px;color:#5b7488;margin-bottom:4px;">テーマ: {_esc(themes)}</div>
      <p style="margin:0;font-size:13px;color:#0b1220;line-height:1.6;">{_esc(i.get('summary'))}</p>
    </div>"""


def _render_company_section(data: dict) -> str:
    company = data["company"]
    name = company.get("company_name", "?")
    category = company.get("industry_category", "")
    target_changes = data["target_changes"]
    actual_updates = data["actual_updates"]
    selected_initiatives = data["selected_initiatives"]

    update_parts = []
    if target_changes:
        update_parts.append(f"目標変更{len(target_changes)}件")
    if actual_updates:
        update_parts.append(f"実績更新{len(actual_updates)}件")
    if selected_initiatives:
        update_parts.append(f"主要取組{len(selected_initiatives)}件")
    update_summary = "・".join(update_parts) if update_parts else "更新なし"

    if target_changes:
        target_html = "".join(_render_target_change(e) for e in target_changes)
    else:
        target_html = ('<p style="color:#64748b;font-size:13px;">当月、公式開示上の目標・KPI・'
                        'ESG評価の変更は確認されませんでした。</p>')

    actual_html = ""
    if actual_updates:
        actual_html = f"""
    <h3 style="font-size:13.5px;margin:14px 0 6px;">実績・進捗の更新</h3>
    <ul style="margin:0;padding-left:1.2em;font-size:13px;">{"".join(_render_actual_update(e) for e in actual_updates)}</ul>"""

    if selected_initiatives:
        initiatives_html = "".join(_render_initiative(i, n) for n, i in enumerate(selected_initiatives, 1))
    else:
        initiatives_html = ('<p style="color:#64748b;font-size:13px;">当月、掲載基準を満たす新規・'
                              '更新取組は確認されませんでした。</p>')

    return f"""
  <div style="margin-top:26px;padding-top:14px;border-top:2px solid #1d4ed8;">
    <h2 style="font-size:16px;margin:0 0 2px;">{_esc(name)}</h2>
    <p style="color:#64748b;font-size:12px;margin:0 0 10px;">
      カテゴリ：{_esc(category)}　当月の更新：{_esc(update_summary)}
    </p>
    <h3 style="font-size:13.5px;margin:0 0 6px;">目標・KPI・ESG評価の変更</h3>
    {target_html}
    {actual_html}
    <h3 style="font-size:13.5px;margin:14px 0 6px;">今月の主な取組</h3>
    {initiatives_html}
  </div>"""


def _render_trends(trends: list) -> str:
    if not trends:
        return ""
    items = "".join(
        f'<li style="margin-bottom:8px;"><b>{_esc(t["title"])}</b>：{_esc(t["summary"])}</li>'
        for t in trends
    )
    return f"""
  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;">今月の注目動向</h2>
  <ul style="font-size:13px;line-height:1.6;padding-left:20px;">{items}</ul>"""


def build_subject(report_month: str, target_change_count: int) -> str:
    year, month = report_month.split("-")
    base = f"【サステナビリティ競合動向｜{year}年{int(month)}月】"
    if target_change_count:
        return f"{base}\n目標変更{target_change_count}件／競合の主要取組"
    return f"{base}\n競合の目標変更・主要取組まとめ"


def build_email(report_month: str, period_start: date, period_end: date,
                companies_data: list, trends: list) -> tuple:
    target_change_total = sum(len(d["target_changes"]) for d in companies_data)
    updated_company_count = sum(
        1 for d in companies_data
        if d["target_changes"] or d["actual_updates"] or d["selected_initiatives"]
    )
    subject = build_subject(report_month, target_change_total)

    summary_html = f"""
  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;">今月のサマリー</h2>
  <table style="width:100%;font-size:13px;border-collapse:collapse;">
    <tr><td style="padding:3px 0;color:#64748b;">監視対象企業</td><td style="padding:3px 0;text-align:right;">{len(companies_data)}社</td></tr>
    <tr><td style="padding:3px 0;color:#64748b;">当月更新企業</td><td style="padding:3px 0;text-align:right;">{updated_company_count}社</td></tr>
    <tr><td style="padding:3px 0;color:#64748b;">目標・KPI変更</td><td style="padding:3px 0;text-align:right;">{target_change_total}件</td></tr>
    <tr><td style="padding:3px 0;color:#64748b;">実績・進捗更新</td><td style="padding:3px 0;text-align:right;">{sum(len(d["actual_updates"]) for d in companies_data)}件</td></tr>
    <tr><td style="padding:3px 0;color:#64748b;">掲載取組事例</td><td style="padding:3px 0;text-align:right;">{sum(len(d["selected_initiatives"]) for d in companies_data)}件</td></tr>
  </table>"""

    sections_html = "".join(_render_company_section(d) for d in companies_data)

    html = f"""<!DOCTYPE html>
<html><body style="font-family:'Hiragino Sans','Meiryo',sans-serif;color:#0f172a;max-width:720px;margin:0 auto;">
  <h1 style="font-size:18px;">🌏 サステナビリティ競合動向</h1>
  <p style="color:#64748b;font-size:12px;">
    対象期間：{period_start.strftime('%Y年%m月%d日')}〜{period_end.strftime('%m月%d日')}
    監視対象：競合{len(companies_data)}社
  </p>
  {summary_html}
  {_render_trends(trends)}
  {sections_html}
  <p style="color:#94a3b8;font-size:11px;margin-top:24px;">
    本メールは自動生成レポートです。要点・示唆はAIによる暫定判断を含み、会社の正式見解ではありません。
  </p>
</body></html>"""
    return subject.replace("\n", "："), html


def build_text(report_month: str, companies_data: list, trends: list) -> str:
    """簡易プレーンテキスト版"""
    lines = [f"サステナビリティ競合動向 {report_month}", ""]
    for t in trends:
        lines.append(f"■ {t['title']}: {t['summary']}")
    lines.append("")
    for d in companies_data:
        name = d["company"].get("company_name", "?")
        lines.append(f"[{name}]")
        for e in d["target_changes"]:
            lines.append(f"  - 目標変更: {e.get('summary', '')}")
        for e in d["actual_updates"]:
            lines.append(f"  - 実績更新: {e.get('summary', '')}")
        for i in d["selected_initiatives"]:
            lines.append(f"  - 取組: {i.get('title')}: {i.get('summary', '')}")
        lines.append("")
    return "\n".join(lines)


# ─── DB読み書き ─────────────────────────────────────────────────────
def list_recipients(client: SupabaseClient) -> list:
    rows = client.select("competitor_recipients", {
        "select": "email", "active": "eq.true", "notify_monthly_report": "eq.true",
    })
    return [r["email"] for r in rows]


def _get_existing_report(client: SupabaseClient, report_month: str) -> dict:
    rows = client.select("monthly_reports", {"report_month": f"eq.{report_month}", "limit": "1"})
    return rows[0] if rows else None


def save_report(client: SupabaseClient, *, report_month: str, period_start: date, period_end: date,
                 subject: str, summary_json: dict, trends: list, html_body: str, text_body: str,
                 model_deployment: str, existing: dict = None) -> dict:
    patch = {
        "subject": subject, "status": "GENERATED", "summary_json": summary_json,
        "cross_company_trends_json": trends, "html_body": html_body, "text_body": text_body,
        "model_deployment": model_deployment, "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "system",
    }
    if existing:
        client.update("monthly_reports", {"report_id": f"eq.{existing['report_id']}"}, patch)
        return {**existing, **patch}
    rows = client.insert("monthly_reports", [{
        "report_month": report_month, "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(), **patch,
    }])
    return rows[0]


def save_report_companies(client: SupabaseClient, report_id: str, companies_data: list) -> None:
    client.delete("monthly_report_companies", {"monthly_report_id": f"eq.{report_id}"})
    rows = [{
        "monthly_report_id": report_id, "company_id": d["company"]["company_id"],
        "display_order": d["company"].get("display_order") or 0, "include_flag": True,
        "target_change_count": len(d["target_changes"]),
        "esg_rating_update_count": len([e for e in d["target_changes"] if e["record_type"] == "ESG_RATING"]),
        "actual_update_count": len(d["actual_updates"]),
        "initiative_count": len(d["selected_initiatives"]),
    } for d in companies_data]
    if rows:
        client.insert("monthly_report_companies", rows, prefer="return=minimal")


def save_report_items(client: SupabaseClient, report_id: str, companies_data: list) -> None:
    client.delete("monthly_report_items", {"monthly_report_id": f"eq.{report_id}"})
    # PostgRESTの一括insertは全行のキー構成が揃っている必要があるため、
    # item_type別に異なる列(change_event_id/entity_id/source_url等)も
    # 常にNoneで明示し、行ごとにキーの過不足が出ないようにする
    def _item_row(company_id, item_type, order, *, change_event_id=None, entity_id=None,
                  generated_title=None, generated_summary=None, source_url=None):
        return {
            "monthly_report_id": report_id, "company_id": company_id, "item_type": item_type,
            "change_event_id": change_event_id, "entity_id": entity_id,
            "display_order": order, "include_flag": True,
            "generated_title": generated_title, "generated_summary": generated_summary,
            "source_url": source_url,
        }

    rows = []
    for d in companies_data:
        company_id = d["company"]["company_id"]
        for order, e in enumerate(d["target_changes"]):
            rows.append(_item_row(company_id, "TARGET_CHANGE", order,
                                    change_event_id=e["change_event_id"],
                                    generated_title=_event_badge(e), generated_summary=e.get("summary")))
        for order, e in enumerate(d["actual_updates"]):
            rows.append(_item_row(company_id, "ACTUAL_UPDATE", order,
                                    change_event_id=e["change_event_id"],
                                    generated_title=_event_badge(e), generated_summary=e.get("summary")))
        for order, i in enumerate(d["selected_initiatives"]):
            rows.append(_item_row(
                company_id, "INITIATIVE" if i.get("is_new") else "INITIATIVE_UPDATE", order,
                entity_id=i["initiative_id"], generated_title=i.get("title"),
                generated_summary=i.get("summary"), source_url=i.get("source_url")))
        for order, e in enumerate(d["pending_reviews"]):
            rows.append(_item_row(company_id, "PENDING_REVIEW", order,
                                    change_event_id=e["change_event_id"],
                                    generated_title=_event_badge(e), generated_summary=e.get("reasoning_summary")))
    if rows:
        client.insert("monthly_report_items", rows, prefer="return=minimal")


def list_reports(client: SupabaseClient, status: str = None) -> list:
    params = {"select": "*", "order": "report_month.desc"}
    if status:
        params["status"] = f"eq.{status}"
    return client.select("monthly_reports", params)


def reject_report(client: SupabaseClient, report_id: str, reviewer_id: str,
                   reviewer_feedback: dict = None) -> None:
    client.update("monthly_reports", {"report_id": f"eq.{report_id}"}, {
        "status": "CANCELLED", "reviewed_by": reviewer_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
    audit.log_action(client, "monthly_report", report_id, "rejected", reviewer_id)


def approve_and_send(client: SupabaseClient, config: dict, report_id: str, reviewer_id: str,
                      reviewer_feedback: dict = None) -> dict:
    rows = client.select("monthly_reports", {"report_id": f"eq.{report_id}", "limit": "1"})
    report = rows[0] if rows else None
    if report is None:
        return {"ok": False, "error": "report_idが見つかりません"}
    if report.get("status") == "CANCELLED":
        return {"ok": False, "error": "却下済みのレポートは送信できません"}

    now_iso = datetime.now(timezone.utc).isoformat()
    client.update("monthly_reports", {"report_id": f"eq.{report_id}"}, {
        "status": "APPROVED", "reviewed_by": reviewer_id, "reviewed_at": now_iso,
        "approved_by": reviewer_id, "approved_at": now_iso,
    })

    recipients = list_recipients(client)
    result = send_email(report["subject"], report["html_body"], config)
    patch = {
        "send_mode": result.get("mode"), "recipients": recipients,
        "send_status": "success" if result.get("ok") else "error",
        "send_error_message": result.get("error"), "sent_at": now_iso,
    }
    if result.get("ok"):
        patch["status"] = "SENT"
    client.update("monthly_reports", {"report_id": f"eq.{report_id}"}, patch)
    audit.log_action(client, "monthly_report", report_id, "reviewed_and_sent", reviewer_id,
                      {"send_mode": result.get("mode")})
    return {**result, "status": patch.get("status", "APPROVED")}


# ─── フェーズ1: ドラフト生成 ─────────────────────────────────────────
def build_draft(report_month: str = None) -> str:
    config = load_config()
    if not common.is_enabled(config):
        print("SUSTAINABILITY_EXPERT_ENABLED が無効です。処理を行わず終了します。")
        return None

    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if not azure_client:
        print("Azure OpenAI / OpenAI のAPIキーが設定されていません")
        return None

    report_month, period_start, period_end, period_end_exclusive = resolve_period(report_month)

    existing = _get_existing_report(client, report_month)
    if existing and existing.get("status") in ("APPROVED", "SENT"):
        print(f"{report_month}分は既に{existing['status']}のため再生成しません（report_id={existing['report_id']}）")
        return existing["report_id"]

    companies = get_target_companies(client, config)
    print(f"対象企業: {len(companies)}社　対象期間: {period_start}〜{period_end}")

    companies_data = []
    for company in companies:
        data = collect_company_month_data(client, company, period_start, period_end_exclusive)
        scored = score_initiatives(azure_client, model, company["company_name"], data["initiatives"])
        by_id = {i["initiative_id"]: i for i in data["initiatives"]}
        top = select_top_initiatives(scored)
        data["selected_initiatives"] = [
            {**by_id[s["initiative_id"]], **s} for s in top if s["initiative_id"] in by_id
        ]
        companies_data.append(data)
        print(f"  {company['company_name']}: 目標変更{len(data['target_changes'])}件 "
              f"実績更新{len(data['actual_updates'])}件 取組候補{len(data['initiatives'])}件"
              f"→掲載{len(data['selected_initiatives'])}件")

    print("企業横断の注目動向を生成中...")
    trends = generate_cross_company_trends(azure_client, model, companies_data)

    subject, html_body = build_email(report_month, period_start, period_end, companies_data, trends)
    text_body = build_text(report_month, companies_data, trends)

    summary_json = {
        "monitored_companies": len(companies_data),
        "updated_companies": sum(
            1 for d in companies_data
            if d["target_changes"] or d["actual_updates"] or d["selected_initiatives"]),
        "target_change_count": sum(len(d["target_changes"]) for d in companies_data),
        "actual_update_count": sum(len(d["actual_updates"]) for d in companies_data),
        "initiative_count": sum(len(d["selected_initiatives"]) for d in companies_data),
        "review_pending_count": sum(len(d["pending_reviews"]) for d in companies_data),
    }

    report = save_report(
        client, report_month=report_month, period_start=period_start, period_end=period_end,
        subject=subject, summary_json=summary_json, trends=trends, html_body=html_body,
        text_body=text_body, model_deployment=model, existing=existing,
    )
    save_report_companies(client, report["report_id"], companies_data)
    save_report_items(client, report["report_id"], companies_data)

    print(f"ドラフトを保存しました（report_id={report['report_id']}, status=GENERATED）。"
          f"送信は行いません。sustainability_expert_dashboard.py でレビュー・承認してください。")
    return report["report_id"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="競合サステナ月次メール（ドラフト生成→レビュー→送信）")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_build = subparsers.add_parser("build", help="ドラフトを生成しGENERATEDで保存する（送信しない）")
    p_build.add_argument("--report-month", default=None, help="'YYYY-MM'形式。未指定なら当月")

    p_send = subparsers.add_parser("send", help="承認済みドラフトを送信する（運用フォールバック）")
    p_send.add_argument("--report-id", required=True)
    p_send.add_argument("--reviewer-id", default="cli")

    cli_args = parser.parse_args()
    if cli_args.cmd == "build":
        build_draft(report_month=cli_args.report_month)
    else:
        _config = load_config()
        _client = SupabaseClient(_config)
        _result = approve_and_send(_client, _config, cli_args.report_id, reviewer_id=cli_args.reviewer_id)
        print(_result)
