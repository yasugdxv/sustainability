"""互換用の入口。実体は collection/crawl_health_check.py に移動した（共通クロール部分）。

- import するとき: 実体と同じモジュールを返す（テストの差し替えや非公開関数の参照も従来どおり動く）
- 直接実行するとき: `python crawl_health_check.py ...` が従来どおり動く（run_daily.py の STEPS はこちらを呼ぶ）
"""
import sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("collection.crawl_health_check", run_name="__main__", alter_sys=True)
else:
    import importlib
    sys.modules[__name__] = importlib.import_module("collection.crawl_health_check")
