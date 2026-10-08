"""コンテナ起動時に環境変数からconfig.jsonを組み立てて、Gunicorn管理下の
Uvicornワーカー（複数プロセス）としてAPIサーバー(api_server.py)を起動する。

このコンテナが提供するのはAPIバックエンドのみ（FastAPI/uvicorn）。
フロントエンド(eco-digest-spark、TanStack Start)とPMOレビュー用のStreamlitアプリ
(sustainability_expert_dashboard.py)は別プロセス/別デプロイとして扱う
（DEPLOY_GUIDE.md参照。フロントエンドの本番ビルド配信は未検証のため、
現状はこのコンテナと同一ホストで`npm run dev`を動かす運用を想定）。

2026-09-29: 単一プロセス(uvicorn.run)だと、記事一覧のような重いリクエストの処理中に
他のリクエスト（軽いエンドポイントを含む）がCPU(GIL)待ちで詰まる事象を本番で確認した
ため、Gunicornに複数プロセスの管理を任せる方式に変更した。Gunicorn自体はLinux/Unix
専用（本番のAzure App ServiceはLinuxのため問題ない）。Azure側のスタートアップコマンドは
`python docker_entrypoint.py`のまま変更不要（config.json生成後、本スクリプト自身が
os.execvpでgunicornに置き換わる）。
"""
import json
import os
import sys
from pathlib import Path

from config_utils import CONFIG_PATH, build_config_from_env


def _sync_config_file(env_config: dict, config_path: Path) -> str:
    """環境変数から構築した設定で、必要に応じてconfig.jsonを書き込む。戻り値はログ出力用メッセージ。

    2026-10-08発見: 以前は「ファイルが無い時だけ生成」だったため、Azure App Service
    (Linuxは/homeが再起動・デプロイをまたいで永続化される)で一度config.jsonが
    生成された後にZYTE_API_KEY等を環境変数へ追加しても、既存のconfig.jsonが
    使われ続け、新しい環境変数が永久に反映されない不具合があった。

    env_configが空でない(= Docker/Azure Web App等、関連する環境変数が設定された
    実行環境)場合は、既存ファイルの有無によらず常に上書きし、起動のたびに
    最新の環境変数を反映する。env_configが空(= ローカル開発等、関連する環境変数が
    一切無い)場合のみ、既存のconfig.json(手動で用意したもの)をそのまま使う
    従来通りの挙動を維持する（上書きしない）。"""
    if env_config or not config_path.exists():
        config_path.write_text(json.dumps(env_config, ensure_ascii=False, indent=2), encoding="utf-8")
        return ("config.json を環境変数から生成しました（既存ファイルがあれば上書き）。" if env_config
                else "config.json を環境変数から生成しました（空の設定です）。")
    return "環境変数からは生成できないため、既存の config.json を使用します。"


def main():
    print(_sync_config_file(build_config_from_env(), CONFIG_PATH))

    os.environ.setdefault("API_HOST", "0.0.0.0")
    os.environ.setdefault("API_PORT", os.environ.get("DASHBOARD_PORT", "8000"))

    workers = os.environ.get("API_WORKERS", "2")
    timeout = os.environ.get("API_WORKER_TIMEOUT", "120")
    bind = f"{os.environ['API_HOST']}:{os.environ['API_PORT']}"
    # sys.executable経由でこのプロセスと同じ仮想環境のgunicornをモジュールとして
    # 呼び出す（PATH上のgunicornに依存しない）。execvpは現在のプロセスをgunicornで
    # 置き換える（プロセスが増えるわけではない。以降の複数ワーカーはgunicorn自身が
    # fork管理する）。
    os.execvp(sys.executable, [
        sys.executable, "-m", "gunicorn", "api_server:app",
        "-k", "uvicorn.workers.UvicornWorker",
        "-w", workers,
        "-b", bind,
        "--timeout", timeout,
    ])


if __name__ == "__main__":
    main()
