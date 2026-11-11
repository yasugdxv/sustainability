"""
当社サステナビリティ専門家MVP: 承認/レビュー操作の共通アイデンティティ境界（Phase 3）。

背景:
これまで週次メール/競合日次ダイジェスト/競合月次レポートの3つの承認画面・APIは、
いずれもStreamlitの自由記述テキスト入力（レビュー担当者ID）やHTTP APIのリクエスト
ボディの自己申告フィールドを、そのままDBのreviewer_id列（＝送信を承認した人物の
監査記録）として書き込んでいた。空欄は"unknown"に丸められ、"PMO_Manager_Yamada"の
ような架空の名前もそのまま承認者として記録される（Baseline v1 pmo-002/007/008）。

本モジュールは、承認/却下操作が必ず通るべき、ただ1つの「認証済みアイデンティティ」の
境界を提供する。まだEntra ID接続は実装していない（このフェーズの対象外）ため、
本番相当（DEV AUTHモードが明示的に有効化されていない状態）では、認証済み
principalを解決する手段が現時点で存在せず、常にfail-closed（拒否）する。
開発時のみ、環境変数で明示的に有効化されたDEV AUTHモードに限り、環境変数由来の
FakePrincipalを使うことができる。

呼び出し側（api_server.py / sustainability_expert_api.py）は、
resolve_principal() で得たAuthenticatedPrincipal.user_idを、既存のreviewer_id列に
書き込む値としてそのまま使う（DBスキーマは変更しない。reviewer_id列自体は残し、
その「値の出どころ」だけを自由記述からprincipal.user_idへ切り替える）。
"""
import contextvars
import os
from dataclasses import dataclass, field

# ─── DEV AUTHモード（config.jsonスキーマは変更しないため、環境変数のみで判定する） ──
DEV_AUTH_MODE_ENV = "SUSTAINABILITY_EXPERT_DEV_AUTH_MODE"
DEV_USER_ID_ENV = "SUSTAINABILITY_EXPERT_DEV_USER_ID"
DEV_DISPLAY_NAME_ENV = "SUSTAINABILITY_EXPERT_DEV_DISPLAY_NAME"

# このビジネスドメインには、レビューと承認を分ける既存のロール区分が無い
# （reviewer_idは元々レビュー・承認・却下の全操作で同一人物を指す1つの識別子だった）。
# そのため過剰なロール体系は作らず、最小限の単一ロールのみを定義する。
ROLE_REVIEWER = "reviewer"

# authentication_sourceに使う既知の値（監査可能性のため、常にこのどちらかを使う。
# 本物のEntra ID接続は本フェーズでは未実装）
AUTH_SOURCE_DEV_FAKE = "dev_fake"
AUTH_SOURCE_ENTRA = "entra_id"      # SSO(Entra ID)。entra_auth.validate_access_token が付与する

# ─── リクエストのBearerトークン（SSO。2026-10-23追加）─────────────────────────────
# api_serverのミドルウェアが、リクエストごとに Authorization ヘッダーのトークンをここへ入れ、
# _review_principal() が resolve_principal(bearer_token=...) へ渡す。承認系の各エンドポイントの
# 引数を変えずに済むようにするための橋渡し（コンテキスト変数なので、リクエスト間では混ざらない）
_request_bearer_token = contextvars.ContextVar("sustainability_request_bearer_token", default=None)


def bearer_token_from_header(authorization) -> str:
    """'Authorization: Bearer xxx' からトークンを取り出す（形式が違う・無ければ None）"""
    parts = (authorization or "").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def set_request_bearer_token(token):
    """このリクエストのトークンを設定する。戻り値は、reset_request_bearer_token へ渡す"""
    return _request_bearer_token.set(token)


def reset_request_bearer_token(reset_token) -> None:
    _request_bearer_token.reset(reset_token)


def current_bearer_token():
    return _request_bearer_token.get()


def is_dev_auth_mode_enabled() -> bool:
    """DEV AUTHモードが明示的に有効化されているかどうか。
    config.jsonのスキーマは本フェーズで変更禁止のため、判定は環境変数のみで行う
    （config.json経由のフォールバックは意図的に持たせない）。
    本番環境ではこの環境変数を絶対に設定しないこと。"""
    return os.environ.get(DEV_AUTH_MODE_ENV, "").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """承認/却下操作の唯一の監査アイデンティティ。

    user_id: 監査用の安定した識別子。DBのreviewer_id/reviewed_by/approved_by列には
             常にこの値だけを書き込む。自由記述文字列や"unknown"は絶対に入れない
             （__post_init__でも最低限のガードをかける）。
    display_name/email: 表示専用メタデータ。認可判断には使わない。
    roles: 付与されているロール（このドメインでは "reviewer" のみ）。
    authentication_source: どの経路で認証されたか（例: "dev_fake"。将来のEntra ID
             接続では "entra_id" 等になる想定だが、本フェーズでは未実装）。
    """
    user_id: str
    display_name: str
    email: str = ""
    roles: tuple = field(default_factory=tuple)
    authentication_source: str = "unknown"
    tenant_id: str = None
    authenticated_at: str = None

    def __post_init__(self):
        if not self.user_id or not str(self.user_id).strip():
            raise ValueError("AuthenticatedPrincipal.user_id は必須です（空文字は不可）")
        if self.user_id.strip().lower() == "unknown":
            raise ValueError('AuthenticatedPrincipal.user_id に "unknown" は使用できません')


class AuthenticationError(Exception):
    """認証済みprincipalを解決できなかったことを表す（呼び出し側はfail-closedすること。
    このエラーを握りつぶしてFakePrincipal等へ自動フォールバックしてはならない）"""


def make_dev_principal(user_id: str, display_name: str = None, roles=(ROLE_REVIEWER,)) -> AuthenticatedPrincipal:
    """DEV AUTHモード専用のFakePrincipalを組み立てる。
    is_dev_auth_mode_enabled()の確認はここでは行わない（それはresolve_principal()の
    責務）。authentication_source="dev_fake"を必ず付与し、監査ログ上でも
    「開発用の仮アイデンティティである」ことが常に判別できるようにする。"""
    return AuthenticatedPrincipal(
        user_id=user_id,
        display_name=display_name or user_id,
        email="",
        roles=tuple(roles),
        authentication_source=AUTH_SOURCE_DEV_FAKE,
    )


def dev_principal_from_env() -> AuthenticatedPrincipal:
    """DEV AUTHモードが有効な場合のみ、環境変数からFakePrincipalを組み立てて返す。
    DEV AUTHモードが無効なら常にNoneを返す（呼び出し側で「本番なのでこの値は使えない」
    という判断をさせないため、ここで既に安全側に倒す）。

    ユーザーがStreamlit画面で自由に入力したテキストからこの値を作ることは禁止
    （"レビュー担当者ID"欄の値をここに混ぜてはいけない）。あくまで環境変数
    （SUSTAINABILITY_EXPERT_DEV_USER_ID）という、開発者が明示的に設定する経路のみを
    使う。環境変数が未設定ならNoneを返す（＝DEV AUTHモードが有効でも、
    ユーザーIDが明示されていなければ解決失敗として扱われ、自動フォールバックはしない）。"""
    if not is_dev_auth_mode_enabled():
        return None
    user_id = os.environ.get(DEV_USER_ID_ENV, "").strip()
    if not user_id:
        return None
    display_name = os.environ.get(DEV_DISPLAY_NAME_ENV, "").strip() or user_id
    return make_dev_principal(user_id=user_id, display_name=display_name)


def resolve_principal(dev_principal: AuthenticatedPrincipal = None, bearer_token: str = None) -> AuthenticatedPrincipal:
    """承認/却下操作の唯一の入口。呼び出し元は必ずこの関数を通してprincipalを
    取得すること（Streamlitのレビュー担当者ID自由入力や、APIリクエストボディの
    reviewer_idを直接使ってはならない）。

    - DEV AUTHモードが明示的に有効な場合のみ、渡されたdev_principalを検証のうえ
      そのまま返す（dev_principalがNone、またはauthentication_source!="dev_fake"
      なら拒否する）。
    - DEV AUTHモードが無効で、Entra ID(SSO)が設定済み(entra_auth.entra_config_from_env)の
      場合は、bearer_token(Authorization: Bearer)を検証して、本人を解決する。トークンが無い・
      不正・期限切れなら、AuthenticationErrorでfail-closedする（2026-10-23追加）。
    - それ以外（DEV AUTHもSSOも未設定）の場合は、認証済みprincipalを得る手段が存在せず、
      常にAuthenticationErrorでfail-closedする。「解決に失敗したらFakePrincipalへ
      自動フォールバックする」という挙動は絶対に行わない。
    """
    if is_dev_auth_mode_enabled():
        if dev_principal is None:
            raise AuthenticationError(
                f"DEV AUTHモード（{DEV_AUTH_MODE_ENV}）が有効ですが、principalが渡されて"
                f"いません（fail-closed。{DEV_USER_ID_ENV}が未設定の可能性があります）")
        if dev_principal.authentication_source != AUTH_SOURCE_DEV_FAKE:
            raise AuthenticationError(
                f'DEV AUTHモードでは authentication_source="{AUTH_SOURCE_DEV_FAKE}" の'
                f'principalのみ使用できます（渡された値: {dev_principal.authentication_source!r}）')
        return dev_principal

    # 本番相当: Entra ID(SSO)が設定済みなら、Bearerトークンを検証する。
    # （entra_authは、循環importを避けるため、使う時に読み込む）
    from entra_auth import entra_config_from_env, validate_access_token
    entra_config = entra_config_from_env()
    if entra_config is not None:
        return validate_access_token(bearer_token, entra_config)

    # DEV AUTHもSSOも未設定 → 認証コンテキストが存在しないので、必ずfail-closed。
    # dev_principalが渡されていても、DEV AUTHモードが無効な限り絶対に使わない
    # （本番がDEVモードへフォールバックすることを防ぐ）。
    raise AuthenticationError(
        "認証済みprincipalを解決できません（Entra ID(SSO)が未設定のため"
        f"fail-closedしました。SSOは、環境変数 SUSTAINABILITY_ENTRA_TENANT_ID / SUSTAINABILITY_ENTRA_CLIENT_ID "
        f"で有効化します。開発時のみ、環境変数 {DEV_AUTH_MODE_ENV}=true と "
        f"{DEV_USER_ID_ENV}=<開発者ID> を明示的に設定してDEV AUTHモードを有効化して"
        "ください）")


def can_review(principal: AuthenticatedPrincipal) -> bool:
    """内容の確認・修正保存が可能か（principalが無ければ常に不可）"""
    if principal is None:
        return False
    return ROLE_REVIEWER in (principal.roles or ())


def can_approve(principal: AuthenticatedPrincipal) -> bool:
    """承認（＝送信トリガー）・却下が可能か。
    現状のビジネスドメインにはレビューと承認を分ける既存のロール区分が無いため、
    reviewerロールのみを要求する（過剰な新ロール体系は作らない）。"""
    if principal is None:
        return False
    return ROLE_REVIEWER in (principal.roles or ())


def audit_reviewer_id(principal: AuthenticatedPrincipal) -> str:
    """承認/却下操作でDBのreviewer_id/reviewed_by/approved_by列に書き込むべき値。
    必ずprincipal.user_idを返す。呼び出し側はここにユーザー入力の自由文字列や
    リクエストボディの自己申告値を混ぜてはいけない（このドメインで、それら列に
    書き込む値を決める唯一の関数とする）。"""
    if principal is None:
        raise AuthenticationError("principalが無いため監査用reviewer_idを解決できません")
    return principal.user_id
