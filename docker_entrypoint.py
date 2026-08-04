"""コンテナ起動時に環境変数からconfig.jsonを組み立ててAPIサーバー(api_server.py)を起動する。

このコンテナが提供するのはAPIバックエンドのみ（FastAPI/uvicorn）。
フロントエンド(eco-digest-spark、TanStack Start)とPMOレビュー用のStreamlitアプリ
(sustainability_expert_dashboard.py)は別プロセス/別デプロイとして扱う
（DEPLOY_GUIDE.md参照。フロントエンドの本番ビルド配信は未検証のため、
現状はこのコンテナと同一ホストで`npm run dev`を動かす運用を想定）。
"""
import json
import os

from config_utils import CONFIG_PATH, build_config_from_env


def main():
    # /home配下はApp Service再起動をまたいで永続化されるため、config.jsonが無い場合のみ
    # 生成する方式だと環境変数を後から追加・変更しても反映されない（古いconfig.jsonが
    # 使われ続ける）。環境変数からの設定が存在する場合は常に優先して上書きする。
    env_config = build_config_from_env()
    if env_config:
        CONFIG_PATH.write_text(json.dumps(env_config, ensure_ascii=False, indent=2), encoding="utf-8")
        print("config.json を環境変数から生成しました（既存ファイルがあれば上書き）。")
    elif CONFIG_PATH.exists():
        print("既存の config.json を使用します。")
    else:
        print("環境変数・config.jsonのいずれも見つかりません。")

    os.environ.setdefault("API_HOST", "0.0.0.0")
    os.environ.setdefault("API_PORT", os.environ.get("DASHBOARD_PORT", "8000"))

    import uvicorn
    from api_server import app

    uvicorn.run(app, host=os.environ["API_HOST"], port=int(os.environ["API_PORT"]))


if __name__ == "__main__":
    main()
