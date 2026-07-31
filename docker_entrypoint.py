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
    if not CONFIG_PATH.exists():
        config = build_config_from_env()
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        print("config.json を環境変数から生成しました。")
    else:
        print("既存の config.json を使用します。")

    os.environ.setdefault("API_HOST", "0.0.0.0")
    os.environ.setdefault("API_PORT", os.environ.get("DASHBOARD_PORT", "8000"))

    import uvicorn
    from api_server import app

    uvicorn.run(app, host=os.environ["API_HOST"], port=int(os.environ["API_PORT"]))


if __name__ == "__main__":
    main()
