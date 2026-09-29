"""日次パイプライン(run_daily.py)完了後に、記事一覧ダッシュボードの共有キャッシュを温める。

api_server.py(Gunicorn複数ワーカー)は、記事一覧の取得結果を
cache/shared_api_cache/ 配下のファイルを介してワーカー間で共有している
（sustainability_expert_common.shared_cache_get/set）。記事データの実体は
このバッチ処理でしか更新されないため、ここで先回りしてキャッシュを書いておけば、
ユーザーの最初のリクエストがDBからの数万件規模の再取得（数十秒〜1分規模）を
踏まずに済む。

失敗してもパイプライン全体は止めない（run_daily.py側で他ステップと同様に
1ステップの失敗として扱われるのみで、次回のリクエスト時に通常のキャッシュミス
経路で取得される）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config_utils import load_config  # noqa: E402
import sustainability_dashboard_core as core  # noqa: E402
import sustainability_expert_common as common  # noqa: E402

DEFAULT_LOOKBACK_DAYS = 30  # api_server.pyのDEFAULT_LOOKBACK_DAYSと同じ値


def main():
    config = load_config()
    data = core.fetch_dashboard_articles(config, DEFAULT_LOOKBACK_DAYS)
    common.shared_cache_set(f"articles:{DEFAULT_LOOKBACK_DAYS}", data)
    print(f"[warm_dashboard_cache] since_days={DEFAULT_LOOKBACK_DAYS}: {len(data)}件をキャッシュしました。")


if __name__ == "__main__":
    main()
