# -*- coding: utf-8 -*-
"""未翻訳の記事タイトル・要約を、稼働中のapi_server.pyへ順にリクエストして
先回り翻訳するワンショットスクリプト。

背景: 一覧表示（GET /api/articles）は「未翻訳ならとりあえず原文を返しつつ裏で翻訳する」
fire-and-forget方式のため、誰もアクセスしたことが無い記事は翻訳されないまま残る。
今回、検索画面に期間フィルター（3ヶ月・全期間）を追加したことで、今まで画面に
出てこなかった古い記事が一気に露出し、その分の未翻訳記事が目立つようになった。

本スクリプトはDB書き込みは行わず、稼働中のAPIサーバーに対して
GET /api/articles/{id}?lang=ja（ブロッキング翻訳）を1件ずつ叩くことで、
サーバープロセス内の翻訳キャッシュ（sustainability_dashboard_core._short_cache）を
温める。translation_cacheテーブル（DB永続化）が適用済みならそこにも書き込まれる。

使い方:
    python backfill_translations.py                  # since_days=36500(実質全期間)
    python backfill_translations.py --since-days 90
    python backfill_translations.py --limit 50        # 動作確認用
"""
import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API_BASE = "http://127.0.0.1:8000"
JP_PAT = re.compile(r"[぀-ヿ一-鿿]")


def needs_translation(title: str) -> bool:
    return bool(title) and not JP_PAT.search(title) and "文字化け" not in title


def main():
    parser = argparse.ArgumentParser(description="未翻訳記事の先回り翻訳バックフィル")
    parser.add_argument("--since-days", type=int, default=36500)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()

    resp = requests.get(f"{API_BASE}/api/articles",
                         params={"since_days": args.since_days, "lang": "ja"}, timeout=120,
                         proxies={"http": None, "https": None})
    resp.raise_for_status()
    articles = resp.json()["articles"]
    targets = [a["id"] for a in articles if needs_translation(a["title"])]
    if args.limit:
        targets = targets[:args.limit]
    print(f"対象記事数: {len(targets)}件（全体{len(articles)}件中）")

    def _translate_one(article_id: str):
        try:
            r = requests.get(f"{API_BASE}/api/articles/{article_id}", params={"lang": "ja"}, timeout=60,
                              proxies={"http": None, "https": None})
            r.raise_for_status()
            return article_id, True, None
        except Exception as e:
            return article_id, False, f"{type(e).__name__}: {e}"

    ok, failed = 0, 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(_translate_one, aid): aid for aid in targets}
        for i, future in enumerate(as_completed(futures), 1):
            article_id, success, error = future.result()
            if success:
                ok += 1
            else:
                failed += 1
                print(f"  [{i}/{len(targets)}] {article_id} 失敗: {error}")
            if i % 50 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)}件完了（成功{ok} / 失敗{failed}）")

    print(f"\n完了: 成功{ok}件 / 失敗{failed}件")


if __name__ == "__main__":
    sys.exit(main())
