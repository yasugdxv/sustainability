"""
Geo Intelligence API（別アプリ geopolitical_monitor_dashboard）とやり取りする
Request/Responseの型定義と、Responseのスキーマ検証のみを担当するモジュール。
HTTP通信・DB保存には一切関与しない（geo_intelligence_client.py / geo_intelligence_service.py
が別途担当する）。

Request/Responseの実仕様は geopolitical_monitor_dashboard/geo_intelligence_schema.py
（Pydantic GeoIntelligenceRequest/GeoIntelligenceResponse）を正として確認済み:
    - confidence は "high"/"medium"/"low" の文字列（0〜1の数値ではない）
    - status（Geo側の業務ステータス。completed/partial/failed）は、Sustainability側の
      通信ステータス（success/unavailable/failed/invalid_response/disabled）とは
      別物の語彙のため、本モジュールでは意図的に "status" というフィールド名のまま
      保持し、呼び出し側（geo_intelligence_service.py）で "remote_status" として
      DBの別カラムに記録する（通信状態と業務ステータスを混同しないため）。
    - request_idは送信した値をGeo側がそのままエコーバックする実装のため、
      不一致は「別のリクエストに対する応答が紛れ込んでいる」異常事態とみなし、
      parse_response()でGeoResponseValidationErrorとして扱う。
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import uuid

import jsonschema

REQUEST_TYPES = ("weekly_article", "user_question", "weekly_monitoring")
KNOWLEDGE_SUFFICIENCY_VALUES = ("sufficient", "partial", "insufficient")
# 参考情報: Geo側が実際にセットする値（confidence/remote_statusにCHECK制約は
# 設けない。Geo側が将来値を追加してもクライアント側の検証・保存が壊れないように
# するため。ここでは実装時点の既知の値をコメントとして残すのみ）
KNOWN_CONFIDENCE_VALUES = ("high", "medium", "low")
KNOWN_REMOTE_STATUS_VALUES = ("completed", "partial", "failed")


class GeoResponseValidationError(ValueError):
    """Geo Responseがそのまま下流へ流せないと判断した場合の例外
    （必須フィールド欠落等のスキーマ不正、またはrequest_id不一致）。
    geo_intelligence_client.py はこれを捕捉し invalid_response として扱う。"""


def new_request_id() -> str:
    return str(uuid.uuid4())


def _iso_date(value):
    """Geoへ送信する日付をISO 8601文字列(YYYY-MM-DD)へ変換する。
    datetimeは時刻成分を含むため必ず.date()経由で日付部分のみ取り出し、
    dateはそのままisoformat()する。文字列やNoneはそのまま通す
    （既にYYYY-MM-DD文字列で渡された場合を壊さない）。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


@dataclass
class GeoQueryRequest:
    """Geo Intelligence APIへ送信するRequest。
    to_payload()はNoneのキーも残す（Geo側の必須/任意判断をこちらで先回りして
    間引かない）。ただしGeo側の実スキーマがlist系フィールドにdefault_factory=list
    を使っているため、Noneより空リストの方が意図が伝わるものは空リストにする。"""
    request_id: str
    question: str = None  # weekly_monitoringのみ省略可（Geo側が内部用に自動合成する）
    requester_app: str = "sustainability"
    request_type: str = "user_question"
    country_region: list = field(default_factory=list)
    themes: list = field(default_factory=list)
    context: str = None
    time_horizon: str = None
    source_refs: list = field(default_factory=list)
    analysis_requested: bool = True
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # weekly_monitoring専用フィールド（他のrequest_typeでは常にNone）
    period_start: object = None
    period_end: object = None
    sustainability_themes: list = None
    priority_geographies: list = None
    business_context: str = None
    # Geo側 Phase B-B（Cross-Department Inquiry Receiver）向け。Geo側は未指定なら
    # 既存の無差別user_question扱いのまま動く（後方互換）。source_workflow は
    # 呼び出し元capability（"chat"/"search"等）をそのまま渡す想定。
    source_department: str = None
    source_workflow: str = None
    freshness_requirement: str = None

    def to_payload(self) -> dict:
        return {
            "request_id": self.request_id,
            "requester_app": self.requester_app,
            "request_type": self.request_type,
            "question": self.question,
            "country_region": self.country_region or [],
            "themes": self.themes or [],
            "context": self.context,
            "time_horizon": self.time_horizon,
            "source_refs": self.source_refs or [],
            "analysis_requested": self.analysis_requested,
            "requested_at": self.requested_at,
            "period_start": _iso_date(self.period_start),
            "period_end": _iso_date(self.period_end),
            "sustainability_themes": self.sustainability_themes or [],
            "priority_geographies": self.priority_geographies or [],
            "business_context": self.business_context,
            "source_department": self.source_department,
            "source_workflow": self.source_workflow,
            "freshness_requirement": self.freshness_requirement,
        }


def build_request(*, request_type, question=None, country_region=None, themes=None,
                   context=None, time_horizon=None, source_refs=None,
                   analysis_requested=True, request_id=None,
                   period_start=None, period_end=None, sustainability_themes=None,
                   priority_geographies=None, business_context=None,
                   source_department=None, source_workflow=None,
                   freshness_requirement=None) -> GeoQueryRequest:
    """Weekly(S2)/Chat(S3)から呼ばれる想定のRequest組み立てヘルパー。
    question はGeo側で必須・空文字不可のため、ここでも先に弾く（Geoまで
    送ってから400を受け取るより、送信前に気づけた方が呼び出し元にとって分かりやすい）。
    ただしweekly_monitoringのみGeo側のnormalize_requestと同じくquestion省略可
    （Geo側が内部用に自動合成するため）。"""
    if request_type not in REQUEST_TYPES:
        raise ValueError(f"未対応のrequest_type: {request_type}（対応: {REQUEST_TYPES}）")
    if request_type != "weekly_monitoring" and (not question or not question.strip()):
        raise ValueError("questionは必須です（空文字不可）")
    return GeoQueryRequest(
        request_id=request_id or new_request_id(),
        question=question,
        request_type=request_type,
        country_region=country_region or [],
        themes=themes or [],
        context=context,
        time_horizon=time_horizon,
        source_refs=source_refs or [],
        analysis_requested=analysis_requested,
        period_start=period_start,
        period_end=period_end,
        sustainability_themes=sustainability_themes,
        priority_geographies=priority_geographies,
        business_context=business_context,
        source_department=source_department,
        source_workflow=source_workflow,
        freshness_requirement=freshness_requirement,
    )


@dataclass
class GeoQueryResponse:
    """Geo Intelligence APIから受け取ったResponse。値の穴埋め・推測は一切しない
    （例: references=null ならNoneのまま保持し、架空の参照を生成しない）。"""
    request_id: str
    status: str  # Geo側の業務ステータス（completed/partial/failed）。通信状態ではない
    geo_assessment: str
    key_stakeholders: list
    political_dynamics: str
    outlook: str
    cross_domain_implications: str
    references: list
    confidence: str  # "high"/"medium"/"low" の文字列（0〜1の数値ではない）
    knowledge_sufficiency: str
    knowledge_gap: dict
    error: dict
    completed_at: str
    raw: dict  # 検証済みの生dict（デバッグ・保存用にそのまま保持）
    # weekly_monitoring専用フィールド（他のrequest_typeでは常にNone）
    period_start: str = None
    period_end: str = None
    relevant_intelligence: list = None
    truncated_count: int = None
    # Geo側 Phase B-B（Cross-Department Inquiry Receiver）向けoptional block。
    # source_department未送信の既存Requestに対してGeoは常にNoneを返す設計のため、
    # 未送信のままでも本フィールドは常にNoneのまま安全に無視できる（後方互換）。
    department_intelligence: dict = None


RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "required": ["request_id", "status", "knowledge_sufficiency"],
    "properties": {
        "request_id": {"type": "string", "minLength": 1},
        "status": {"type": "string", "minLength": 1},
        "knowledge_sufficiency": {"type": "string", "enum": list(KNOWLEDGE_SUFFICIENCY_VALUES)},
        "confidence": {"type": ["string", "null"]},
        "references": {"type": ["array", "null"]},
        "knowledge_gap": {"type": ["object", "null"]},
        "error": {"type": ["object", "null"]},
        "completed_at": {"type": ["string", "null"]},
    },
    "additionalProperties": True,  # Geo側の将来的なフィールド追加でここが壊れないように前方互換
}


def parse_response(raw: dict, expected_request_id: str = None) -> GeoQueryResponse:
    """raw dictを検証し、GeoQueryResponseへ変換する。
    - 必須フィールド欠落・型不正 → jsonschema.ValidationError
    - expected_request_idを渡した場合、raw["request_id"]と不一致なら
      GeoResponseValidationError（別のリクエストへの応答が紛れ込んでいる可能性が
      あり、正常なResponseとして扱えないため）
    いずれの場合も呼び出し元（geo_intelligence_client.py）が捕捉しinvalid_responseに変換する。
    値の穴埋め・推測はしない。"""
    try:
        jsonschema.validate(raw, RESPONSE_JSON_SCHEMA)
    except jsonschema.ValidationError as e:
        raise GeoResponseValidationError(f"Geo Responseのスキーマ検証に失敗: {e.message}") from e

    if expected_request_id is not None and raw.get("request_id") != expected_request_id:
        raise GeoResponseValidationError(
            f"request_id不一致: 送信={expected_request_id!r}, 受信={raw.get('request_id')!r}"
        )

    return GeoQueryResponse(
        request_id=raw.get("request_id"),
        status=raw.get("status"),
        geo_assessment=raw.get("geo_assessment"),
        key_stakeholders=raw.get("key_stakeholders"),
        political_dynamics=raw.get("political_dynamics"),
        outlook=raw.get("outlook"),
        cross_domain_implications=raw.get("cross_domain_implications"),
        references=raw.get("references"),
        confidence=raw.get("confidence"),
        knowledge_sufficiency=raw.get("knowledge_sufficiency"),
        knowledge_gap=raw.get("knowledge_gap"),
        error=raw.get("error"),
        completed_at=raw.get("completed_at"),
        raw=raw,
        period_start=raw.get("period_start"),
        period_end=raw.get("period_end"),
        relevant_intelligence=raw.get("relevant_intelligence"),
        truncated_count=raw.get("truncated_count"),
        department_intelligence=raw.get("department_intelligence"),
    )
