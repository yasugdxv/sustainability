"""外部情報収集（共通クロール）部分の境界の定義。

サステナアプリの中で、処理を「共通クロール部分」と「サステナ独自の処理」に分け、
共通クロール部分をいつでもアプリとして切り出せる状態に保つための、唯一の一覧。

配置:
  collection/   共通クロール部分の実体（切り出すときは、このフォルダを持っていく）
  ルート直下の article_crawler.py などは、従来の import・直接実行(run_daily.py の STEPS 等)を
  壊さないための薄い入口（実体は collection/ にある）。

ルール（tests/test_collection_boundary.py が検査する）:
  共通クロール部分のモジュールは、次のものだけを import できる。
    ・標準ライブラリ・外部パッケージ
    ・collection パッケージ内のモジュール（`from collection.xxx import ...` の形で書く）
    ・HOST_PROVIDED（アプリ側が用意する部品。切り出し先のアプリで差し替える）
  サステナ独自のモジュールや、ルート直下の入口（article_crawler 等）を import してはいけない。
  サステナ独自の処理から、共通クロール部分を使うのは自由。

共通クロール部分を増やす・減らすときは、このファイルだけを直す。
"""

# 共通クロール部分のモジュール（collection/ 直下の .py の名前、拡張子なし）
COMMON_MODULES = [
    "supabase_client",       # Supabase(PostgREST)の薄いラッパー
    "article_crawler",       # 記事の取得・抽出・段階的な再取得（通常→Zyte→TinyFish）と保存
    "api_article_crawler",   # API 型ソースの記事取得
    "reference_list_monitor",  # 参照リスト（規制・基準などの一覧）の監視
    "crawl_health_check",    # クロール結果の健全性チェック
    "consumers",             # クロール先(Source)を使うアプリの扱い（crawl_targets.used_by）
    "steps",                 # 日次の工程の一覧（クロール→参照リスト→API記事→ヘルスチェック）
    "pipeline",              # 工程を順番に実行する仕組み（ロック・失敗しても続行・スキップ指定）
]

# アプリ側が用意する部品（共通クロール部分が import してよい、ルート直下のモジュール）。
# 切り出し先のアプリでは、同じ名前・同じ関数（load_config / make_proxies / BASE）で用意する。
# 設定（config.json・環境変数）の置き場所は、当面サステナのまま。
HOST_PROVIDED = [
    "config_utils",
]

# 共通クロール部分が読み書きするテーブル（説明用。切り出し時の移行対象）
COMMON_TABLES = [
    "crawl_targets",   # クロール先（Source）
    "article_urls",    # 見つけた記事URL
    "articles",        # 記事本文
    "article_files",   # 添付ファイル（PDF等）
    "crawl_logs",      # クロールの実行ログ
]
