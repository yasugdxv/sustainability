"""
Phase 3: identity.py（AuthenticatedPrincipal / 承認アイデンティティ境界）のテスト。

Baseline v1で確認された監査整合性の欠陥（pmo-002/007/008: 空欄reviewer_idが
"unknown"に丸められる、"PMO_Manager_Yamada"のような自由記述の名前がそのまま
承認者として記録される、モジュールごとに挙動がバラバラ）が、本フェーズで
導入したidentity.AuthenticatedPrincipal境界によって塞がれていることを確認する。
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import identity  # noqa: E402

DEV_ENV = {
    identity.DEV_AUTH_MODE_ENV: "true",
    identity.DEV_USER_ID_ENV: "dev.tanaka",
    identity.DEV_DISPLAY_NAME_ENV: "田中（開発用）",
}


def _clear_identity_env():
    """3つのDEV AUTH関連環境変数をすべて未設定にする（本番相当を再現する）"""
    return patch.dict(os.environ, {
        identity.DEV_AUTH_MODE_ENV: "", identity.DEV_USER_ID_ENV: "", identity.DEV_DISPLAY_NAME_ENV: "",
    })


# ─── (1) 有効な認証済みprincipal → レビュー/承認可能 ────────────────────────
def test_valid_principal_can_review_and_approve():
    principal = identity.make_dev_principal(user_id="dev.sato", roles=(identity.ROLE_REVIEWER,))
    assert identity.can_review(principal) is True
    assert identity.can_approve(principal) is True


# ─── (2) principalが無い → 拒否 ────────────────────────────────────────────
def test_missing_principal_cannot_review_or_approve():
    assert identity.can_review(None) is False
    assert identity.can_approve(None) is False
    with pytest.raises(identity.AuthenticationError):
        identity.audit_reviewer_id(None)


# ─── (3) 自由記述のみのreviewer ID → 本番では拒否 ───────────────────────────
def test_free_text_reviewer_id_rejected_in_production():
    with _clear_identity_env():
        assert identity.is_dev_auth_mode_enabled() is False
        with pytest.raises(identity.AuthenticationError):
            # Streamlit/CLIの自由記述reviewer_idをdev_principalとして渡そうとしても、
            # 本番相当（DEV AUTHモード無効）では常に拒否される
            identity.resolve_principal(dev_principal=None)


# ─── (4) reviewer_id == "unknown" → 拒否 ───────────────────────────────────
def test_reviewer_id_unknown_literal_is_rejected():
    with pytest.raises(ValueError):
        identity.AuthenticatedPrincipal(
            user_id="unknown", display_name="unknown", roles=(identity.ROLE_REVIEWER,))
    with pytest.raises(ValueError):
        identity.make_dev_principal(user_id="unknown")


# ─── (5) 架空の特権者名 "PMO_Manager_Yamada" → 本番では使用不可 ─────────────
def test_fake_privileged_reviewer_name_not_usable_in_production():
    with _clear_identity_env():
        with pytest.raises(identity.AuthenticationError):
            # 呼び出し側が"PMO_Manager_Yamada"のようなdisplay_nameを持つdev_principalを
            # 組み立てて渡そうとしても、DEV AUTHモードが無効な本番では拒否される
            fake = identity.AuthenticatedPrincipal(
                user_id="PMO_Manager_Yamada", display_name="PMO_Manager_Yamada",
                roles=(identity.ROLE_REVIEWER,), authentication_source="dev_fake")
            identity.resolve_principal(dev_principal=fake)


# ─── (6) ロール不足 → 承認不可 ──────────────────────────────────────────────
def test_insufficient_role_cannot_approve():
    principal = identity.AuthenticatedPrincipal(
        user_id="dev.viewer", display_name="閲覧のみ", roles=(), authentication_source="dev_fake")
    assert identity.can_approve(principal) is False
    assert identity.can_review(principal) is False


# ─── (7) 適切なロール → 承認可能 ────────────────────────────────────────────
def test_valid_role_can_approve():
    principal = identity.make_dev_principal(user_id="dev.approver")
    assert identity.can_approve(principal) is True


# ─── (8) 明示的なDEV AUTHモード + FakePrincipal → 許可 ─────────────────────
def test_dev_auth_mode_with_fake_principal_allowed():
    with patch.dict(os.environ, DEV_ENV):
        dev_principal = identity.dev_principal_from_env()
        assert dev_principal is not None
        assert dev_principal.authentication_source == "dev_fake"
        resolved = identity.resolve_principal(dev_principal=dev_principal)
        assert resolved.user_id == "dev.tanaka"


# ─── (9) 本番 + FakePrincipal → 拒否 ────────────────────────────────────────
def test_production_with_fake_principal_rejected():
    with _clear_identity_env():
        fake = identity.make_dev_principal(user_id="dev.tanaka")
        with pytest.raises(identity.AuthenticationError):
            identity.resolve_principal(dev_principal=fake)


# ─── (10) 認証解決失敗 → FakePrincipalへの自動フォールバック禁止 ───────────
def test_auth_resolution_failure_never_auto_falls_back_to_fake():
    # DEV AUTHモードは有効だが、DEV_USER_ID_ENVが未設定 → dev_principal_from_env()はNone
    with patch.dict(os.environ, {identity.DEV_AUTH_MODE_ENV: "true", identity.DEV_USER_ID_ENV: ""}):
        assert identity.dev_principal_from_env() is None
        with pytest.raises(identity.AuthenticationError):
            identity.resolve_principal(dev_principal=identity.dev_principal_from_env())

    # 本番相当でdev_principal=Noneでも、暗黙にFakePrincipalへフォールバックしない
    with _clear_identity_env():
        with pytest.raises(identity.AuthenticationError):
            identity.resolve_principal(dev_principal=None)


# ─── audit_reviewer_id: 常にprincipal.user_idを返す（監査アイデンティティの単一の出所） ──
def test_audit_reviewer_id_returns_principal_user_id_only():
    principal = identity.make_dev_principal(user_id="dev.kimura", display_name="木村（自由記述の別名）")
    assert identity.audit_reviewer_id(principal) == "dev.kimura"


# ─── (11) Streamlit: 自由記述reviewer_id入力欄がアイデンティティの出所から除外されている ──
def test_dashboard_source_excludes_free_text_reviewer_id_from_identity():
    """sustainability_expert_dashboard.py が、"レビュー担当者ID"自由記述入力の値を
    reviewer_id/reviewed_byとしてバックエンドに渡していないことをソースレベルで確認する
    （Baseline v1 pmo-002/007/008の再発防止）。Streamlit自体を起動せずに確認できるよう、
    ソースの静的検査のみを行う。"""
    src = (Path(__file__).parent.parent / "sustainability_expert_dashboard.py").read_text(encoding="utf-8")
    assert 'st.text_input("レビュー担当者ID"' not in src
    assert 'reviewer_id or "unknown"' not in src
    assert "_authorize_reviewer_action" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
