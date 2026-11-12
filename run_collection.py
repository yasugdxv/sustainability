"""共通クロール部分だけを実行する入口（クロール → 参照リスト → API記事 → ヘルスチェック）。

サステナの run_daily.py は、同じ工程の一覧(collection/steps.py)を使って、続けてサステナ独自の
処理（分析・選定など）を実行する。このスクリプトは、共通クロールだけを単独で動かしたいとき用。
（切り出し後は、このスクリプトが新しいアプリの日次の入口になる）

使い方:
    python run_collection.py                  # 全工程
    python run_collection.py --skip-crawl     # 指定した工程だけ飛ばす（--skip-<工程名>）
"""
import os
import sys
from pathlib import Path

from collection.pipeline import main
from collection.steps import COLLECTION_STEPS

BASE = Path(__file__).parent
# Azure WebJob は WEBJOBS_NAME を自動設定する。ジョブ単位でロックを分け、run_daily とは別のロックにする
LOCK_PATH = BASE / f"run_collection_{os.environ.get('WEBJOBS_NAME', 'default')}.lock"


def _notify(results: list) -> None:
    """失敗の通知。サステナの run_daily と同じ通知（メール）を使う。切り出し後は、新しいアプリ側で差し替える。"""
    try:
        import run_daily
        run_daily.notify_failures(results)
    except Exception as e:
        print(f"失敗通知の送信自体に失敗しました: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main(COLLECTION_STEPS, base=BASE, lock_path=LOCK_PATH, notify=_notify))
