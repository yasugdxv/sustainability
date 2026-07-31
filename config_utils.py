"""
config.json 読み込み・Azure OpenAI設定解決の共通処理。

以前は article_crawler.py / run.py / docker_entrypoint.py がそれぞれ
load_config() / make_proxies() を個別実装しており、config.jsonが無い環境向けの
フォールバック生成ロジックが微妙に異なる形状のconfigを作ってしまっていた
（"api_keys.azure_openai" と "azure" の2パターンが混在し、run.py側に
両対応の吸収コードが必要になっていた）。本番Docker環境が実際に生成する
"azure"キー形式に統一し、フォールバック生成をここ一箇所にまとめる。

依存はstdlibのみ（重いクロール系ライブラリを読み込まずに済むようにするため。
docker_entrypoint.pyはconfig.json生成だけのために起動する軽量な処理なので、
article_crawler.py等を経由させたくない）。
"""
import json
import os
from pathlib import Path

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"


def build_config_from_env() -> dict:
    """config.jsonが無い実行環境向け（Docker/Azure Web App等）に環境変数から組み立てる"""
    az_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    supabase_url = os.environ.get("SUPABASE_URL", "")
    if not az_key and not supabase_url:
        return {}

    http_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY", "")
    config: dict = {
        "proxy": {"enabled": bool(http_proxy), "http_proxy": http_proxy},
        "ssl": {"verify": os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0")},
    }
    if az_key:
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Global-AzureOpenAI-DEV-gpt-4o")
        config["azure"] = {
            "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
            "api_key": az_key,
            "deployment": deployment,
            "default_deployment": deployment,
            "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        }
    config["api_keys"] = {
        "openai": os.environ.get("OPENAI_API_KEY", ""),
        "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
    }
    if supabase_url:
        service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        config["supabase"] = {
            "url": supabase_url,
            "service_role_key": service_role_key,
            "key": service_role_key or os.environ.get("SUPABASE_KEY", ""),
        }
    if os.environ.get("SUSTAINABILITY_EXPERT_ENABLED"):
        config["sustainability_expert"] = {
            "enabled": os.environ.get("SUSTAINABILITY_EXPERT_ENABLED", "").strip().lower() in ("1", "true", "yes"),
        }
    return config


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return build_config_from_env()


def make_proxies(config: dict) -> dict:
    proxy_cfg = config.get("proxy", {})
    url = proxy_cfg.get("http_proxy", "") if proxy_cfg.get("enabled") else ""
    return {"http": url, "https": url} if url else {}


def find_azure_cfg(config: dict) -> dict:
    """config.json の構造差異を吸収してAzure OpenAI設定ブロックを返す（正:azure、旧形式:api_keys.azure_openaiも許容）"""
    az = config.get("azure", {})
    if az.get("endpoint") and az.get("api_key"):
        return az
    az = config.get("api_keys", {}).get("azure_openai", {})
    if az.get("endpoint") and az.get("api_key"):
        return az
    return {}
