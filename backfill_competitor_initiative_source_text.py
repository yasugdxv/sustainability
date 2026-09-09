"""既存のcompetitor_initiativesにsource_text(詳細ページ表示用の本文該当箇所)を
バックフィルするスクリプト。

背景: 「取組事例」の検出は1つのsource_url(企業のサステナビリティページ等)から
複数のレコード(TARGET/INITIATIVE等)がまとめて抽出される設計のため、単純に
ページ全体の本文を保存すると、同じsource_urlの別の取組事例でも同じ本文が
表示されてしまう。competitor_classifier.classify_and_extract()を再実行して、
各INITIATIVEに対応するevidence_quote(その取組事例に関する本文該当箇所のみ)を
取得し、既存レコードとタイトル一致で紐づけて補完する（2026-09-09対応）。

タイトルが完全一致しない場合は補完できないため、対象外としてログに出力する
（LLM再抽出は非決定的なため、表記が変わることがある）。
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import competitor_classifier as classifier
from ai_client import make_openai_client
from article_crawler import SupabaseClient, extract_article
from config_utils import load_config, make_proxies


def main():
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)

    classifier.load_goal_categories(client)

    rows = client.select("competitor_initiatives", {
        "select": "initiative_id,company_id,title,source_url",
    })
    print(f"対象取組事例総数: {len(rows)}")

    by_url: dict[str, list] = {}
    for r in rows:
        url = r.get("source_url")
        if url:
            by_url.setdefault(url, []).append(r)
    print(f"ユニークURL数: {len(by_url)}")

    companies = {c["company_id"]: c for c in client.select("competitor_companies", {"select": "*"})}

    matched_total = 0
    unmatched_total = 0
    for i, (url, initiatives) in enumerate(by_url.items(), start=1):
        company_name = companies.get(initiatives[0]["company_id"], {}).get("company_name", "")
        print(f"[{i}/{len(by_url)}] {company_name} | {url}")
        try:
            extracted = extract_article(url, proxies, verify)
            if not extracted.get("ok") or not extracted.get("text"):
                print("  本文取得失敗、スキップ")
                unmatched_total += len(initiatives)
                continue
            records = classifier.classify_and_extract(azure_client, model, company_name, extracted["text"])
        except Exception as e:
            print(f"  再抽出失敗: {type(e).__name__}: {e}")
            unmatched_total += len(initiatives)
            continue

        new_by_title = {
            rec["title"].strip().casefold(): rec.get("evidence_quote")
            for rec in records if rec.get("record_type") == "INITIATIVE" and rec.get("evidence_quote")
        }

        for ini in initiatives:
            quote = new_by_title.get(ini["title"].strip().casefold())
            if quote:
                client.update("competitor_initiatives", {"initiative_id": f"eq.{ini['initiative_id']}"},
                              {"source_text": quote})
                matched_total += 1
            else:
                print(f"  未マッチ: {ini['title'][:40]}")
                unmatched_total += 1
        time.sleep(0.3)

    print(f"補完件数: {matched_total} / 未マッチ: {unmatched_total} / 総数: {len(rows)}")


if __name__ == "__main__":
    sys.exit(main())
