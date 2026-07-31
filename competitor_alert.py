"""
競合サステナビリティモニタリング: 即時アラートモジュール

competitor_change_detector.py が保存した変更イベント(change_status='CHANGE_CONFIRMED')を
対象に、即時アラート本文を生成し、自動配信可否を判定する。

判定ルール（config.json の competitor_monitoring.auto_send_confidence_threshold）:
    change_event.review_required が false かつ confidence がしきい値以上 → 自動配信
    それ以外 → competitor_alerts に alert_status='review_required' で登録し、
              PMOレビュー（sustainability_expert_dashboard.py）を経て配信する

実際の送信は weekly_email_report.send_email() をそのまま再利用する（SMTP未設定時は
プレビューHTML保存にフォールバックする既存の挙動も踏襲）。取組事例(INITIATIVE)は
要件上、即時アラートの対象外（呼び出し側でchange_eventが無い＝そもそも対象にならない）。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient  # noqa: E402
from weekly_email_report import send_email  # noqa: E402
import competitor_audit as audit  # noqa: E402

DEFAULT_AUTO_SEND_THRESHOLD = 0.85


def _esc(text) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_auto_send_threshold(config: dict) -> float:
    return config.get("competitor_monitoring", {}).get(
        "auto_send_confidence_threshold", DEFAULT_AUTO_SEND_THRESHOLD)


def list_recipients(client: SupabaseClient, notify_field: str) -> list:
    rows = client.select("competitor_recipients", {
        "select": "email", "active": "eq.true", notify_field: "eq.true",
    })
    return [r["email"] for r in rows]


def build_alert_email(company: dict, change_event: dict) -> tuple:
    """(subject, html_body) を返す"""
    subject = f"【競合サステナ変更検知】{company.get('company_name', '?')}: {change_event.get('change_type', '')}"
    html = f"""<!DOCTYPE html>
<html><body style="font-family:'Hiragino Sans','Meiryo',sans-serif;color:#0f172a;max-width:640px;margin:0 auto;">
  <h1 style="font-size:18px;">🔔 競合サステナビリティ変更検知</h1>
  <p style="color:#64748b;font-size:12px;">{_esc(company.get('company_name'))}　{change_event.get('record_type', '')}
    　検知日時: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
  <div style="background:#fbfdff;border:1px solid #c7d6e6;border-left:5px solid #2f6fa8;
              border-radius:8px;padding:14px 15px;margin:12px 0;">
    <p style="font-weight:800;margin:0 0 8px;">{_esc(change_event.get('change_type'))}
      （方向性: {_esc(change_event.get('direction') or '—')}）</p>
    <p style="margin:0 0 8px;">{_esc(change_event.get('summary'))}</p>
    <p style="color:#5b7488;font-size:12px;margin:0;">判定根拠: {_esc(change_event.get('reasoning_summary'))}</p>
    <p style="color:#5b7488;font-size:12px;margin:6px 0 0;">confidence: {change_event.get('confidence')}</p>
  </div>
  <p style="color:#94a3b8;font-size:11px;">本メールは自動生成アラートです。要点・判定はAIによる暫定判断を含みます。</p>
</body></html>"""
    return subject, html


def save_alert(client: SupabaseClient, change_event: dict, subject: str, html_body: str,
               alert_status: str, recipients: list = None) -> dict:
    rows = client.insert("competitor_alerts", [{
        "change_event_id": change_event["change_event_id"],
        "alert_status": alert_status, "subject": subject, "html_body": html_body,
        "recipients": recipients or [],
    }])
    return rows[0]


def record_send_result(client: SupabaseClient, alert_id: str, *, recipients: list,
                        send_status: str, send_error_message: str = None) -> None:
    patch = {
        "recipients": recipients, "send_status": send_status,
        "send_error_message": send_error_message, "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    if send_status == "success":
        patch["alert_status"] = "sent"
    client.update("competitor_alerts", {"alert_id": f"eq.{alert_id}"}, patch)


def process_change_event(client: SupabaseClient, config: dict, company: dict, change_event: dict) -> dict:
    """変更イベント確定後の自動配信可否判定〜（可能なら）即時送信までを行う"""
    if change_event.get("change_status") != "CHANGE_CONFIRMED":
        return {"kind": "skipped"}

    subject, html_body = build_alert_email(company, change_event)
    threshold = get_auto_send_threshold(config)
    confidence = change_event.get("confidence") or 0
    auto_ok = (not change_event.get("review_required")) and confidence >= threshold

    if not auto_ok:
        alert = save_alert(client, change_event, subject, html_body, alert_status="review_required")
        audit.log_action(client, "alert", alert["alert_id"], "queued_for_review", "system",
                          {"confidence": confidence, "threshold": threshold})
        return {"kind": "review_required", "alert": alert}

    recipients = list_recipients(client, "notify_immediate_alert")
    alert = save_alert(client, change_event, subject, html_body, alert_status="auto_sent",
                        recipients=recipients)
    result = send_email(subject, html_body, config)
    record_send_result(client, alert["alert_id"], recipients=recipients,
                        send_status="success" if result.get("ok") else "error",
                        send_error_message=result.get("error"))
    audit.log_action(client, "alert", alert["alert_id"], "auto_sent", "system",
                      {"confidence": confidence, "threshold": threshold, "send_mode": result.get("mode")})
    return {"kind": "auto_sent", "alert": alert, "send_result": result}


def approve_and_send(client: SupabaseClient, config: dict, alert_id: str, reviewer_id: str,
                      reviewer_feedback: dict = None) -> dict:
    """PMOレビュー画面からの承認〜送信（weekly_email_report.approve_and_sendと同型）"""
    rows = client.select("competitor_alerts", {"alert_id": f"eq.{alert_id}", "limit": "1"})
    alert = rows[0] if rows else None
    if alert is None:
        return {"ok": False, "error": "alert_idが見つかりません"}
    if alert.get("alert_status") == "rejected":
        return {"ok": False, "error": "却下済みのアラートは送信できません"}

    client.update("competitor_alerts", {"alert_id": f"eq.{alert_id}"}, {
        "alert_status": "approved", "reviewer_id": reviewer_id,
        "reviewer_feedback": reviewer_feedback, "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })

    recipients = list_recipients(client, "notify_immediate_alert")
    result = send_email(alert["subject"], alert["html_body"], config)
    record_send_result(client, alert_id, recipients=recipients,
                        send_status="success" if result.get("ok") else "error",
                        send_error_message=result.get("error"))
    audit.log_action(client, "alert", alert_id, "reviewed_and_sent", reviewer_id,
                      {"send_mode": result.get("mode")})
    return {**result, "alert_status": "sent" if result.get("ok") else "approved"}


def reject_alert(client: SupabaseClient, alert_id: str, reviewer_id: str,
                  reviewer_feedback: dict = None) -> None:
    client.update("competitor_alerts", {"alert_id": f"eq.{alert_id}"}, {
        "alert_status": "rejected", "reviewer_id": reviewer_id,
        "reviewer_feedback": reviewer_feedback, "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
    audit.log_action(client, "alert", alert_id, "rejected", reviewer_id)


def list_alerts(client: SupabaseClient, alert_status: str = None) -> list:
    params = {"select": "*", "order": "created_at.desc"}
    if alert_status:
        params["alert_status"] = f"eq.{alert_status}"
    return client.select("competitor_alerts", params)
