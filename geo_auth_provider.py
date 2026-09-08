"""
Geo Intelligence Client向けの認証方式の差し替えポイント。

現時点はAPIキー方式（geopolitical_monitor_dashboard側の実装を確認済み:
geo_intelligence_auth.py の verify_api_key() が Header "X-API-Key" を
FastAPIのHeader()パラメータで直接検証している。Authorization: Bearer 形式では
ない）。将来Entra ID / Managed Identity / Client Credentials等へ切り替える際は、
GeoAuthProviderを継承した新クラスを追加し、GeoIntelligenceClientへの注入を
差し替えるだけで済むようにする。

本リポジトリに abc.ABC の前例が無いため、正式な抽象基底クラスにはせず
Duck Typing（get_auth_headers()さえ実装すればよい）にとどめ、過剰設計を避ける。
"""

GEO_API_KEY_HEADER = "X-API-Key"


class GeoAuthProvider:
    """認証ヘッダーを作るだけの最小Interface。"""

    def get_auth_headers(self) -> dict:
        raise NotImplementedError


class ApiKeyAuthProvider(GeoAuthProvider):
    """現行方式: 固定APIキーを X-API-Key ヘッダーに載せる。"""

    def __init__(self, api_key: str, header_name: str = GEO_API_KEY_HEADER):
        self.api_key = api_key
        self.header_name = header_name

    def get_auth_headers(self) -> dict:
        if not self.api_key:
            return {}
        return {self.header_name: self.api_key}


class NullAuthProvider(GeoAuthProvider):
    """APIキー未設定時のフォールバック（ヘッダーなし）。
    geopolitical_monitor_dashboard側もAPIキー未設定時は開発環境とみなし
    認証をスキップする実装のため、開発環境同士での疎通に支障はない。"""

    def get_auth_headers(self) -> dict:
        return {}
