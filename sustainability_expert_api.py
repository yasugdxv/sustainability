"""
当社サステナビリティ専門家MVP: 週次レポートレビュー用APIハンドラーの薄いラッパー

ビジネスロジックは weekly_email_report.py にそのまま委譲し、ここではリクエスト/レスポンスの
整形だけを行う。

注意: 記事クラスタ単位の選定・コンテンツ生成・レビューAPI（select_articles等）は、
2026-07-22の統合で週次メールのテーマ別ダイジェストに一本化されたため削除済み
（旧run.pyのDashboardHandlerからのみ呼ばれていたが、run.py自体が廃止されたため）。

Phase 3（承認アイデンティティ境界）: review_weekly_report()は、リクエストボディの
自己申告reviewer_idを認証/監査アイデンティティとして扱わない。まだEntra ID接続は
実装していない（既存のAPIフレームワークから実際の認証コンテキストを受け取る手段が
無い）ため、本番相当ではidentity.resolve_principal()が常にfail-closedし、
DEV AUTHモードでのみ明示的なprincipal注入（principal引数）を許可する。
body["reviewer_id"]は表示/コメント用メタデータとしてのみ残り、DBのreviewer_id列
（監査記録）にはidentity.audit_reviewer_id(principal)（＝principal.user_id）の値だけを
書き込む。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient  # noqa: E402
import weekly_email_report as wer  # noqa: E402
import identity  # noqa: E402

WEEKLY_REVIEW_ALLOWED_STATUSES = ("review_required", "approved", "rejected")


def list_weekly_reports(status: str, config: dict) -> dict:
    """GET /api/weekly-reports?status=review_required"""
    client = SupabaseClient(config)
    reports = wer.list_reports(client, review_status=status or None)
    return {"ok": True, "reports": reports}


def get_weekly_report(report_id: str, config: dict) -> dict:
    """GET /api/weekly-reports/{report_id}"""
    client = SupabaseClient(config)
    report = wer.get_report(client, report_id)
    if report is None:
        return {"ok": False, "error": "指定されたreport_idが見つかりません"}
    return {"ok": True, "report": report}


def review_weekly_report(report_id: str, body: dict, config: dict,
                          principal: identity.AuthenticatedPrincipal = None) -> dict:
    """POST /api/weekly-reports/{report_id}/review
    body: {"status": "approved"|"rejected"|"review_required", "reviewer_id":...,
           "reviewer_feedback":..., "draft_content": {...編集後の内容、省略可}}
    - status='review_required' + draft_content指定 → 修正のみ保存（save_draft_edits）
    - status='rejected'                            → 却下（reject_report）
    - status='approved'                             → 編集があれば先に保存し、承認して送信
                                                       （approve_and_send）

    principal: 呼び出し元（将来Entra ID連携済みになるAPI層）が解決した認証済み
    AuthenticatedPrincipal。まだEntra ID接続は未実装のため、このパラメータは
    DEV AUTHモードでのFakePrincipal注入専用（identity.resolve_principal参照）。
    本番相当（DEV AUTHモード無効）ではこの値に関わらずfail-closedする。

    body["reviewer_id"]は、監査アイデンティティとしては一切使わない
    （＝reject_report/approve_and_sendへ渡すreviewer_idは常に
    identity.audit_reviewer_id(principal)の値のみ）。表示/コメント用メタデータとして
    reviewer_feedbackに埋め込むだけにとどめる。"""
    client = SupabaseClient(config)
    report = wer.get_report(client, report_id)
    if report is None:
        return {"ok": False, "error": "指定されたreport_idが見つかりません"}

    status = body.get("status")
    if status and status not in WEEKLY_REVIEW_ALLOWED_STATUSES:
        return {"ok": False,
                "error": f"不正なstatus: {status}（利用可能: {', '.join(WEEKLY_REVIEW_ALLOWED_STATUSES)}）"}

    try:
        resolved_principal = identity.resolve_principal(dev_principal=principal)
    except identity.AuthenticationError as e:
        return {"ok": False, "error": f"認証エラー: 認証済みレビュー担当者を解決できません（{e}）"}

    draft_content = body.get("draft_content")
    since_days = (report.get("draft_content") or {}).get("since_days", 7)

    # body["reviewer_id"]は表示専用のコメント扱いに格下げする（監査アイデンティティには
    # 絶対に使わない）。reviewer_feedbackが辞書であれば、自己申告値をそこに残すだけ。
    reviewer_feedback = body.get("reviewer_feedback")
    self_reported_reviewer_id = body.get("reviewer_id")
    if self_reported_reviewer_id and isinstance(reviewer_feedback, dict):
        reviewer_feedback = {**reviewer_feedback, "self_reported_reviewer_id": self_reported_reviewer_id}

    if draft_content is not None:
        if not identity.can_review(resolved_principal):
            return {"ok": False, "error": "この操作を行う権限がありません（reviewerロールが必要です）"}
        wer.save_draft_edits(client, report_id, draft_content, since_days)

    if status == "rejected":
        if not identity.can_approve(resolved_principal):
            return {"ok": False, "error": "この操作を行う権限がありません（reviewerロールが必要です）"}
        wer.reject_report(client, report_id, reviewer_id=identity.audit_reviewer_id(resolved_principal),
                           reviewer_feedback=reviewer_feedback)
        return {"ok": True, "review_status": "rejected"}

    if status == "approved":
        if not identity.can_approve(resolved_principal):
            return {"ok": False, "error": "この操作を行う権限がありません（reviewerロールが必要です）"}
        result = wer.approve_and_send(client, config, report_id,
                                       reviewer_id=identity.audit_reviewer_id(resolved_principal),
                                       reviewer_feedback=reviewer_feedback)
        return {"ok": result.get("ok", False), **result}

    return {"ok": True, "review_status": "review_required"}
