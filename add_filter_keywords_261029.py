# -*- coding: utf-8 -*-
"""フィルタ語彙の日本語⇔英語の対応の抜けを埋める（2026-10-29、多言語化の段階1）。

背景: 10/14のSustainable Japan記事「ESG評価機関ランキング2026」が、語彙に日本語の「ESG評価」が
なく、フィルタ除外された。英語側にだけある語（ESG rating等）の日本語訳、日本語側にだけある語の
英語訳を追加する。フランス語・スペイン語等(段階2)は、language列の制約(ja/en)の変更が要るため別途。

注意: 「閾値語（横断）」グループは、sustainability_article_selectorが下位軸の判定に使うため、触らない。
既定は確認用（一覧の表示とCSV出力のみ）。--apply を付けたときだけDBへ登録する（再実行しても安全）。

使い方:
    python add_filter_keywords_261029.py            # 確認のみ
    python add_filter_keywords_261029.py --apply    # 登録
"""
import csv
import re
import sys
from pathlib import Path

from article_crawler import load_config, SupabaseClient

_ASCII_WORD = re.compile(r"^[A-Za-z0-9 \-.'&]+$")
NOTES = "261029_日英の対応の抜けを補完(多言語化段階1)"


def _lang(kw: str) -> str:
    return "en" if _ASCII_WORD.match(kw) else "ja"


# (グループ, 区分, [語...])。一般語はタイトルのみ照合、厳密語はタイトルと本文の先頭1000字を照合する
ADDITIONS = [
    ("ESG評価・サステナブルファイナンス", "厳密語", [
        "ESG評価", "ESG格付", "ESG評価機関", "ESG投資", "ESGファンド", "サステナブルファイナンス",
        "グリーンファイナンス", "トランジション・ファイナンス", "サステナビリティ・リンク・ローン", "EUタクソノミー",
        "ESG rating agency", "ESG investing", "ESG fund", "green finance", "transition finance",
        "sustainability-linked loan", "EU taxonomy",
    ]),
    ("基準設定機関・国際機関", "厳密語", [
        "国際サステナビリティ基準審議会", "気候変動に関する政府間パネル", "国連環境計画", "国連気候変動枠組条約",
        "気候関連財務情報開示タスクフォース", "自然関連財務情報開示タスクフォース", "サステナビリティ基準委員会",
        "GHGプロトコル", "持続可能な開発のための世界経済人会議",
    ]),
    ("情報開示", "厳密語", [
        "サステナビリティ開示", "サステナビリティ情報開示", "企業サステナビリティ報告指令",
        "欧州サステナビリティ報告基準", "国際サステナビリティ基準",
    ]),
    ("TH-06-11", "厳密語", [
        "企業人権ベンチマーク", "世界ベンチマーキング同盟", "人権ベンチマーク",
    ]),
    ("プラスチックの略称・派生", "厳密語", [
        "chemical recycling", "mechanical recycling", "plastic waste", "plastic pollution", "recycled plastic",
        "recycled material",
    ]),
    ("ジェンダー・DEI", "厳密語", [
        "women in leadership", "female managers", "gender diversity", "gender gap", "women's empowerment",
    ]),
    ("原料調達", "厳密語", [
        "森林破壊フリー", "森林破壊ゼロ", "持続可能なパーム油",
    ]),
    ("健康", "厳密語", [
        "高脂肪・高糖分・高塩分", "NOVA classification",
    ]),
    ("責任あるマーケティング", "厳密語", [
        "誤認広告", "広告基準", "グリーンクレーム",
    ]),
    ("人権", "厳密語", [
        "人権侵害", "現代奴隷法",
    ]),
    ("人的資本", "厳密語", [
        "従業員ウェルビーイング", "ペイエクイティ",
    ]),
    ("地政学・貿易・供給網リスク", "厳密語", [
        "輸出制限", "貿易合意", "地政学的リスク",
    ]),
    ("地政学・貿易・供給網リスク", "一般語", [
        "攻撃",
    ]),
    ("TH-02-12", "厳密語", [
        "炭素回収", "持続可能な航空燃料", "合成燃料",
    ]),
    ("TH-02-05", "厳密語", [
        "コーポレートPPA",
    ]),
    ("TH-05-05", "厳密語", [
        "重要生物多様性地域",
    ]),
    ("TH-05-09", "厳密語", [
        "デジタル配列情報", "利益配分",
    ]),
    ("TH-05-10", "厳密語", [
        "生物多様性枠組", "昆明・モントリオール生物多様性枠組",
    ]),
    ("TH-09-09", "厳密語", [
        "Foods for Specified Health Uses", "Foods with Function Claims", "FOSHU",
    ]),
    ("TH-09-12", "厳密語", [
        "Consumer Affairs Agency",
    ]),
    ("TH-10-04", "厳密語", [
        "information leakage",
    ]),
]


def main():
    apply = "--apply" in sys.argv
    client = SupabaseClient(load_config())
    existing = client.select("filter_keywords", {"select": "keyword_text,keyword_group,tier"})
    existing_keys = {(r["keyword_text"], r["keyword_group"], r["tier"]) for r in existing}
    # 同じ語が、別グループ・別区分にすでにあるものは重複扱いにしない（unique制約は3列の組のため）が、
    # 同一グループ・同一区分の重複は除く
    rows = []
    for group, tier, keywords in ADDITIONS:
        assert group != "閾値語（横断）"
        for kw in keywords:
            if (kw, group, tier) in existing_keys:
                continue
            rows.append({"keyword_text": kw, "keyword_group": group, "tier": tier,
                         "language": _lang(kw), "notes": NOTES})
    print(f"追加候補: {len(rows)}件（日本語 {sum(r['language']=='ja' for r in rows)} / 英語 {sum(r['language']=='en' for r in rows)}）")
    for r in rows:
        print(f"  [{r['keyword_group']}/{r['tier']}/{r['language']}] {r['keyword_text']}")
    out = Path.home() / "Downloads" / "フィルタ語彙_追加案_2026-10-29.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["keyword_group", "tier", "language", "keyword_text", "notes"])
        w.writeheader()
        w.writerows({k: r[k] for k in w.fieldnames} for r in rows)
    print(f"一覧を出力: {out}")
    if not apply:
        print("（確認のみ。登録するには --apply を付けて再実行）")
        return
    if rows:
        client.insert("filter_keywords", rows)
    print(f"{len(rows)}件を登録しました")


if __name__ == "__main__":
    sys.exit(main())
