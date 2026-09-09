"""既存記事のarticles.extracted_textから、本文抽出時に混入した図表キャプション
（例:「Figure 1: ...」「図1：...」）を除去し直す一括更新スクリプト。

article_crawler.strip_figure_captions()を、今後クロール時に自動適用するよう
組み込んだのとあわせて、それ以前にクロール済みの既存データにも同じ処理を1回適用する
（2026-09-09、川崎さんからの要約品質指摘対応）。
"""
import sys

from article_crawler import SupabaseClient, strip_figure_captions
from config_utils import load_config


def main():
    config = load_config()
    client = SupabaseClient(config)

    rows = client.select("articles", {
        "select": "article_id,extracted_text",
        "is_current": "eq.true",
    })
    print(f"対象記事総数: {len(rows)}")

    updated = 0
    for r in rows:
        original = r.get("extracted_text") or ""
        cleaned = strip_figure_captions(original)
        if cleaned != original:
            client.update("articles", {"article_id": f"eq.{r['article_id']}"},
                          {"extracted_text": cleaned})
            updated += 1
            print(f"更新: {r['article_id']} ({len(original)}文字 → {len(cleaned)}文字)")

    print(f"更新件数: {updated}")


if __name__ == "__main__":
    sys.exit(main())
