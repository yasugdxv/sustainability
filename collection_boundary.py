"""外部情報収集（共通クロール）部分の境界の定義。

サステナアプリの中で、処理を「共通クロール部分」と「サステナ独自の処理」に分け、
共通クロール部分をいつでもアプリとして切り出せる状態に保つための、唯一の一覧。

ルール（tests/test_collection_boundary.py が検査する）:
  共通クロール部分のモジュールは、サステナ独自のモジュールを import してはいけない。
  （共通 → 共通、または標準ライブラリ・外部パッケージのみ）
  サステナ独自の処理から、共通クロール部分を使うのは自由。

共通クロール部分を増やす・減らすときは、このファイルだけを直す。
"""

# 共通クロール部分のモジュール（ルート直下の .py の名前、拡張子なし）
COMMON_MODULES = [
    "config_utils",          # 設定の読み込み（config.json / 環境変数）
    "supabase_client",       # Supabase(PostgREST)の薄いラッパー
    "article_crawler",       # 記事の取得・抽出・段階的な再取得（通常→Zyte→TinyFish）と保存
    "api_article_crawler",   # API 型ソースの記事取得
    "reference_list_monitor",  # 参照リスト（規制・基準などの一覧）の監視
    "crawl_health_check",    # クロール結果の健全性チェック
]

# 共通クロール部分が読み書きするテーブル（説明用。切り出し時の移行対象）
COMMON_TABLES = [
    "crawl_targets",   # クロール先（Source）
    "article_urls",    # 見つけた記事URL
    "articles",        # 記事本文
    "article_files",   # 添付ファイル（PDF等）
    "crawl_logs",      # クロールの実行ログ
]
