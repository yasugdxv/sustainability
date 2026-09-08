# -*- coding: utf-8 -*-
"""tag_reference へ下位軸（下位種別大分類＋5小分類）を追加する（2026-07-31 PMO回答2.3節対応）。

要件定義案（デAI本部提示版・2.5節）で「主要9軸＋横断2軸＋下位5軸」と定義されていたが、
tag_referenceへの実装（データ投入）が漏れていたことが判明したため、ここで追加する。
既存のテーマ軸9大分類とは別に、テーマ軸の10番目の大分類「下位種別」を新設し、
その配下に5小分類として追加する（既存の大分類/小分類の親子構造をそのまま流用、スキーマ変更なし）。

sql/2026-07-15_tag_reference_seed_v2.sql と同じ列構成・記述スタイルに合わせている。
再実行しても安全（既存のtag_idと重複するものはスキップ）。
"""
import sys
from article_crawler import load_config, SupabaseClient

# (tag_id, tag_code, tag_name, parent_tag_id, tag_meaning, tag_criteria, display_order)
ROWS = [
    ("TH-10", "10", "下位種別", None,
     "主要9軸に含まれない補助的なテーマ区分。常時収集の対象とせず、"
     "下位5軸それぞれの監視閾値（3条件）に該当する場合のみ収集する",
     "製品品質・ガバナンス・ルールメーキング・データセキュリティ・アニマルウェルフェアの"
     "いずれかに該当する場合", 118),
    ("TH-10-01", "10-01", "製品品質", "TH-10",
     "製品の安全性・品質管理に関する規制・重大事案",
     "リコール、food safety alert等、製品品質に関する規制新設・重大事案を扱う場合", 119),
    ("TH-10-02", "10-02", "ガバナンス", "TH-10",
     "コーポレートガバナンス（取締役会構成・報酬・株主対応等）に関する規制・基準の改定",
     "取締役会構成、役員報酬、株主対応等に関する規制・基準の新設・改定を扱う場合", 120),
    ("TH-10-03", "10-03", "ルールメーキング", "TH-10",
     "業界団体・企業によるルール形成、標準化、ロビイングに関する動き",
     "業界標準の策定、ロビー活動、団体間のルール形成を扱う場合", 121),
    ("TH-10-04", "10-04", "データセキュリティ", "TH-10",
     "データ保護・プライバシー・情報セキュリティに関する規制の新設・改定、重大インシデント",
     "データ保護規制の新設・改定、業界を揺るがす重大な漏えい事案を扱う場合"
     "（一般的なインシデント報道やセキュリティベンダーの動向は対象外）", 122),
    ("TH-10-05", "10-05", "アニマルウェルフェア", "TH-10",
     "動物福祉に関する規制・基準・イニシアチブの動向",
     "動物福祉に関する規制新設・基準改定・重要イニシアチブを扱う場合", 123),
]


def main():
    config = load_config()
    client = SupabaseClient(config)

    existing = client.select("tag_reference", {"select": "tag_id"})
    existing_ids = {r["tag_id"] for r in existing}

    rows = []
    for tag_id, tag_code, tag_name, parent_tag_id, tag_meaning, tag_criteria, display_order in ROWS:
        if tag_id in existing_ids:
            print(f"skip（既存）: {tag_id}")
            continue
        rows.append({
            "tag_id": tag_id,
            "tag_axis": "テーマ",
            "tag_level": "大分類" if parent_tag_id is None else "小分類",
            "tag_code": tag_code,
            "tag_name": tag_name,
            "parent_tag_id": parent_tag_id,
            "tag_meaning": tag_meaning,
            "tag_criteria": tag_criteria,
            "display_order": display_order,
            "status": "有効",
        })

    if not rows:
        print("追加対象なし（すべて登録済み）")
        return

    # 自己参照FK（parent_tag_id）のため、親(大分類)を先にコミットしてから子(小分類)を入れる
    parents = [r for r in rows if r["parent_tag_id"] is None]
    children = [r for r in rows if r["parent_tag_id"] is not None]
    if parents:
        client.insert("tag_reference", parents)
    if children:
        client.insert("tag_reference", children)
    print(f"{len(rows)}件を登録しました")
    for r in rows:
        print(f"  {r['tag_id']} {r['tag_name']}")


if __name__ == "__main__":
    sys.exit(main())
