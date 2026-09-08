# -*- coding: utf-8 -*-
"""「閾値語（横断）」グループに、下位軸3条件のうち(i)「規制の…重要改定」(ii)「重大訴訟・不祥事」
に対応する語彙を追加する（2026-08-03 PMOフィードバック2章：3条件の文言漏れの指摘対応）。

sustainability_article_selector.py の Tier3（下位軸ワイルドカード）が、この
filter_keywords「閾値語（横断）」グループへの本文一致を3条件の代理判定に使っているため、
「改定」「不祥事」系の語彙が薄いと、そのパターンの記事を拾い損ねる。

seed_filter_keywords.py と同じ冪等パターン（再実行しても安全）。
"""
import re
import sys
from article_crawler import load_config, SupabaseClient

_ASCII_WORD = re.compile(r"^[A-Za-z0-9 \-.'&]+$")


def _lang(kw: str) -> str:
    return "en" if _ASCII_WORD.match(kw) else "ja"


GROUPS = [
    ("閾値語（横断）", "厳密語", [
        "改定", "改正", "amendment", "amended", "revised", "revision",
        "不祥事", "scandal", "misconduct", "fraud", "偽装", "隠蔽",
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
                "keyword_text": kw, "keyword_group": group, "tier": tier,
                "language": _lang(kw),
                "notes": "260803_重要度判定・記事掲載ルール_フィードバック_v0.1.docx",
            })

    if not rows:
        print("追加対象なし（すべて登録済み）")
        return

    client.insert("filter_keywords", rows)
    print(f"{len(rows)}件を登録しました")
    for r in rows:
        print(f"  [{r['keyword_group']}/{r['tier']}] {r['keyword_text']}")


if __name__ == "__main__":
    sys.exit(main())
