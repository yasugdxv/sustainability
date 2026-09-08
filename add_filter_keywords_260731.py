# -*- coding: utf-8 -*-
"""filter_keywords への追加投入（2026-07-31 PMO回答「追加フィルターキーワードのご相談への回答」対応）。

対象文書: 260731_追加フィルターキーワード_回答.docx
PMOが追加に同意した3件を、文書内の表の群名のまま新規グループとして登録する。
既に他グループ・他tierで登録済みのキーワード（RE100, rPET, DEI, ジェンダー[一般語]など）は
重複登録を避けるため、ここでは追加しない（filter_keywords は OR 判定のため機能上は問題ないが、
群単位のブックキーピングを汚さないための措置）。

sql/2026-07-30_filter_keywords_table.sql 適用済みであること。
既存の (keyword_text, keyword_group, tier) と重複するキーワードは自動的にスキップされるため、
再実行しても安全。
"""
import re
import sys
from article_crawler import load_config, SupabaseClient

_ASCII_WORD = re.compile(r"^[A-Za-z0-9 \-.'&]+$")


def _lang(kw: str) -> str:
    return "en" if _ASCII_WORD.match(kw) else "ja"


# (keyword_group, tier, [keywords...])
GROUPS = [
    # 1章 表: エネルギー転換（RE100は基準設定機関・国際機関に登録済みのためここでは省略）
    ("エネルギー転換", "厳密語", [
        "EP100", "PPA", "太陽光", "solar", "風力", "wind", "水素", "hydrogen",
        "燃料転換", "省エネ", "energy efficiency", "化石燃料", "fossil fuel",
        "carbon credit", "カーボンクレジット", "内部炭素価格",
    ]),
    # 1章 表: プラスチックの略称・派生（rPETは容器包装に登録済みのためここでは省略）
    ("プラスチックの略称・派生", "厳密語", [
        "廃プラ", "プラごみ", "バイオプラ", "生分解性", "biodegradable",
        "ケミカルリサイクル", "マテリアルリサイクル", "再生材",
    ]),
    # 1章 表: ジェンダー・DEI（DEIは人的資本に登録済みのためここでは省略。
    # 「女性」は単独では一般語のまま据え置き＝ここには追加しない）
    ("ジェンダー・DEI", "厳密語", [
        "女性活躍", "女性管理職", "ジェンダー", "diversity", "inclusion",
        "賃金格差", "pay gap", "多様性",
    ]),
]


def main():
    config = load_config()
    client = SupabaseClient(config)

    existing = client.select("filter_keywords", {"select": "keyword_text,keyword_group,tier"})
    existing_keys = {(r["keyword_text"], r["keyword_group"], r["tier"]) for r in existing}

    rows = []
    for group, tier, keywords in GROUPS:
        for kw in keywords:
            key = (kw, group, tier)
            if key in existing_keys:
                continue
            rows.append({
                "keyword_text": kw,
                "keyword_group": group,
                "tier": tier,
                "language": _lang(kw),
                "notes": "260731_追加フィルターキーワード_回答.docx",
            })

    if not rows:
        print("追加対象なし（すべて登録済み）")
        return

    CHUNK = 200
    for i in range(0, len(rows), CHUNK):
        client.insert("filter_keywords", rows[i:i + CHUNK])
    print(f"{len(rows)}件を登録しました")
    for r in rows:
        print(f"  [{r['keyword_group']}/{r['tier']}] {r['keyword_text']}")

    total = client.select("filter_keywords", {"select": "keyword_id", "status": "eq.有効"})
    print(f"filter_keywords 有効件数: {len(total)}件")


if __name__ == "__main__":
    sys.exit(main())
