# -*- coding: utf-8 -*-
"""Secondary Redundancy Classificationの新ロジックを、既存の分析済み記事に対して
DB書き込み無しで試験実行するための検証専用スクリプト（Phase 6のサンプルレビュー用）。

対象: primary_source_status in (一次照合済み/解釈・分析/一次未確認) の記事から
無作為にN件選び、find_candidate_primary_articles→classify_secondary_redundancyを
実行して結果を表示するだけ。article_analysisへの書き込みは一切行わない。

使い方:
    python validate_secondary_redundancy.py --limit 15
"""
import argparse
import sys

from ai_client import make_openai_client
from article_crawler import load_config, SupabaseClient
import article_analyzer as aa


def main():
    parser = argparse.ArgumentParser(description="Secondary Redundancy Classificationの実データ検証")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if not azure_client:
        print("[ERROR] Azure OpenAIクライアントを初期化できません")
        sys.exit(1)

    analyses = client.select("article_analysis", {
        "select": "article_id,primary_source_status",
        "is_current": "eq.true",
        "analysis_status": "eq.処理済",
        "primary_source_status": "in.(一次照合済み,解釈・分析,一次未確認)",
        "order": "created_at.desc",
        "limit": str(args.limit * 3),
    })
    analyses = analyses[:args.limit]
    article_ids = [a["article_id"] for a in analyses]
    status_by_id = {a["article_id"]: a["primary_source_status"] for a in analyses}

    articles = client.select("articles", {
        "select": "article_id,title,extracted_text,published_at",
        "article_id": f"in.({','.join(article_ids)})",
    })
    tags_by_article: dict = {}
    for r in client.select("article_tags", {
        "select": "article_id,tag_id", "article_id": f"in.({','.join(article_ids)})",
    }):
        tags_by_article.setdefault(r["article_id"], []).append(r["tag_id"])

    print(f"検証対象: {len(articles)}件\n")
    n_matched, n_redundant, n_reporting, n_analysis, n_no_match = 0, 0, 0, 0, 0
    for a in articles:
        title = (a.get("title") or "")[:50]
        tag_ids = tags_by_article.get(a["article_id"], [])
        candidates = aa.find_candidate_primary_articles(client, a, tag_ids)
        if not candidates:
            print(f"[候補なし] {status_by_id.get(a['article_id'])}: {title}")
            n_no_match += 1
            continue
        try:
            result = aa.classify_secondary_redundancy(azure_client, model, a, candidates)
        except Exception as e:
            print(f"[判定失敗] {title}: {type(e).__name__}: {e}")
            continue
        matched = result.get("matched_primary_article_id")
        cls = result.get("classification")
        if not matched or not cls:
            print(f"[候補{len(candidates)}件だが不一致と判定] {title}")
            n_no_match += 1
            continue
        n_matched += 1
        cand_title = next((c["title"] for c in candidates if c["article_id"] == matched), "?")
        print(f"[{cls}] 二次:「{title}」 <-> 一次:「{cand_title[:40]}」")
        print(f"    理由: {result.get('reasoning')}")
        if cls == "redundant_summary":
            n_redundant += 1
        elif cls == "value_added_reporting":
            n_reporting += 1
        elif cls == "value_added_analysis":
            n_analysis += 1

    print(f"\n=== 集計 ===")
    print(f"候補なし/不一致: {n_no_match}件")
    print(f"マッチ: {n_matched}件（redundant_summary={n_redundant}, "
          f"value_added_reporting={n_reporting}, value_added_analysis={n_analysis}）")


if __name__ == "__main__":
    sys.exit(main())
