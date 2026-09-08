# -*- coding: utf-8 -*-
"""sustainablejapan.jp等、有料会員限定記事で本文プレビューの途中から会員登録・ログイン
誘導の定型文に切り替わり、それがそのままextracted_textに収録されてしまっていた記事を
修復するワンショットスクリプト。

article_crawler._truncate_at_paywall()の追加（2026-09-03）により新規クロール分は
再発しないが、既に保存済みの記事はDBに残ったままのため、既存のextracted_textに対して
同じ関数を適用してUPDATEするだけで直せる（再クロール不要、pure string変換のため）。

使い方:
    python repair_paywall_boilerplate.py --dry-run   # 対象件数の確認のみ
    python repair_paywall_boilerplate.py             # 実際に修復する
"""
import argparse
import sys

import article_crawler
from article_crawler import load_config, SupabaseClient


def main():
    parser = argparse.ArgumentParser(description="有料登録定型文混入記事の修復")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    client = SupabaseClient(config)

    articles = client.select("articles", {
        "select": "article_id,title,extracted_text", "is_current": "eq.true",
    })
    targets = [
        a for a in articles if a.get("extracted_text")
        and article_crawler._truncate_at_paywall(a["extracted_text"]) != a["extracted_text"]
    ]
    print(f"対象件数: {len(targets)}件")
    if args.dry_run:
        return

    fixed = 0
    for a in targets:
        new_text = article_crawler._truncate_at_paywall(a["extracted_text"])
        if not new_text.strip():
            print(f"  [スキップ] {a['article_id']} 定型文除去後に本文が空になるため据え置き: "
                  f"{(a.get('title') or '')[:40]}")
            continue
        client.update("articles", {"article_id": f"eq.{a['article_id']}"}, {"extracted_text": new_text})
        fixed += 1

    print(f"完了: 修復{fixed}件 / スキップ{len(targets) - fixed}件")


if __name__ == "__main__":
    sys.exit(main())
