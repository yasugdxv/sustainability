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

from config_utils import CONFIG_PATH, build_config_from_env


def main():
    if not CONFIG_PATH.exists():
        config = build_config_from_env()
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        print("config.json を環境変数から生成しました。")
    else:
        print("既存の config.json を使用します。")

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
