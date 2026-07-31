"""
競合サステナビリティモニタリング: 日次アラートダイジェスト

変更イベント(competitor_change_events)を1件ごとに即時配信するのではなく、1日1回・
その日確定した変更イベントをまとめて1通のメールにし、PMOがレビューしてから送信する
（weekly_email_report.py / monthly_competitor_report.py と同じレビューゲート構成）。

ただし、その日の全イベントが確信度高くレビュー不要と判定された場合は、従来通り
自動配信する（auto_send_eligible）。取組事例(initiative)はダイジェスト対象外
（月次メールのみに含める、変更なし）。

competitor_crawler.py の main() 末尾から1回だけ build_digest() を呼ぶ想定
（変更イベント1件ごとの即時配信呼び出しcompetitor_alert.process_change_eventは廃止）。
"""
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient  # noqa: E402
from weekly_email_report import send_email  # noqa: E402
from competitor_display import (  # noqa: E402
    esc as _esc, event_badge as _event_badge,
    format_before_after as _format_before_after, format_fields as _format_fields,
)
import competitor_alert  # noqa: E402
import competitor_audit as audit  # noqa: E402


def collect_today_change_events(client: SupabaseClient) -> list:
    """本日(UTC日付)確定した変更イベントを取得する。
    実績(ACTUAL)・ESG評価(ESG_RATING)の変化は日次ダイジェストの対象外
    （目標・KPIの変更のみを扱う。実績更新は月次メールでまとめて報告する）"""
    today = datetime.now(timezone.utc).date().isoformat()
    events = client.select("competitor_change_events", {
        "select": "*", "created_at": f"gte.{today}T00:00:00+00:00",
        "record_type": "in.(TARGET,KPI)",
        "order": "created_at.asc",
    })
    return [e for e in events if e["created_at"][:10] == today]


def _fetch_target_records(client: SupabaseClient, events: list) -> dict:
    """変更イベントのafter_record_id/before_record_idが指す目標レコード
    （タイトル・テーマ・構造化フィールド）をまとめて取得する"""
    ids = {e["after_record_id"] for e in events if e.get("after_record_id")}
    ids |= {e["before_record_id"] for e in events if e.get("before_record_id")}
    if not ids:
        return {}
    rows = client.select("competitor_target_records", {
        "select": "record_id,title,themes,structured_fields",
        "record_id": f"in.({','.join(ids)})",
    })
    return {r["record_id"]: r for r in rows}


def _get_existing_digest(client: SupabaseClient, digest_date: str) -> dict:
    rows = client.select("competitor_daily_alert_digests", {
        "digest_date": f"eq.{digest_date}", "limit": "1",
    })
    return rows[0] if rows else None


def _render_event_item(e: dict, target_records: dict) -> str:
    """1件の変更イベントを、生データの羅列ではなく総括した説明文として描画する。
    どの目標が対象か(タイトル・テーマ)と、変更前/変更後（新規目標の場合はその内容）を
    項目別に示したうえで、summary(LLMによる1〜2文の要約)を添える"""
    badge = _event_badge(e)
    after = target_records.get(e.get("after_record_id")) or {}
    target_title = after.get("title") or "（タイトルなし）"
    themes = "・".join(after.get("themes") or [])
    target_line = f"{target_title}（{themes}）" if themes else target_title

    changed_fields = e.get("changed_fields") or []
    if changed_fields:
        before_text, after_text = _format_before_after(changed_fields)
        diff_html = f"""
      <div style="display:flex;gap:14px;margin:6px 0;font-size:12px;color:#334155;">
        <div style="flex:1;"><b>変更前</b><br>{_esc(before_text).replace(chr(10), '<br>')}</div>
        <div style="flex:1;"><b>変更後</b><br>{_esc(after_text).replace(chr(10), '<br>')}</div>
      </div>"""
    else:
        fields_text = _format_fields(after.get("structured_fields") or {})
        diff_html = (f'<div style="margin:6px 0;font-size:12px;color:#334155;">'
                     f'<b>目標内容</b><br>{_esc(fields_text).replace(chr(10), "<br>")}</div>'
                     if fields_text else "")

    footnote = (f'<p style="margin:6px 0 0;font-size:11.5px;color:#64748b;">'
                f'判定根拠: {_esc(e.get("reasoning_summary"))}</p>'
                if e.get("review_required") and e.get("reasoning_summary") else "")

    return f"""
    <div style="background:#fbfdff;border:1px solid #c7d6e6;border-left:4px solid #2f6fa8;
                border-radius:6px;padding:10px 12px;margin:0 0 8px;">
      <div style="font-size:11.5px;font-weight:700;color:#0b3a63;margin-bottom:4px;">{badge}</div>
      <div style="font-size:12.5px;font-weight:600;color:#1e3a5f;">対象目標: {_esc(target_line)}</div>
      {diff_html}
      <p style="margin:0;font-size:13px;color:#0b1220;line-height:1.6;">{_esc(e.get('summary'))}</p>
      {footnote}
    </div>"""


def _render_company_section(company_name: str, events: list, target_records: dict) -> str:
    items_html = "".join(_render_event_item(e, target_records) for e in events)
    return f"""
  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;margin-top:20px;">
    {_esc(company_name)}（{len(events)}件）
  </h2>
  {items_html}"""


def _group_by_company(events: list, companies: dict) -> list:
    """company_idでグルーピングし、企業名の昇順で(company_name, events)のリストを返す"""
    grouped = defaultdict(list)
    for e in events:
        grouped[e["company_id"]].append(e)
    company_names = {cid: companies.get(cid, {}).get("company_name", "?") for cid in grouped}
    return sorted(
        ((company_names[cid], evs) for cid, evs in grouped.items()),
        key=lambda pair: pair[0],
    )


def build_email(events: list, companies: dict, digest_date: str, target_records: dict = None) -> tuple:
    """(subject, html_body) を返す。対象企業ごとにパートを分け、各変更について
    変更前→変更後を端的に示す構成にする"""
    target_records = target_records or {}
    company_count = len({e["company_id"] for e in events})
    subject = f"【競合サステナ目標変更検知】{digest_date}: {len(events)}件（{company_count}社）"
    sections_html = "".join(
        _render_company_section(company_name, company_events, target_records)
        for company_name, company_events in _group_by_company(events, companies)
    )
    html = f"""<!DOCTYPE html>
<html><body style="font-family:'Hiragino Sans','Meiryo',sans-serif;color:#0f172a;max-width:720px;margin:0 auto;">
  <h1 style="font-size:18px;">🔔 競合サステナビリティ 日次目標変更ダイジェスト</h1>
  <p style="color:#64748b;font-size:12px;">対象日: {digest_date}　検知件数: {len(events)}件（目標・KPI変更のみ、実績更新は対象外）</p>
  {sections_html}
  <p style="color:#94a3b8;font-size:11px;margin-top:24px;">
    本メールは自動生成レポートです。要点・判定はAIによる暫定判断を含みます。
  </p>
</body></html>"""
    return subject, html


def list_recipients(client: SupabaseClient) -> list:
    return competitor_alert.list_recipients(client, "notify_immediate_alert")


def save_or_update_digest(client: SupabaseClient, *, digest_date: str, change_event_ids: list,
                           subject: str, html_body: str, auto_send_eligible: bool,
                           existing: dict = None) -> dict:
    patch = {
        "change_event_ids": change_event_ids, "subject": subject, "html_body": html_body,
        "auto_send_eligible": auto_send_eligible,
    }
    if existing:
        client.update("competitor_daily_alert_digests", {"digest_id": f"eq.{existing['digest_id']}"}, patch)
        return {**existing, **patch}
    rows = client.insert("competitor_daily_alert_digests", [{
        "digest_date": digest_date, "review_status": "review_required", **patch,
    }])
    return rows[0]


def record_send_result(client: SupabaseClient, digest_id: str, *, recipients: list,
                        send_status: str, send_error_message: str = None,
                        send_mode: str = None) -> None:
    patch = {
        "recipients": recipients, "send_status": send_status,
        "send_error_message": send_error_message, "send_mode": send_mode,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    if send_status == "success":
        patch["review_status"] = "sent"
    client.update("competitor_daily_alert_digests", {"digest_id": f"eq.{digest_id}"}, patch)


def build_digest(client: SupabaseClient, config: dict) -> dict:
    """本日分の変更イベントを集計し、ダイジェストを作成・（可能なら）自動送信する。
    対象イベントが0件ならNoneを返す（何も作らない）。
    既に当日分がapproved/sentならそのまま返し、再構築しない"""
    today = datetime.now(timezone.utc).date().isoformat()
    existing = _get_existing_digest(client, today)
    if existing and existing.get("review_status") in ("approved", "sent"):
        return existing

    events = collect_today_change_events(client)
    if not events:
        return None

    companies = {c["company_id"]: c for c in client.select("competitor_companies", {"select": "*"})}
    target_records = _fetch_target_records(client, events)
    subject, html_body = build_email(events, companies, today, target_records)

    threshold = competitor_alert.get_auto_send_threshold(config)
    auto_send_eligible = all(
        (not e.get("review_required")) and (e.get("confidence") or 0) >= threshold
        for e in events
    )

    digest = save_or_update_digest(
        client, digest_date=today, change_event_ids=[e["change_event_id"] for e in events],
        subject=subject, html_body=html_body, auto_send_eligible=auto_send_eligible,
        existing=existing,
    )

    if auto_send_eligible:
        recipients = list_recipients(client)
        result = send_email(subject, html_body, config)
        record_send_result(
            client, digest["digest_id"], recipients=recipients,
            send_status="success" if result.get("ok") else "error",
            send_error_message=result.get("error"), send_mode=result.get("mode"),
        )
        if result.get("ok"):
            digest["review_status"] = "sent"
        audit.log_action(client, "daily_digest", digest["digest_id"], "auto_sent", "system",
                          {"event_count": len(events), "threshold": threshold})
    else:
        audit.log_action(client, "daily_digest", digest["digest_id"], "queued_for_review", "system",
                          {"event_count": len(events)})

    return digest


# ─── PMOレビュー（一覧・承認・却下） ────────────────────────────────
def list_digests(client: SupabaseClient, review_status: str = None) -> list:
    params = {"select": "*", "order": "digest_date.desc"}
    if review_status:
        params["review_status"] = f"eq.{review_status}"
    return client.select("competitor_daily_alert_digests", params)


def reject_digest(client: SupabaseClient, digest_id: str, reviewer_id: str,
                   reviewer_feedback: dict = None) -> None:
    client.update("competitor_daily_alert_digests", {"digest_id": f"eq.{digest_id}"}, {
        "review_status": "rejected", "reviewer_id": reviewer_id,
        "reviewer_feedback": reviewer_feedback, "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
    audit.log_action(client, "daily_digest", digest_id, "rejected", reviewer_id)


def approve_and_send(client: SupabaseClient, config: dict, digest_id: str, reviewer_id: str,
                      reviewer_feedback: dict = None) -> dict:
    rows = client.select("competitor_daily_alert_digests", {"digest_id": f"eq.{digest_id}", "limit": "1"})
    digest = rows[0] if rows else None
    if digest is None:
        return {"ok": False, "error": "digest_idが見つかりません"}
    if digest.get("review_status") == "rejected":
        return {"ok": False, "error": "却下済みのダイジェストは送信できません"}

    client.update("competitor_daily_alert_digests", {"digest_id": f"eq.{digest_id}"}, {
        "review_status": "approved", "reviewer_id": reviewer_id,
        "reviewer_feedback": reviewer_feedback, "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })

    recipients = list_recipients(client)
    result = send_email(digest["subject"], digest["html_body"], config)
    record_send_result(client, digest_id, recipients=recipients,
                        send_status="success" if result.get("ok") else "error",
                        send_error_message=result.get("error"), send_mode=result.get("mode"))
    audit.log_action(client, "daily_digest", digest_id, "reviewed_and_sent", reviewer_id,
                      {"send_mode": result.get("mode")})
    return {**result, "review_status": "sent" if result.get("ok") else "approved"}
