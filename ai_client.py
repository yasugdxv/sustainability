"""
OpenAI / Azure OpenAI クライアント生成（共通）

以前は run.py（~5000行のダッシュボード本体）に定義されており、
make_openai_client を使うためだけの軽量スクリプト（article_analyzer.py、
sustainability_article_selector.py 等）まで run.py 全体をimportして
起動コストが増えていた。依存を最小限（httpx, openai, config_utils）に
した本モジュールへ切り出す。
"""
import httpx

from config_utils import find_azure_cfg


def make_openai_client(config: dict):
    """Azure OpenAI を優先し、なければ OpenAI を使う。(client, model) を返す"""
    proxy_cfg = config.get("proxy", {})
    ssl_verify = config.get("ssl", {}).get("verify", True)
    http_client = None
    if proxy_cfg.get("enabled") and proxy_cfg.get("http_proxy"):
        http_client = httpx.Client(proxy=proxy_cfg["http_proxy"], verify=ssl_verify)

    # ── Azure OpenAI（優先） ──
    az = find_azure_cfg(config)
    az_endpoint = (az.get("endpoint") or "").strip()
    az_key      = (az.get("api_key")  or "").strip()
    if az_endpoint and az_key:
        from openai import AzureOpenAI
        dep = (az.get("deployment") or az.get("default_deployment") or "gpt-4o").strip()
        kwargs: dict = {
            "azure_endpoint": az_endpoint,
            "api_key":        az_key,
            "api_version":    (az.get("api_version") or "2024-05-01-preview").strip(),
        }
        if http_client:
            kwargs["http_client"] = http_client
        return AzureOpenAI(**kwargs), dep

    # ── OpenAI フォールバック ──
    key = config.get("api_keys", {}).get("openai", "")
    if key:
        from openai import OpenAI
        kwargs = {"api_key": key}
        if http_client:
            kwargs["http_client"] = http_client
        return OpenAI(**kwargs), "gpt-4o"

    return None, None
