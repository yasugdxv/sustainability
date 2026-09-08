# -*- coding: utf-8 -*-
"""社内プロキシの瞬断等により文字化けしたまま保存されてしまった記事
（articles.title / extracted_text）を、対象URLを再取得して修復するワンショットスクリプト。

article_crawler.extract_article()は2026-09-02以降、取得直後に文字化けを検知して
最大2回リトライするようになった（新規クロール分の再発防止）。本スクリプトは
その修正が入る前に既に保存されてしまった過去分を、同じ検知ロジックで洗い出し、
final_urlを再取得して直せるものだけ直す（新しい取得も文字化けなら記事は変更せず
スキップし、既存のUIフォールバック表示に任せる）。

記事本体を削除することは無い（案A: 直せなければそのまま残す）。
article_analysis（タグ・重要度・要約）は本スクリプトの対象外
（タイトル・本文が直ったことで既存の分析結果と多少ずれる可能性はあるが、
再分析するかは別途判断してもらう）。

使い方:
    python repair_garbled_articles.py             # 実際に修復する
    python repair_garbled_articles.py --dry-run    # 対象件数の確認のみ、DB書き込み無し
"""
import argparse
import sys

import article_crawler
import sustainability_dashboard_core as core
from article_crawler import load_config, SupabaseClient


def find_garbled_articles(client: SupabaseClient) -> list:
    rows = client.select("articles", {
        "select": "article_id,title,extracted_text,final_url,fetched_url",
        "is_current": "eq.true",
    })
    garbled = []
    for r in rows:
        title = core.repair_mojibake(r.get("title") or "")
        if core._looks_garbled(title):
            garbled.append(r)
    return garbled


def main():
    parser = argparse.ArgumentParser(description="文字化け記事の再取得・修復")
    parser.add_argument("--dry-run", action="store_true", help="対象件数の確認のみ行い、書き込みは行わない")
    args = parser.parse_args()

    config = load_config()
    client = SupabaseClient(config)
    proxies = article_crawler.make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)

    targets = find_garbled_articles(client)
    print(f"文字化けが疑われる記事: {len(targets)}件")
    if args.dry_run:
        return

    fixed, still_broken, failed = 0, 0, 0
    for i, a in enumerate(targets, 1):
        url = a.get("final_url") or a.get("fetched_url")
        print(f"[{i}/{len(targets)}] {a['article_id']} {url} ...", end=" ", flush=True)
        if not url:
            print("final_url/fetched_urlが無いためスキップ")
            failed += 1
            continue
        result = article_crawler.extract_article(url, proxies, verify)
        if not result.get("ok"):
            print(f"再取得失敗: {result.get('error')}")
            failed += 1
            continue
        new_title = result.get("title") or ""
        new_text = result.get("text") or ""
        if core._looks_garbled(new_title) or result.get("encoding_suspect"):
            print("再取得しても文字化けのまま。スキップ（既存のUIフォールバック表示のまま残す）")
            still_broken += 1
            continue
        client.update("articles", {"article_id": f"eq.{a['article_id']}"}, {
            "title": new_title,
            "extracted_text": new_text,
        })
        print(f"修復完了: {new_title[:40]}")
        fixed += 1

    print(f"\n完了: 修復{fixed}件 / 再取得しても文字化け{still_broken}件 / 取得失敗{failed}件")


if __name__ == "__main__":
    sys.exit(main())
