"""共通クロール部分の日次工程の一覧。

(工程名, python への引数, タイムアウト秒)。各工程は独立したサブプロセスとして実行される。
`-m collection.xxx` の形で呼ぶので、collection/ を別アプリへ持っていっても、そのまま動く
（ルート直下の互換用の入口には依存しない）。

サステナの run_daily.py も、この一覧をそのまま使う（工程名・--skip-<工程名> は従来どおり）。
"""

COLLECTION_STEPS = [
    ("crawl", ["-m", "collection.article_crawler"], 21600),                 # 通常クロール（RSS/HTML/ブラウザ操作）
    ("reference_list", ["-m", "collection.reference_list_monitor"], 900),   # 参照リスト差分検知
    ("api_articles", ["-m", "collection.api_article_crawler"], 900),        # 規制系記事API
    ("crawl_health_check", ["-m", "collection.crawl_health_check"], 300),   # クロール結果の健全性チェック
]
