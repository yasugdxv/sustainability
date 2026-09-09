"""文字化けしたタイトルを持つ既存記事を、元記事URL(final_url)から再クロールして
正しいタイトル・本文に更新する一括修復スクリプト。

背景: 一部の記事でHTTPレスポンスのContent-Typeにcharsetが無く、requestsが
ISO-8859-1と誤判定してタイトル・本文が文字化けしたままDBに保存されていた。
article_crawler._decoded_html()でapparent_encodingにフォールバックする対策は
既に実装済みだが、対策実装前にクロール済みのデータはそのまま残っているため、
このスクリプトで再取得して直す（2026-09-09、川崎さんからの不具合報告対応）。
"""
import sys
import time

from article_crawler import SupabaseClient, extract_article, strip_figure_captions
from config_utils import load_config, make_proxies
from sustainability_dashboard_core import _looks_garbled


def main():
    config = load_config()
    client = SupabaseClient(config)
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)

    rows = client.select("articles", {
        "select": "article_id,title,final_url",
        "is_current": "eq.true",
    })
    garbled = [r for r in rows if r.get("title") and _looks_garbled(r["title"])]
    print(f"対象記事数: {len(garbled)}")

    fixed = 0
    still_failed = 0
    for i, r in enumerate(garbled, start=1):
        try:
            result = extract_article(r["final_url"], proxies, verify)
        except Exception as e:
            print(f"[{i}/{len(garbled)}] 取得失敗: {r['article_id']} ({type(e).__name__}: {e})")
            still_failed += 1
            continue

        new_title = result.get("title") or ""
        new_text = strip_figure_captions(result.get("text") or "")
        if not result.get("ok") or not new_title or _looks_garbled(new_title):
            print(f"[{i}/{len(garbled)}] 修復できず: {r['article_id']}")
            still_failed += 1
            continue

        client.update("articles", {"article_id": f"eq.{r['article_id']}"}, {
            "title": new_title,
            "extracted_text": new_text,
        })
        fixed += 1
        print(f"[{i}/{len(garbled)}] 修復: {r['article_id']} -> {new_title[:40]}")
        time.sleep(0.3)  # 相手サイトへの連続アクセス負荷を抑える

    print(f"修復件数: {fixed} / 修復できず: {still_failed} / 総数: {len(garbled)}")


if __name__ == "__main__":
    sys.exit(main())
