"""
競合サステナビリティモニタリング機能の監査ログ共通ヘルパー。

判定・配信・レビューの各処理から呼び出し、competitor_audit_log に1行ずつ記録する。
挿入失敗（テーブル未作成等）でパイプライン全体を止めないよう、失敗時は警告printのみ行う
（既存のweekly_email_report.save_draft_reportと同じ方針）。
"""
from article_crawler import SupabaseClient  # noqa: E402


def log_action(client: SupabaseClient, entity_type: str, entity_id: str, action: str,
                actor: str, detail: dict = None) -> None:
    try:
        client.insert("competitor_audit_log", [{
            "entity_type": entity_type,
            "entity_id": entity_id,
            "action": action,
            "actor": actor,
            "detail": detail or {},
        }])
    except Exception as e:
        print(f"  [警告] competitor_audit_logへの記録に失敗しました"
              f"（sql/2026-07-27_competitor_monitoring_schema.sql の適用が必要な可能性があります）: "
              f"{type(e).__name__}: {e}")
