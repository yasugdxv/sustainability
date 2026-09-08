"""
Geo Intelligence APIをApplication側（Weekly/Sustainability AI Chat）から使うための
公開Interface。HTTP・認証・リトライ・タイムアウトは呼び出し側に一切意識させない。

query_geo_intelligence() が唯一の公開エントリポイント。geo_client（Geo Intelligence
Client）とdb_client（Supabase監査ログ用）は明確に分離した別引数として受け取る
（GeoIntelligenceClientはDBを一切知らず、SupabaseClientはGeoの存在を一切知らない）。

Phase S1時点ではこの関数はテストコードからのみ呼ばれ、実際のWeekly/Chatの処理からは
呼ばれない（Article Pipeline / Sustainability AIの既存Business Logicには一切影響しない）。
"""
from datetime import datetime, timezone

from article_crawler import SupabaseClient, load_config, make_proxies
from geo_intelligence_client import GeoIntelligenceClient
from geo_intelligence_schema import build_request

_SENSITIVE_KEY_MARKERS = ("api_key", "apikey", "authorization", "token", "secret", "password")


def _scrub(payload):
    """DB保存前の最終防衛線。Requestスキーマ設計上、認証情報はpayloadに含まれない
    想定だが、将来の実装ミス・Geo側の予期しないエコーバックに備え、キー名に
    機微語を含む値は問答無用でマスクする。"""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if isinstance(k, str) and any(marker in k.lower() for marker in _SENSITIVE_KEY_MARKERS):
                out[k] = "***REDACTED***"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(payload, list):
        return [_scrub(v) for v in payload]
    return payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def query_geo_intelligence(
    *,
    request_type: str,
    question: str = None,
    country_region: list = None,
    themes: list = None,
    context: str = None,
    time_horizon: str = None,
    source_refs: list = None,
    analysis_requested: bool = True,
    source_type: str = None,
    source_id: str = None,
    geo_client: GeoIntelligenceClient = None,
    db_client: SupabaseClient = None,
    config: dict = None,
    period_start=None,
    period_end=None,
    sustainability_themes: list = None,
    priority_geographies: list = None,
    business_context: str = None,
    request_fingerprint_hash: str = None,
    source_department: str = None,
    source_workflow: str = None,
    freshness_requirement: str = None,
) -> dict:
    """Weekly(S2)/Sustainability AI Chat(S3)から呼ばれる想定の唯一の公開関数。

    geo_client: 省略時はconfigからGeoIntelligenceClientを生成する。
    db_client: 省略時はconfigからSupabaseClientを生成する（監査ログ保存用）。
    config: 省略時はload_config()。

    period_start/period_end/sustainability_themes/priority_geographies/business_contextは
    weekly_monitoring専用のキーワード引数（Phase S2追加）。既存のuser_question呼び出しは
    これらを渡さなければ従来通り動作する（後方互換）。

    request_fingerprint_hash: Phase B（Department Intelligence AI）のExact Response Cache用。
    呼び出し元（sustainability_chat_geo_service.compute_geo_request_fingerprint()）が計算済みの
    ハッシュ値をそのまま渡す（この関数では計算しない）。省略時（weekly呼び出し等）はNULLのまま
    external_intelligence_callsへ保存され、後方互換を保つ。

    source_department/source_workflow/freshness_requirement: Geo側 Phase B-B（Cross-Department
    Inquiry Receiver）向け。省略時はGeo側が既存user_question扱いのまま処理する（後方互換）。

    戻り値はGeoIntelligenceClient.query()と同じGeoCallResult形状のdictに、
    external_intelligence_callsへ保存できた場合のみ"external_call_id"を追加したもの
    （weekly側がraw responseを二重保存せず、この列経由で参照できるようにするため。
    DB書き込み自体が失敗した場合はNoneのまま）。
    """
    config = config or load_config()
    if geo_client is None:
        proxies = make_proxies(config)
        verify = config.get("ssl", {}).get("verify", True)
        geo_client = GeoIntelligenceClient(config, proxies, verify)
    if db_client is None:
        db_client = SupabaseClient(config)

    request = build_request(
        request_type=request_type, question=question, country_region=country_region,
        themes=themes, context=context, time_horizon=time_horizon,
        source_refs=source_refs, analysis_requested=analysis_requested,
        period_start=period_start, period_end=period_end,
        sustainability_themes=sustainability_themes,
        priority_geographies=priority_geographies, business_context=business_context,
        source_department=source_department, source_workflow=source_workflow,
        freshness_requirement=freshness_requirement,
    )

    requested_at = _now_iso()
    result = geo_client.query(request)
    external_call_id = _record_call(db_client, request, result, source_type, source_id, requested_at,
                                     request_fingerprint_hash=request_fingerprint_hash)
    result = dict(result)
    result["external_call_id"] = external_call_id
    return result


def _record_call(db_client: SupabaseClient, request, result: dict,
                  source_type: str, source_id: str, requested_at: str,
                  request_fingerprint_hash: str = None) -> str:
    """external_intelligence_callsへの1行書き込み。書き込み自体の失敗はprintのみで
    呼び出し元へ伝播させない（Geo呼び出し結果とDB記録の成否は独立させる）。
    戻り値: 生成されたid（失敗時はNone）"""
    try:
        response = result.get("response")
        rows = db_client.insert("external_intelligence_calls", [{
            "target_service": "geo_intelligence",
            "request_type": request.request_type,
            "source_type": source_type,
            "source_id": source_id,
            "local_request_id": request.request_id,
            "remote_request_id": response.request_id if response else None,
            "request_payload": _scrub(request.to_payload()),
            "response_payload": _scrub(result.get("raw_response")),
            "status": result["status"],
            "remote_status": result.get("remote_status"),
            "knowledge_sufficiency": response.knowledge_sufficiency if response else None,
            "confidence": response.confidence if response else None,
            "requested_at": requested_at,
            "completed_at": _now_iso() if result["status"] != "disabled" else None,
            "latency_ms": result.get("latency_ms"),
            "http_status_code": result.get("http_status"),
            "error_type": result.get("error_type"),
            "error_message": result.get("error_message"),
            "request_fingerprint_hash": request_fingerprint_hash,
        }])
        return rows[0]["id"] if rows else None
    except Exception as e:
        print(f"[geo_intelligence_service] 通信ログ保存失敗（Geo呼び出し結果には影響なし）: "
              f"{type(e).__name__}: {e}")
        return None
