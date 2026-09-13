"""
Phase 3: sustainability_expert_api.review_weekly_report()のAPIアイデンティティ境界テスト。

NOTE: このファイルはPhase 0の許可リストには無かった新規ファイルである
（Phase 3許可リストはsustainability_expert_api.py自体の変更のみを想定しており、
専用テストファイルの新設までは明示していなかった）。ユーザーのPhase 3仕様が明示的に
要求するAPI経路のテスト（reviewer_id単独では承認不可・原則の監査アイデンティティは
principal・principal無しはfail-closed）を検証できる場所が他に無いため、最小限の
追加として新設した。本文の詳細はPhase 3完了報告の「Scope Guard」項を参照。

実SupabaseClient/実send_emailには一切触れない（tests/_fakes.FakeSupabaseClientと
その場限りのFakeSendEmailに置き換える）。
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import identity  # noqa: E402
import sustainability_expert_api as expert_api  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


def _report_row(report_id="r-1", review_status="review_required"):
    return {
        "report_id": report_id, "period_start": "2026-07-12", "period_end": "2026-07-19",
        "subject": "件名", "html_body": "<p>本文</p>",
        "draft_content": {"articles": [], "synthesis": {}, "since_days": 7},
        "status": "success", "review_status": review_status,
        "send_mode": None, "recipients": [], "send_status": None,
    }


def _fake_config():
    return {"sustainability_expert": {"enabled": True}, "email": {"enabled": False}}


class _FakeSendEmail:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "mode": "fake"}

    def __call__(self, subject, html_body, config, to_addresses):
        self.calls.append({"subject": subject, "to_addresses": list(to_addresses or [])})
        return dict(self.result)


def _clear_identity_env():
    return patch.dict(os.environ, {
        identity.DEV_AUTH_MODE_ENV: "", identity.DEV_USER_ID_ENV: "", identity.DEV_DISPLAY_NAME_ENV: "",
    })


def _client_with_report(**kwargs):
    return FakeSupabaseClient({
        "weekly_email_reports": [_report_row(**kwargs)],
        "email_recipients": [{"email": "pmo-fixture@example.invalid", "active": True, "is_test": False,
                               "notify_weekly_digest": True}],
    })


# ─── (13) request-body reviewer_id単独 → 本番では承認不可 ──────────────────
def test_body_reviewer_id_alone_cannot_approve_in_production():
    client = _client_with_report(report_id="R13")
    fake_send = _FakeSendEmail()
    with _clear_identity_env(), \
         patch("sustainability_expert_api.SupabaseClient", return_value=client), \
         patch("weekly_email_report.send_email", fake_send):
        result = expert_api.review_weekly_report("R13", {
            "status": "approved", "reviewer_id": "sato_ryo（本人申告のみ）",
            "reviewer_feedback": {"free_text": None},
        }, _fake_config())  # principal引数を渡さない = 本番相当

    assert result.get("ok") is False
    assert fake_send.calls == []
    row = next(r for r in client.tables["weekly_email_reports"] if r["report_id"] == "R13")
    assert row["review_status"] == "review_required"  # 承認は起きていない


# ─── (14) 認証済みprincipal + 詐称されたbody reviewer_id → 監査actorはprincipal側 ──
def test_authenticated_principal_wins_over_spoofed_body_reviewer_id():
    client = _client_with_report(report_id="R14")
    fake_send = _FakeSendEmail()
    principal = identity.make_dev_principal(user_id="dev.sato", display_name="佐藤（正規レビュー担当）")

    with patch.dict(os.environ, {identity.DEV_AUTH_MODE_ENV: "true", identity.DEV_USER_ID_ENV: "dev.sato"}), \
         patch("sustainability_expert_api.SupabaseClient", return_value=client), \
         patch("weekly_email_report.send_email", fake_send):
        result = expert_api.review_weekly_report("R14", {
            "status": "approved", "reviewer_id": "PMO_Manager_Yamada",  # 詐称された自己申告値
            "reviewer_feedback": {"free_text": None},
        }, _fake_config(), principal=principal)

    assert result.get("ok") is True
    row = next(r for r in client.tables["weekly_email_reports"] if r["report_id"] == "R14")
    assert row["reviewer_id"] == "dev.sato"  # principal.user_idが監査actor
    assert row["reviewer_id"] != "PMO_Manager_Yamada"
    assert fake_send.calls  # 送信自体は行われた（正規principalによる承認のため）


# ─── (15) principal無しのAPIリクエスト → fail-closed ───────────────────────
def test_no_principal_api_request_fails_closed():
    client = _client_with_report(report_id="R15")
    fake_send = _FakeSendEmail()
    with _clear_identity_env(), \
         patch("sustainability_expert_api.SupabaseClient", return_value=client), \
         patch("weekly_email_report.send_email", fake_send):
        result = expert_api.review_weekly_report("R15", {
            "status": "approved", "reviewer_feedback": {"free_text": None},
        }, _fake_config())  # principal引数なし・本番相当

    assert result.get("ok") is False
    assert "認証" in (result.get("error") or "")
    assert fake_send.calls == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
