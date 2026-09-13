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
import competitor_change_detector as change_detector  # noqa: E402
import send_state_machine as ssm  # noqa: E402


def collect_today_change_events(client: SupabaseClient) -> list:
    """本日(UTC日付)確定した変更イベントのうち、一次開示照合が完了したものを取得する。
    実績(ACTUAL)・ESG評価(ESG_RATING)の変化は日次ダイジェストの対象外
    （目標・KPIの変更のみを扱う。実績更新は月次メールでまとめて報告する）。

    2026-08-24追加: verification_status='VERIFIED'のみを対象にする（competitor_disclosure_
    verifier.py参照）。WORDING_ONLY等の非実質変更や、一次開示で確認できなかった変更
    （PARTIALLY_VERIFIED/UNVERIFIED/CONTRADICTED）は自動速報の対象から除外される
    （DBには残るため、レビュー画面やDBから個別に確認は可能）"""
    today = datetime.now(timezone.utc).date().isoformat()
    events = client.select("competitor_change_events", {
        "select": "*", "created_at": f"gte.{today}T00:00:00+00:00",
        "record_type": "in.(TARGET,KPI)",
        "verification_status": "eq.VERIFIED",
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

    # 一次開示照合結果（competitor_disclosure_verifier.py）。ダイジェスト対象は既に
    # verification_status='VERIFIED'のみに絞られているため、ここでは常に確認済みバッジ＋
    # 原典リンクのみを短く添える（メール本文を長くしないため、根拠テキストまでは載せない）
    primary_source_html = ""
    if e.get("primary_source_url"):
        doc_type = e.get("primary_source_document_type")
        link_label = f"Primary Source（{_esc(doc_type)}）" if doc_type else "Primary Source"
        primary_source_html = (
            f'<p style="margin:4px 0 0;font-size:11px;">'
            f'<span style="color:#1e7a4c;font-weight:700;">✓ 一次開示確認済み</span>　'
            f'<a href="{_esc(e["primary_source_url"])}" style="color:#064f8a;">{link_label}</a></p>'
        )

    return f"""
    <div style="background:#fbfdff;border:1px solid #c7d6e6;border-left:4px solid #2f6fa8;
                border-radius:6px;padding:10px 12px;margin:0 0 8px;">
      <div style="font-size:11.5px;font-weight:700;color:#0b3a63;margin-bottom:4px;">{badge}</div>
      <div style="font-size:12.5px;font-weight:600;color:#1e3a5f;">対象目標: {_esc(target_line)}</div>
      {diff_html}
      <p style="margin:0;font-size:13px;color:#0b1220;line-height:1.6;">{_esc(e.get('summary'))}</p>
      {footnote}
      {primary_source_html}
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


# ─── Phase 2: 競合変更通知の自動送信ゲート（機械検証可能性による事実のみの通知） ─────────
# LLMの確信度(confidence)だけでは自動送信を許可しない。イベント1件ごとにcompetitor_change_
# detector.is_machine_verifiable_change()で機械検証可能性を判定し、その日の全イベントが
# 機械検証可能な場合のみダイジェスト全体を自動送信する（1件でも意味解釈を要する変更が
# 含まれる場合は、イベントを間引かずダイジェスト全体をHuman Reviewへ回す＝安全側の設計。
# 自動送信対象外のイベントも、build_digest()自体はこれまで通り作成・保存され、
# レビュー画面には全イベントがそのまま残る＝「自動送信されない」は「破棄される」ではない）
def _evaluate_event_gate(e: dict, target_records: dict) -> dict:
    """1件の変更イベントについて、is_machine_verifiable_change()に渡すbefore/after
    （変更前後レコードのstructured_fields）とchange_metadataを組み立てて判定する"""
    before = (target_records.get(e.get("before_record_id")) or {}).get("structured_fields")
    after = (target_records.get(e.get("after_record_id")) or {}).get("structured_fields")
    llm_raw_output = e.get("llm_raw_output") or {}
    change_metadata = {
        "change_type": e.get("change_type"),
        "direction": e.get("direction"),
        "same_entity": llm_raw_output.get("same_entity"),
        "review_required": e.get("review_required"),
    }
    return change_detector.is_machine_verifiable_change(
        before, after, e.get("verification_status"), change_metadata)


def _render_auto_send_event_item(e: dict, target_records: dict) -> str:
    """自動送信メール専用の描画。summary/reasoning_summary等、LLMの解釈を含む文章は一切載せず、
    対象目標名と変更前/変更後の構造化フィールドの生の値のみを示す（Phase 2の核心原則:
    機械検証可能な事実のみを自動送信し、意味解釈は自動で断定しない）"""
    after = target_records.get(e.get("after_record_id")) or {}
    target_title = after.get("title") or "（タイトルなし）"
    themes = "・".join(after.get("themes") or [])
    target_line = f"{target_title}（{themes}）" if themes else target_title

    changed_fields = e.get("changed_fields") or []
    diff_html = ""
    if changed_fields:
        before_text, after_text = _format_before_after(changed_fields)
        diff_html = f"""
      <div style="display:flex;gap:14px;margin:6px 0;font-size:12px;color:#334155;">
        <div style="flex:1;"><b>変更前</b><br>{_esc(before_text).replace(chr(10), '<br>')}</div>
        <div style="flex:1;"><b>変更後</b><br>{_esc(after_text).replace(chr(10), '<br>')}</div>
      </div>"""

    primary_source_html = ""
    if e.get("primary_source_url"):
        primary_source_html = (
            f'<p style="margin:4px 0 0;font-size:11px;">'
            f'<a href="{_esc(e["primary_source_url"])}" style="color:#064f8a;">Primary Source</a></p>'
        )

    return f"""
    <div style="background:#fbfdff;border:1px solid #c7d6e6;border-left:4px solid #2f6fa8;
                border-radius:6px;padding:10px 12px;margin:0 0 8px;">
      <div style="font-size:12.5px;font-weight:600;color:#1e3a5f;">対象目標: {_esc(target_line)}</div>
      {diff_html}
      {primary_source_html}
    </div>"""


def _render_auto_send_company_section(company_name: str, events: list, target_records: dict) -> str:
    items_html = "".join(_render_auto_send_event_item(e, target_records) for e in events)
    return f"""
  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;margin-top:20px;">
    {_esc(company_name)}（{len(events)}件）
  </h2>
  {items_html}"""


def build_auto_send_email(events: list, companies: dict, digest_date: str, target_records: dict = None) -> tuple:
    """自動送信専用メール本文の生成（Phase 2）。この関数の呼び出し元は、events全件が
    is_machine_verifiable_change()でmachine_verifiable=Trueと判定された場合のみに限る前提。
    LLMの要約・判定根拠等の解釈文は一切含めず、変更前/変更後の構造化フィールドの生の値のみを
    通知する（意味解釈が必要な変更はbuild_email()の内容のままHuman Reviewへ回り、本関数は
    経由しない）"""
    target_records = target_records or {}
    company_count = len({e["company_id"] for e in events})
    subject = f"【競合サステナ目標変更検知（機械検証済み事実のみ）】{digest_date}: {len(events)}件（{company_count}社）"
    sections_html = "".join(
        _render_auto_send_company_section(company_name, company_events, target_records)
        for company_name, company_events in _group_by_company(events, companies)
    )
    html = f"""<!DOCTYPE html>
<html><body style="font-family:'Hiragino Sans','Meiryo',sans-serif;color:#0f172a;max-width:720px;margin:0 auto;">
  <h1 style="font-size:18px;">🔔 競合サステナビリティ 日次目標変更ダイジェスト（自動配信）</h1>
  <p style="color:#64748b;font-size:12px;">対象日: {digest_date}　検知件数: {len(events)}件<br>
    本メールは、一次開示で確認され、かつ機械的に検証可能と判定された事実（数値・年限・
    定義済みステータス値の変更）のみを通知しています。変更の意味合い（強化/後退等の解釈）は
    含みません。</p>
  {sections_html}
  <p style="color:#94a3b8;font-size:11px;margin-top:24px;">
    本メールは自動生成レポートです。
  </p>
</body></html>"""
    return subject, html


def list_recipients(client: SupabaseClient, test_mode: bool = False) -> list:
    return competitor_alert.list_recipients(client, "notify_immediate_alert", test_mode=test_mode)


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

    # Phase 2: LLMの確信度(confidence)だけでは自動送信を許可しない。イベントごとに
    # 機械検証可能性(is_machine_verifiable_change())を判定し、その日の全イベントが
    # 機械検証可能な場合のみ自動送信する（1件でも意味解釈を要する変更が含まれれば、
    # イベントを間引かずダイジェスト全体をHuman Reviewへ回す＝安全側の設計）
    gates = [_evaluate_event_gate(e, target_records) for e in events]
    auto_send_eligible = bool(events) and all(g["machine_verifiable"] for g in gates)

    digest = save_or_update_digest(
        client, digest_date=today, change_event_ids=[e["change_event_id"] for e in events],
        subject=subject, html_body=html_body, auto_send_eligible=auto_send_eligible,
        existing=existing,
    )

    reason_codes = [g["reason_code"] for g in gates]
    if auto_send_eligible:
        recipients = list_recipients(client)  # 自動配信は常に本番受信者（test_mode=False）
        # 自動送信メールは機械検証可能な事実のみを通知する専用テンプレートを使う
        # （build_email()のsummary/reasoning_summary等、LLMの解釈を含む文章は自動送信では使わない）
        auto_subject, auto_html_body = build_auto_send_email(events, companies, today, target_records)
        result = send_email(auto_subject, auto_html_body, config, recipients)
        record_send_result(
            client, digest["digest_id"], recipients=recipients,
            send_status="success" if result.get("ok") else "error",
            send_error_message=result.get("error"), send_mode=result.get("mode"),
        )
        if result.get("ok"):
            digest["review_status"] = "sent"
        audit.log_action(client, "daily_digest", digest["digest_id"], "auto_sent", "system",
                          {"event_count": len(events), "reason_codes": reason_codes})
    else:
        audit.log_action(client, "daily_digest", digest["digest_id"], "queued_for_review", "system",
                          {"event_count": len(events), "reason_codes": reason_codes})

    return digest


# ─── PMOレビュー（一覧・承認・却下） ────────────────────────────────
def list_digests(client: SupabaseClient, review_status: str = None) -> list:
    params = {"select": "*", "order": "digest_date.desc"}
    if review_status:
        params["review_status"] = f"eq.{review_status}"
    return client.select("competitor_daily_alert_digests", params)


def reject_digest(client: SupabaseClient, digest_id: str, reviewer_id: str,
                   reviewer_feedback: dict = None) -> dict:
    """既に送信済み(review_status='sent')のダイジェストは却下できないようガードする
    （send_state_machine.py: 却下ガードの共通化）"""
    rows = client.select("competitor_daily_alert_digests", {"digest_id": f"eq.{digest_id}", "limit": "1"})
    digest = rows[0] if rows else None
    if digest is None:
        return {"ok": False, "error": "digest_idが見つかりません"}
    state_machine = ssm.SendStateMachine(client, "competitor_daily_alert_digests", "digest_id")
    result = state_machine.guard_reject(
        digest, status_field="review_status", blocked_statuses=("sent",),
        patch={
            "review_status": "rejected", "reviewer_id": reviewer_id,
            "reviewer_feedback": reviewer_feedback, "reviewed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if result["ok"]:
        audit.log_action(client, "daily_digest", digest_id, "rejected", reviewer_id)
    return result


def approve_and_send(client: SupabaseClient, config: dict, digest_id: str, reviewer_id: str,
                      reviewer_feedback: dict = None, test_mode: bool = False) -> dict:
    rows = client.select("competitor_daily_alert_digests", {"digest_id": f"eq.{digest_id}", "limit": "1"})
    digest = rows[0] if rows else None
    if digest is None:
        return {"ok": False, "error": "digest_idが見つかりません"}

    # review_status='approved'への更新と「送信中」claimを1回の条件付きUPDATEで
    # 原子的に行う（send_state_machine.py: pmo-003/009の二重送信防止）
    state_machine = ssm.SendStateMachine(client, "competitor_daily_alert_digests", "digest_id")
    claim = state_machine.claim_for_sending(
        digest, status_field="review_status", blocked_statuses=("rejected", "sent"),
        extra_patch={
            "review_status": "approved", "reviewer_id": reviewer_id,
            "reviewer_feedback": reviewer_feedback, "reviewed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if not claim["ok"]:
        return claim
    digest = claim["row"]

    recipients = list_recipients(client, test_mode=test_mode)
    result = send_email(digest["subject"], digest["html_body"], config, recipients)
    record_send_result(client, digest_id, recipients=recipients,
                        send_status="success" if result.get("ok") else "error",
                        send_error_message=result.get("error"), send_mode=result.get("mode"))
    audit.log_action(client, "daily_digest", digest_id, "reviewed_and_sent", reviewer_id,
                      {"send_mode": result.get("mode")})
    return {**result, "review_status": "sent" if result.get("ok") else "approved"}
