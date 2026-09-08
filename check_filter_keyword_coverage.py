# -*- coding: utf-8 -*-
"""filter_keywordsのタグカバレッジ検証（PMOレビュー対応: フィルタ語彙のtag_reference
正本化）。

2026年7月に「ジェンダー」タグ（tag_referenceにはあるがfilter_keywordsには無い）という
捕捉漏れが実際に発生した。article_filter.pyのキーワードはtag_referenceと独立して
人手管理されており、この種の穴は事後に検知できない。

本スクリプトはtag_reference.filter_coverage_policyを唯一の正本としてスコープを取得する
（sustainability_article_selector.py側のPython定数[theme_major_ids等]は一切参照しない。
コード側との二重管理を避けるため）。

RLSとDB権限の前提: filter_keyword_tag_mapはRLSを有効化しているが、本スクリプトが使う
article_crawler.SupabaseClientはconfig.jsonのsupabase.key（service_role鍵）を使用して
おり、service_roleはRLSを常にバイパスする（external_intelligence_calls等、既存のRLS
有効化済みテーブルと同じ前提。追加のSELECT policyは不要）。

将来の申し送り事項（今回は未実装）: もし将来「フィルタ除外記事を完全非保存（物理削除）」
に戻す場合は、以下をGate条件として必須とする。
    1. Coverage Check NG（holesが1件でもある）時は、article_filter.pass_filter()による
       キーワードフィルタリング自体を実行しない（全件analyzeへ通す）フェイルセーフ
    2. 有効なtag_referenceについてfilter_coverage_policyの棚卸しが完了し、
       unreviewedが0件であること
（article_analyzer.pyのdelete_filtered_article()のdocstringにも同内容を明記している）

使い方: python check_filter_keyword_coverage.py
    holesが1件でもあれば非ゼロ終了（run_daily.pyの失敗通知の対象）。
    unreviewedのみの場合は一覧を出力するが終了コードは0のまま
    （棚卸し未了の大量の既存タグが毎日同じ内容の失敗通知を生み続けるのを防ぐため）。
"""
import sys

from article_crawler import load_config, SupabaseClient


def check_filter_keyword_coverage(client) -> dict:
    """tag_reference.filter_coverage_policyを唯一の正本としてscopeを取得する。
    - policy='required'かつ有効なfilter_keyword_tag_map紐付けが無い → holes（穴）
    - policy='exempt' → チェック対象外
    - policy='unreviewed' → unreviewed（設定未完了、別カテゴリとして報告）
    親タグにキーワードがあっても、'required'の子タグ自身に紐付けが無ければ
    holesとして検出する（2026年7月のジェンダー事象の再発防止）。
    戻り値: {"holes": [...], "unreviewed": [...]}"""
    all_tags = client.select("tag_reference", {
        "select": "tag_id,tag_name,filter_coverage_policy", "status": "eq.有効",
    })
    active_keyword_ids = {
        k["keyword_id"] for k in client.select("filter_keywords", {"select": "keyword_id", "status": "eq.有効"})
    }
    mapped_rows = client.select("filter_keyword_tag_map", {"select": "tag_id,keyword_id"})
    covered_tag_ids = {m["tag_id"] for m in mapped_rows if m["keyword_id"] in active_keyword_ids}

    holes = [t for t in all_tags
             if t["filter_coverage_policy"] == "required" and t["tag_id"] not in covered_tag_ids]
    unreviewed = [t for t in all_tags if t["filter_coverage_policy"] == "unreviewed"]
    return {"holes": holes, "unreviewed": unreviewed}


def main():
    config = load_config()
    client = SupabaseClient(config)
    result = check_filter_keyword_coverage(client)

    if result["unreviewed"]:
        print(f"[WARNING] filter_coverage_policy未設定(unreviewed): {len(result['unreviewed'])}件")
        for t in result["unreviewed"]:
            print(f"  - {t['tag_id']} {t['tag_name']}")

    if result["holes"]:
        print(f"[ERROR] Coverage Hole検出: {len(result['holes'])}件")
        for t in result["holes"]:
            print(f"  - {t['tag_id']} {t['tag_name']}")
        sys.exit(1)

    print("[OK] Coverage Hole無し")


if __name__ == "__main__":
    main()
