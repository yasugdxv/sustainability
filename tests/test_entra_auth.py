"""Entra ID(SSO)のアクセストークン検証のテスト（2026-10-23追加）

実際のEntra IDには接続しない。テスト用のRSA鍵でJWTを署名し、公開鍵の取得クライアントを差し替えて検証する。
確認すること: 正しいトークンだけが通る / 期限切れ・別テナント・別宛先・署名不正・アプリ専用トークンは拒否 /
DEV AUTHとの優先関係 / SSO未設定ならfail-closed / リクエストごとのトークンが混ざらない。
"""
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import entra_auth  # noqa: E402
import identity  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ENTRA_ENV = {entra_auth.TENANT_ID_ENV: TENANT, entra_auth.CLIENT_ID_ENV: CLIENT}
CLEAN_ENV = {k: "" for k in (identity.DEV_AUTH_MODE_ENV, identity.DEV_USER_ID_ENV, identity.DEV_DISPLAY_NAME_ENV,
                              entra_auth.TENANT_ID_ENV, entra_auth.CLIENT_ID_ENV, entra_auth.AUDIENCE_ENV,
                              entra_auth.REQUIRED_SCOPE_ENV, entra_auth.JWKS_URI_ENV)}

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwks:
    """PyJWKClientの代わり。常にテスト用の公開鍵を返す"""

    class _K:
        key = _KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._K()


def _claims(**over):
    now = int(time.time())
    c = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": f"api://{CLIENT}", "tid": TENANT,
         "iat": now, "nbf": now, "exp": now + 3600, "scp": "access_as_user", "oid": "oid-0001",
         "preferred_username": "Taro.Suntory@suntory.co.jp", "name": "サントリー 太郎"}
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def _token(key=None, **over):
    return jwt.encode(_claims(**over), key or _KEY, algorithm="RS256")


def _validate(token, env=None):
    with patch.dict(os.environ, {**CLEAN_ENV, **ENTRA_ENV, **(env or {})}):
        return entra_auth.validate_access_token(token, entra_auth.entra_config_from_env(), jwks_client=_FakeJwks())


# ─── 正しいトークン ───
def test_valid_token_becomes_a_reviewer_principal():
    p = _validate(_token())
    assert p.user_id == "taro.suntory@suntory.co.jp"                 # 監査ID: 小文字のUPN
    assert p.display_name == "サントリー 太郎" and p.tenant_id == TENANT
    assert p.authentication_source == identity.AUTH_SOURCE_ENTRA == "entra_id"
    assert identity.can_approve(p) and identity.audit_reviewer_id(p) == p.user_id


def test_client_id_itself_is_also_accepted_as_audience():
    assert _validate(_token(aud=CLIENT)).user_id


def test_user_id_falls_back_to_oid_when_no_name_claims():
    assert _validate(_token(preferred_username=None, upn=None, email=None)).user_id == "oid-0001"


# ─── 拒否するトークン ───
@pytest.mark.parametrize("label,kwargs", [
    ("期限切れ", {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}),
    ("別の宛先(aud)", {"aud": "api://someone-else"}),
    ("別の発行元(iss)", {"iss": "https://login.microsoftonline.com/other-tenant/v2.0"}),
    ("別のテナント(tid)", {"tid": "99999999-0000-0000-0000-000000000000"}),
    ("アプリ専用トークン(scpなし)", {"scp": None, "roles": ["Some.Role"]}),
    ("ユーザーを特定できない", {"preferred_username": None, "oid": None}),
])
def test_invalid_tokens_are_rejected(label, kwargs):
    with pytest.raises(identity.AuthenticationError):
        _validate(_token(**kwargs))


def test_token_signed_with_another_key_is_rejected():
    with pytest.raises(identity.AuthenticationError):
        _validate(_token(key=_OTHER_KEY))


def test_unsigned_alg_none_token_is_rejected():
    forged = jwt.encode(_claims(), key=None, algorithm="none")
    with pytest.raises(identity.AuthenticationError):
        _validate(forged)


@pytest.mark.parametrize("token", [None, "", "   ", "not.a.jwt"])
def test_missing_or_garbage_token_is_rejected(token):
    with pytest.raises(identity.AuthenticationError):
        _validate(token)


def test_required_scope_is_enforced_when_configured():
    env = {entra_auth.REQUIRED_SCOPE_ENV: "access_as_user"}
    assert _validate(_token(scp="access_as_user other"), env).user_id
    with pytest.raises(identity.AuthenticationError):
        _validate(_token(scp="some_other_scope"), env)


def test_jwks_fetch_failure_is_rejected_not_crashing():
    class Broken:
        def get_signing_key_from_jwt(self, token):
            raise OSError("network down")

    with patch.dict(os.environ, {**CLEAN_ENV, **ENTRA_ENV}):
        with pytest.raises(identity.AuthenticationError):
            entra_auth.validate_access_token(_token(), entra_auth.entra_config_from_env(), jwks_client=Broken())


# ─── identity.resolve_principal との関係 ───
def test_sso_is_disabled_without_tenant_and_client_id_and_fails_closed():
    with patch.dict(os.environ, CLEAN_ENV):
        assert entra_auth.entra_config_from_env() is None
        with pytest.raises(identity.AuthenticationError):
            identity.resolve_principal(dev_principal=None, bearer_token=_token())    # SSO未設定なら、トークンがあっても通さない


def test_resolve_principal_validates_the_bearer_token_when_sso_is_configured():
    with patch.dict(os.environ, {**CLEAN_ENV, **ENTRA_ENV}), \
            patch.object(entra_auth, "_default_jwks_client", lambda uri: _FakeJwks()):
        assert identity.resolve_principal(bearer_token=_token()).authentication_source == "entra_id"
        with pytest.raises(identity.AuthenticationError):
            identity.resolve_principal(bearer_token=None)                             # トークンなし = 拒否
        with pytest.raises(identity.AuthenticationError):
            identity.resolve_principal(dev_principal=identity.make_dev_principal("dev.x"))   # DEV AUTH無効なら、開発用の本人は使えない


def test_dev_auth_mode_still_wins_for_local_development():
    env = {**CLEAN_ENV, **ENTRA_ENV, identity.DEV_AUTH_MODE_ENV: "true", identity.DEV_USER_ID_ENV: "dev.tanaka"}
    with patch.dict(os.environ, env):
        p = identity.resolve_principal(dev_principal=identity.dev_principal_from_env(), bearer_token=None)
    assert p.authentication_source == "dev_fake" and p.user_id == "dev.tanaka"


# ─── Authorizationヘッダーの読み取りと、リクエスト間の分離 ───
@pytest.mark.parametrize("header,expected", [
    ("Bearer abc.def.ghi", "abc.def.ghi"), ("bearer   abc", "abc"), ("Bearer", None), ("Basic abc", None),
    ("", None), (None, None), ("Bearer    ", None),
])
def test_bearer_token_from_header(header, expected):
    assert identity.bearer_token_from_header(header) == expected


def test_request_token_is_scoped_and_restored():
    assert identity.current_bearer_token() is None
    reset = identity.set_request_bearer_token("tok-1")
    assert identity.current_bearer_token() == "tok-1"
    identity.reset_request_bearer_token(reset)
    assert identity.current_bearer_token() is None


def test_api_server_wires_the_token_into_the_principal_resolution():
    """api_serverは、設定の読み込みが重く、テストではimportしない（test_identity.pyと同じ方針で、配線を確認）"""
    src = (Path(__file__).parent.parent / "api_server.py").read_text(encoding="utf-8")
    assert "identity.bearer_token_from_header(request.headers.get(\"authorization\"))" in src
    assert "bearer_token=identity.current_bearer_token()" in src
    assert "identity.reset_request_bearer_token(reset)" in src


def test_pyjwt_is_listed_in_the_azure_requirements():
    reqs = (Path(__file__).parent.parent / "requirements_azure.txt").read_text(encoding="utf-8").lower()
    assert "pyjwt" in reqs
