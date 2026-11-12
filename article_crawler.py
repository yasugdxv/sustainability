"""互換用の入口。実体は collection/article_crawler.py に移動した（共通クロール部分）。

- import するとき: 実体と同じモジュールを返す（テストの差し替えや非公開関数の参照も従来どおり動く）
- 直接実行するとき: `python article_crawler.py ...` が従来どおり動く（run_daily.py の STEPS はこちらを呼ぶ）
"""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("collection.article_crawler", run_name="__main__", alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module("collection.article_crawler")
