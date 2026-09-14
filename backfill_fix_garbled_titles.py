# -*- coding: utf-8 -*-
"""既存記事のarticles.titleが文字化けして復元不能なものを、extract_article()で
再取得し直す一括更新スクリプト。

repair_mojibake()で修復できる「UTF-8をLatin-1と誤解釈」パターンとは別に、RSS経由の
記事でtrafilaturaのtitle抽出が空になりRSS仮タイトルへフォールバックする際、文字化け
チェック（_looks_encoding_corrupted、プロキシ瞬断対策）が未適用だった不具合が原因で
保存されてしまった記事が対象（article_crawler.list_rss_candidates()側は修正済み、
2026-09-14）。今回はその不具合で既に保存されてしまった過去分を直す。

使い方:
    python backfill_fix_garbled_titles.py
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sustainability_dashboard_core as core
from article_crawler import SupabaseClient, extract_article, make_proxies
from config_utils import load_config

REQUEST_INTERVAL_SEC = 0.5


def find_garbled_articles(client: SupabaseClient) -> list:
    rows = client.select("articles", {
        "select": "article_id,title,final_url,fetched_url",
        "is_current": "eq.true",
    })
    targets = []
    for r in rows:
        repaired = core.repair_mojibake(r.get("title") or "")
        if core._looks_garbled(repaired):
            targets.append(r)
    return targets


def main():
    config = load_config()
    client = SupabaseClient(config)
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)

    targets = find_garbled_articles(client)
    print(f"対象記事総数: {len(targets)}")

    fixed = still_broken = 0
    for r in targets:
        url = r.get("final_url") or r.get("fetched_url") or ""
        if not url:
            still_broken += 1
            print(f"URL無し、スキップ: {r['article_id']}")
            continue

        result = extract_article(url, proxies, verify)
        new_title = (result.get("title") or "").strip()
        if result.get("ok") and new_title and not core._looks_garbled(core.repair_mojibake(new_title)):
            client.update("articles", {"article_id": f"eq.{r['article_id']}"}, {"title": new_title})
            fixed += 1
            print(f"修復: {r['article_id']} → {new_title[:60]}")
        else:
            still_broken += 1
            print(f"未修復(再取得でも文字化け/失敗): {r['article_id']} url={url}")
        time.sleep(REQUEST_INTERVAL_SEC)

    print(f"\n修復件数: {fixed} / 未修復: {still_broken} / 対象合計: {len(targets)}")


if __name__ == "__main__":
    sys.exit(main())
