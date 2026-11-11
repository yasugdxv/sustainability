"""
Entra ID(旧Azure AD)のアクセストークン検証（SSO対応）。

構成:
- フロント(eco-digest-spark)が、MSAL(SPA・認可コード+PKCE)でログインし、自社APIのスコープの
  アクセストークンを取得して、`Authorization: Bearer <token>` でAPIを呼ぶ。
- API側(本モジュール)が、そのトークンを検証し、identity.AuthenticatedPrincipal に変換する。
  承認・却下の監査ID(reviewer_id)には、principal.user_id が書き込まれる（identity.audit_reviewer_id）。

検証する内容（すべて満たさなければ AuthenticationError。fail-closed）:
- 署名(RS256)。公開鍵は、テナントのJWKS(login.microsoftonline.com)から取得し、キャッシュする
- 発行元(iss)が、自社テナントのもの(v2.0。v1の sts.windows.net も許容)
- テナント(tid)が、自社テナントと一致
- 宛先(aud)が、このAPI（クライアントID、または api://<クライアントID>、または追加指定）
- 有効期限(exp)・発行時刻(iat)。時計のずれは、60秒まで許容
- 委任トークン(scp)であること。アプリ専用トークン(client credentials。ユーザーが居ない)は拒否する
- （任意）必要なスコープ名を含むこと

設定（環境変数。config.jsonのスキーマは変更しない）:
- SUSTAINABILITY_ENTRA_TENANT_ID : 自社テナントID（必須。設定されて初めてSSOが有効になる）
- SUSTAINABILITY_ENTRA_CLIENT_ID : アプリ(クライアント)ID（必須）
- SUSTAINABILITY_ENTRA_AUDIENCE  : 追加で許容する宛先（カンマ区切り。任意）
- SUSTAINABILITY_ENTRA_REQUIRED_SCOPE : 必須スコープ名（例: access_as_user。任意）
- SUSTAINABILITY_ENTRA_JWKS_URI  : 公開鍵のURL（任意。既定はテナントのv2.0 keys）

ロール: ログインできた人は、全員 reviewer（誰がログインできるかは、Entra側の
「ユーザー割り当ての要求」と、割り当てたグループで制御する。ロールの細分化は後続対応）。
"""
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import identity
# PyJWT（署名検証にcryptographyを使う）は、使う場所で読み込む。設定(環境変数)が無い環境や、
# 未インストールの環境でも、identity.resolve_principalが従来どおりfail-closedで動くようにするため

TENANT_ID_ENV = "SUSTAINABILITY_ENTRA_TENANT_ID"
CLIENT_ID_ENV = "SUSTAINABILITY_ENTRA_CLIENT_ID"
AUDIENCE_ENV = "SUSTAINABILITY_ENTRA_AUDIENCE"
REQUIRED_SCOPE_ENV = "SUSTAINABILITY_ENTRA_REQUIRED_SCOPE"
JWKS_URI_ENV = "SUSTAINABILITY_ENTRA_JWKS_URI"

CLOCK_SKEW_SEC = 60
JWKS_CACHE_SEC = 3600


@dataclass(frozen=True)
class EntraConfig:
    tenant_id: str
    client_id: str
    audiences: tuple
    issuers: tuple
    required_scope: str
    jwks_uri: str


def entra_config_from_env():
    """環境変数からEntraの設定を読む。テナントIDかクライアントIDが未設定なら、SSOは無効（None）"""
    tenant = os.environ.get(TENANT_ID_ENV, "").strip()
    client = os.environ.get(CLIENT_ID_ENV, "").strip()
    if not tenant or not client:
        return None
    extra = [a.strip() for a in os.environ.get(AUDIENCE_ENV, "").split(",") if a.strip()]
    return EntraConfig(
        tenant_id=tenant,
        client_id=client,
        audiences=tuple(dict.fromkeys([client, f"api://{client}", *extra])),
        issuers=(f"https://login.microsoftonline.com/{tenant}/v2.0", f"https://sts.windows.net/{tenant}/"),
        required_scope=os.environ.get(REQUIRED_SCOPE_ENV, "").strip(),
        jwks_uri=os.environ.get(JWKS_URI_ENV, "").strip()
        or f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
    )


_jwks_clients = {}


def _default_jwks_client(jwks_uri: str):
    """公開鍵の取得クライアント（URLごとに1つ。鍵はキャッシュして、毎回の通信を避ける）"""
    from jwt import PyJWKClient
    if jwks_uri not in _jwks_clients:
        _jwks_clients[jwks_uri] = PyJWKClient(jwks_uri, cache_keys=True, lifespan=JWKS_CACHE_SEC)
    return _jwks_clients[jwks_uri]


def validate_access_token(token: str, config: EntraConfig, jwks_client=None) -> identity.AuthenticatedPrincipal:
    """アクセストークンを検証して、AuthenticatedPrincipalを返す。検証に失敗したら AuthenticationError。
    jwks_clientは、テスト用の差し替え口（get_signing_key_from_jwt(token).key を持つもの）"""
    if not token or not str(token).strip():
        raise identity.AuthenticationError("アクセストークンがありません（Authorization: Bearer が必要です）")
    try:
        import jwt
    except ImportError as e:
        raise identity.AuthenticationError("PyJWTが未インストールのため、トークンを検証できません") from e
    try:
        client = jwks_client or _default_jwks_client(config.jwks_uri)
        key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"],          # 署名アルゴリズムを固定（alg=none等を拒否）
            audience=list(config.audiences), issuer=list(config.issuers),
            leeway=CLOCK_SKEW_SEC,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as e:
        # 期限切れ・署名不正・宛先違い・発行元違い・鍵が見つからない、など。詳細は、ログ追跡用に型名だけ残す
        raise identity.AuthenticationError(f"アクセストークンを検証できません（{type(e).__name__}: {e}）") from e
    except Exception as e:  # noqa: BLE001  公開鍵の取得失敗(通信エラー等)も、拒否する（fail-closed）
        raise identity.AuthenticationError(f"アクセストークンの検証中にエラーが発生しました（{type(e).__name__}）") from e

    if claims.get("tid") != config.tenant_id:
        raise identity.AuthenticationError("アクセストークンのテナント(tid)が、自社テナントと一致しません")
    scopes = str(claims.get("scp") or "").split()
    if not scopes:
        raise identity.AuthenticationError(
            "委任トークン(scp)ではありません（ユーザーのサインインを伴わないトークンは使用できません）")
    if config.required_scope and config.required_scope not in scopes:
        raise identity.AuthenticationError(f"必要なスコープ({config.required_scope})がトークンにありません")

    user_id = (claims.get("preferred_username") or claims.get("upn") or claims.get("email") or "").strip().lower() \
        or str(claims.get("oid") or "").strip()
    if not user_id:
        raise identity.AuthenticationError("トークンにユーザーを特定できる項目(preferred_username/upn/email/oid)がありません")
    return identity.AuthenticatedPrincipal(
        user_id=user_id,
        display_name=str(claims.get("name") or user_id),
        email=str(claims.get("email") or claims.get("preferred_username") or ""),
        roles=(identity.ROLE_REVIEWER,),
        authentication_source=identity.AUTH_SOURCE_ENTRA,
        tenant_id=config.tenant_id,
        authenticated_at=datetime.fromtimestamp(int(claims["iat"]), tz=timezone.utc).isoformat(),
    )
