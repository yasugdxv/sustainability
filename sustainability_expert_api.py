"""
当社サスティナビリティ専門家MVP: 週次レポートレビュー用APIハンドラーの薄いラッパー

ビジネスロジックは weekly_email_report.py にそのまま委譲し、ここではリクエスト/レスポンスの
整形だけを行う。

注意: 記事クラスタ単位の選定・コンテンツ生成・レビューAPI（select_articles等）は、
2026-07-22の統合で週次メールのテーマ別ダイジェストに一本化されたため削除済み
（旧run.pyのDashboardHandlerからのみ呼ばれていたが、run.py自体が廃止されたため）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient  # noqa: E402
import weekly_email_report as wer  # noqa: E402

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


def review_weekly_report(report_id: str, body: dict, config: dict) -> dict:
    """POST /api/weekly-reports/{report_id}/review
    body: {"status": "approved"|"rejected"|"review_required", "reviewer_id":...,
           "reviewer_feedback":..., "draft_content": {...編集後の内容、省略可}}
    - status='review_required' + draft_content指定 → 修正のみ保存（save_draft_edits）
    - status='rejected'                            → 却下（reject_report）
    - status='approved'                             → 編集があれば先に保存し、承認して送信
                                                       （approve_and_send）"""
    client = SupabaseClient(config)
    report = wer.get_report(client, report_id)
    if report is None:
        return {"ok": False, "error": "指定されたreport_idが見つかりません"}

    status = body.get("status")
    if status and status not in WEEKLY_REVIEW_ALLOWED_STATUSES:
        return {"ok": False,
                "error": f"不正なstatus: {status}（利用可能: {', '.join(WEEKLY_REVIEW_ALLOWED_STATUSES)}）"}

    reviewer_id = body.get("reviewer_id")
    reviewer_feedback = body.get("reviewer_feedback")
    draft_content = body.get("draft_content")
    since_days = (report.get("draft_content") or {}).get("since_days", 7)

    if draft_content is not None:
        wer.save_draft_edits(client, report_id, draft_content, since_days)

    if status == "rejected":
        wer.reject_report(client, report_id, reviewer_id=reviewer_id, reviewer_feedback=reviewer_feedback)
        return {"ok": True, "review_status": "rejected"}

    if status == "approved":
        result = wer.approve_and_send(client, config, report_id, reviewer_id=reviewer_id,
                                       reviewer_feedback=reviewer_feedback)
        return {"ok": result.get("ok", False), **result}

    return {"ok": True, "review_status": "review_required"}
