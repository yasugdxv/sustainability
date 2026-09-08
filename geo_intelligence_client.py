"""
Geo Intelligence API（別アプリ geopolitical_monitor_dashboard）を呼び出すHTTP Client。

責務はHTTP通信・リトライ・タイムアウト・障害分離・レスポンスのスキーマ検証呼び出しのみ。
DBアクセスは一切行わない（geo_intelligence_service.py が別途担当し、SupabaseClientと
明確に分離する）。

戻り値は常に構造化dict（GeoCallResult形状、query()のdocstring参照）で、例外を
外に投げない。Sustainability Intelligence全体をGeo側の障害で止めないため。

通信状態（success/unavailable/failed/invalid_response/disabled）と、Geo自身の
業務ステータス（completed/partial/failed、response["remote_status"]）は明確に
分離する。Geo側が200かつスキーマとして正しいレスポンスを返した時点で、Geoの
業務上の結果が失敗（remote_status="failed"）であっても、通信としては"success"
として扱う（Geo側の実装確認により、業務失敗時もGeoはHTTPExceptionではなく
200+構造化errorで返すことを確認済み。詳細はgeo_intelligence_schema.pyのdocstring）。
"""
import time

import requests

from geo_intelligence_schema import GeoQueryRequest, GeoResponseValidationError, parse_response
from geo_auth_provider import GeoAuthProvider, ApiKeyAuthProvider, NullAuthProvider

RETRYABLE_HTTP_STATUS_CODES = (502, 503, 504)
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 5.0


class GeoIntelligenceConfig:
    """config['geo_intelligence']から実行時設定を読み解く。型変換・デフォルト適用を
    ここに集約し、GeoIntelligenceClient本体は生configを触らない。"""

    def __init__(self, config: dict):
        g = (config or {}).get("geo_intelligence") or {}
        self.enabled = bool(g.get("enabled", False))
        self.base_url = (g.get("base_url") or "").rstrip("/")
        self.query_path = g.get("query_path") or "/api/v1/geo-intelligence/query"
        self.timeout_seconds = float(g.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        self.api_key = g.get("api_key") or ""
        self.max_retries = int(g.get("max_retries") if g.get("max_retries") is not None else DEFAULT_MAX_RETRIES)

    @property
    def query_url(self) -> str:
        return f"{self.base_url}{self.query_path}"


def _backoff_seconds(attempt: int) -> float:
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)


def _base_result(request: GeoQueryRequest, status: str, started: float, attempts: int) -> dict:
    return {
        "success": status == "success",
        "status": status,
        "response": None,
        "remote_status": None,
        "error_type": None,
        "error_message": None,
        "http_status": None,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "local_request_id": request.request_id,
        "attempts": attempts,
        "raw_response": None,
    }


class GeoIntelligenceClient:
    """Geo Intelligence APIへのHTTP Client。DBには一切アクセスしない。"""

    def __init__(self, config: dict, proxies: dict = None, verify: bool = True,
                 auth_provider: GeoAuthProvider = None):
        self.settings = GeoIntelligenceConfig(config)
        self.proxies = proxies or {}
        self.verify = verify
        self.auth_provider = auth_provider or (
            ApiKeyAuthProvider(self.settings.api_key) if self.settings.api_key else NullAuthProvider()
        )

    def query(self, request: GeoQueryRequest) -> dict:
        """常に構造化dictを返す。例外を外に投げない。

        戻り値(GeoCallResult形状):
            success: bool
            status: "success" | "unavailable" | "failed" | "invalid_response" | "disabled"
                （Sustainability側の通信状態。Geoの業務ステータスではない）
            response: GeoQueryResponse | None（successの時のみ非None）
            remote_status: str | None（Geo自身のstatus。responseがある時のみ非None）
            error_type: str | None
            error_message: str | None
            http_status: int | None
            latency_ms: int
            local_request_id: str
            attempts: int
            raw_response: dict | None（DB保存用の生レスポンス。取得できなかった場合None）
        """
        started = time.monotonic()

        if not self.settings.enabled:
            return _base_result(request, "disabled", started, 0)
        if not self.settings.base_url:
            result = _base_result(request, "failed", started, 0)
            result["error_type"] = "not_configured"
            result["error_message"] = "geo_intelligence.base_urlが未設定です"
            return result

        headers = {"Content-Type": "application/json", **self.auth_provider.get_auth_headers()}
        payload = request.to_payload()

        attempts = 0
        while True:
            attempts += 1
            try:
                resp = requests.post(
                    self.settings.query_url, json=payload, headers=headers,
                    proxies=self.proxies, verify=self.verify,
                    timeout=self.settings.timeout_seconds,
                )
            except requests.exceptions.Timeout as e:
                if attempts <= self.settings.max_retries:
                    time.sleep(_backoff_seconds(attempts))
                    continue
                result = _base_result(request, "unavailable", started, attempts)
                result["error_type"], result["error_message"] = "timeout", str(e)
                return result
            except requests.exceptions.ConnectionError as e:
                if attempts <= self.settings.max_retries:
                    time.sleep(_backoff_seconds(attempts))
                    continue
                result = _base_result(request, "unavailable", started, attempts)
                result["error_type"], result["error_message"] = "connection_error", str(e)
                return result

            if resp.status_code in RETRYABLE_HTTP_STATUS_CODES:
                if attempts <= self.settings.max_retries:
                    time.sleep(_backoff_seconds(attempts))
                    continue
                result = _base_result(request, "unavailable", started, attempts)
                result["error_type"] = "http_error"
                result["error_message"] = f"HTTP {resp.status_code}"
                result["http_status"] = resp.status_code
                return result

            if resp.status_code >= 400:
                # 400/401/403/404等はリクエスト側の問題である可能性が高く無条件でリトライしない
                result = _base_result(request, "failed", started, attempts)
                result["error_type"] = "http_error"
                result["error_message"] = f"HTTP {resp.status_code}: {resp.text[:500]}"
                result["http_status"] = resp.status_code
                return result

            # 2xx: スキーマ検証（request_id一致確認込み）
            try:
                raw = resp.json()
            except ValueError as e:
                result = _base_result(request, "invalid_response", started, attempts)
                result["error_type"] = "schema_validation"
                result["error_message"] = f"JSONデコード失敗: {e}"
                result["http_status"] = resp.status_code
                return result

            try:
                response = parse_response(raw, expected_request_id=request.request_id)
            except GeoResponseValidationError as e:
                result = _base_result(request, "invalid_response", started, attempts)
                result["error_type"] = "schema_validation"
                result["error_message"] = str(e)
                result["http_status"] = resp.status_code
                result["raw_response"] = raw
                return result

            result = _base_result(request, "success", started, attempts)
            result["response"] = response
            result["remote_status"] = response.status
            result["http_status"] = resp.status_code
            result["raw_response"] = raw
            return result
