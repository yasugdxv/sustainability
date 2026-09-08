# -*- coding: utf-8 -*-
"""未タグ記事（article_tagsに1件も無い、2026-07-22分析分が中心）を、現在のコード・
現在のtag_referenceで再分析し、タグ・重要度を付け直すバックフィルスクリプト。

診断（5件サンプル再分析）で、現在のコードでは正しくタグが付くことを確認済み。
旧いarticle_analysis行はis_current=falseに更新してから新しい行を追加する
（article_analyzer.pyのsave_analysisは新規分析のみを想定しており、再分析時の
is_current切り替えは行わないため、ここで明示的に処理する）。
"""
import sys
import time
from article_crawler import load_config, SupabaseClient
from ai_client import make_openai_client
import article_analyzer as aa

LOG_PATH = "backfill_progress.log"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)

    tags = aa.load_tags(client)
    tag_text = aa.build_tag_text(tags)
    rubric_text = aa.load_rubric_text(client)
    valid_tag_ids = {t["tag_id"] for t in tags}
    ranks = client.select("importance_rank_definitions", {"select": "*"})

    analysis = client.select("article_analysis", {
        "select": "analysis_id,article_id,importance_level",
        "is_current": "eq.true",
    })
    article_tags = client.select("article_tags", {"select": "article_id"})
    tagged_ids = {r["article_id"] for r in article_tags}
    untagged_analysis = [a for a in analysis if a["article_id"] not in tagged_ids]

    articles = {a["article_id"]: a for a in client.select("articles", {
        "select": "article_id,title,extracted_text,published_at,final_url,fetched_url,crawl_target_id",
        "is_current": "eq.true",
    })}
    targets = {t["crawl_target_id"]: t for t in client.select(
        "crawl_targets", {"select": "crawl_target_id,publisher_name,domain"})}

    total = len(untagged_analysis)
    log(f"バックフィル対象: {total}件")

    ok, failed, tag_gained = 0, 0, 0
    for i, old in enumerate(untagged_analysis, start=1):
        article_id = old["article_id"]
        article = articles.get(article_id)
        if not article:
            log(f"  [{i}/{total}] skip (article not found): {article_id}")
            continue
        target = targets.get(article["crawl_target_id"], {})
        try:
            result = aa.analyze_article(azure_client, model, rubric_text, tag_text, article, target)
            # 旧い現行行をis_current=falseへ切り替えてから新しい分析行を追加する
            client.update("article_analysis",
                           {"article_id": f"eq.{article_id}", "is_current": "eq.true"},
                           {"is_current": False})
            saved_tags = aa.save_analysis(client, article, result, ranks, model, valid_tag_ids)
            ok += 1
            if saved_tags:
                tag_gained += 1
            log(f"  [{i}/{total}] OK tags={len(saved_tags)} {article.get('title', '')[:40]}")
        except Exception as e:
            failed += 1
            log(f"  [{i}/{total}] ERROR {type(e).__name__}: {e} ({article_id})")

    log(f"完了: 成功={ok} 失敗={failed} タグ獲得={tag_gained}/{ok}")


if __name__ == "__main__":
    sys.exit(main())
