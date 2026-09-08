# -*- coding: utf-8 -*-
"""filter_keyword_tag_map への初期データ投入（一度限りの移行スクリプト）。

filter_keywords.keyword_group と tag_reference.tag_name の対応関係を人手で
一度突き合わせ、filter_keyword_tag_map（多対多）へ登録する。
sql/2026-08-28_filter_keyword_tag_map.sql・sql/2026-08-28b_filter_coverage_policy_seed.sql
の適用後に実行すること。

対応が無い keyword_group（SJ主体系の「競合企業」「基準設定機関・国際機関」「自社」
「業界主要企業」、および複数タグにまたがり単一タグに対応しない「閾値語（横断）」）は
意図的にマッピングを作らない（対応するタグをrequiredにしないため、
check_filter_keyword_coverage.py のスコープにも含まれない）。

冪等（keyword_id, tag_id の組が既存ならスキップ）なので再実行しても安全。
"""
import sys
from article_crawler import load_config, SupabaseClient

# keyword_group -> tag_id のリスト（1グループが複数タグに寄与する場合はリストで複数指定）
GROUP_TAG_MAP = {
    "水": ["TH-01"],
    "気候変動・GHG": ["TH-02"],
    "エネルギー転換": ["TH-02"],
    "容器包装": ["TH-03"],
    "プラスチックの略称・派生": ["TH-03"],
    "原料調達": ["TH-04", "CR-06"],
    "生物多様性": ["TH-05"],
    "人権": ["TH-06", "CR-06"],
    "健康": ["TH-07"],
    "人的資本": ["TH-08"],
    "ジェンダー・DEI": ["TH-08-04", "TH-08-05"],
    "責任あるマーケティング": ["TH-09", "CR-05"],
    "情報開示": ["CR-01"],
    "ESG評価・サステナブルファイナンス": ["CR-02"],
    "地政学・貿易・供給網リスク": ["CR-03"],
}


def main():
    config = load_config()
    client = SupabaseClient(config)

    keywords = client.select("filter_keywords", {"select": "keyword_id,keyword_group"})
    valid_tag_ids = {t["tag_id"] for t in client.select("tag_reference", {"select": "tag_id"})}
    existing = client.select("filter_keyword_tag_map", {"select": "keyword_id,tag_id"})
    existing_pairs = {(r["keyword_id"], r["tag_id"]) for r in existing}

    rows = []
    skipped_unmapped_groups = set()
    for kw in keywords:
        tag_ids = GROUP_TAG_MAP.get(kw["keyword_group"])
        if not tag_ids:
            skipped_unmapped_groups.add(kw["keyword_group"])
            continue
        for tag_id in tag_ids:
            if tag_id not in valid_tag_ids:
                print(f"[WARNING] tag_reference に存在しないtag_id: {tag_id}（グループ: {kw['keyword_group']}）")
                continue
            pair = (kw["keyword_id"], tag_id)
            if pair in existing_pairs:
                continue
            rows.append({"keyword_id": kw["keyword_id"], "tag_id": tag_id})
            existing_pairs.add(pair)

    if skipped_unmapped_groups:
        print(f"マッピング対象外のkeyword_group（意図的にスキップ）: {sorted(skipped_unmapped_groups)}")

    if not rows:
        print("追加対象なし（すべて登録済み、またはマッピング対象グループなし）")
        return

    CHUNK = 200
    for i in range(0, len(rows), CHUNK):
        client.insert("filter_keyword_tag_map", rows[i:i + CHUNK], prefer="return=minimal")
    print(f"{len(rows)}件を登録しました")


if __name__ == "__main__":
    sys.exit(main())
