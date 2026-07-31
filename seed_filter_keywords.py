# -*- coding: utf-8 -*-
"""filter_keywords テーブルへの初期データ投入（一度限りの移行スクリプト）。

これまで article_filter.py 内にPython定数として直書きしていたキーワードを、
sql/2026-07-30_filter_keywords_table.sql で作成した filter_keywords テーブルへ
移す。upsert（keyword_text, keyword_group, tier の組でユニーク）なので再実行しても安全。

使い方: sql/2026-07-30_filter_keywords_table.sql を実行済みであることを確認した上で
    python seed_filter_keywords.py
"""
import re
import sys
from article_crawler import load_config, SupabaseClient

_ASCII_WORD = re.compile(r"^[A-Za-z0-9 \-.'&]+$")


def _lang(kw: str) -> str:
    return "en" if _ASCII_WORD.match(kw) else "ja"


# (keyword_group, tier, [keywords...])
GROUPS = [
    ("水", "厳密語", [
        "水リスク", "水ストレス", "取水規制", "ウォータースチュワードシップ", "CDP Water",
        "water stewardship", "water stress", "water scarcity", "water withdrawal", "watershed",
        "涵養", "replenishment", "地下水", "groundwater", "流域", "basin",
    ]),
    ("水", "一般語", [
        "水", "water", "節水", "洪水", "干ばつ", "drought", "flood", "floods", "flooding",
    ]),
    ("気候変動・GHG", "厳密語", [
        "気候変動", "温室効果ガス", "カーボンニュートラル", "ネットゼロ", "SBTi", "Science Based Targets",
        "Scope 1", "Scope 2", "Scope 3", "TCFD", "CBAM", "ETS", "carbon pricing", "net zero",
        "net-zero", "decarbonization", "GHG Protocol", "climate transition plan",
    ]),
    ("気候変動・GHG", "一般語", [
        "気候", "climate", "排出", "emissions", "carbon", "脱炭素",
        "山火事", "森林火災", "wildfire", "wildfires", "台風", "typhoon", "hurricane", "熱波", "heatwave",
    ]),
    ("容器包装", "厳密語", [
        "容器包装", "プラスチック規制", "サーキュラーエコノミー", "circular economy", "EPR", "PPWR", "DRS",
        "deposit return scheme", "rPET", "extended producer responsibility", "plastic pact",
    ]),
    ("容器包装", "一般語", [
        "プラスチック", "plastic", "リサイクル", "recycling", "包装", "packaging",
    ]),
    ("原料調達", "厳密語", [
        "持続可能な調達", "サステナブル調達", "regenerative agriculture", "EUDR", "deforestation-free",
        "RSPO", "Rainforest Alliance", "Fairtrade", "traceability",
    ]),
    ("原料調達", "一般語", [
        "原料", "調達", "sourcing", "supply chain", "農業", "agriculture",
    ]),
    ("生物多様性", "厳密語", [
        "TNFD", "SBTN", "nature positive", "ネイチャーポジティブ", "自然資本", "natural capital",
        "生態系保全", "ecosystem restoration",
    ]),
    ("生物多様性", "一般語", [
        "生物多様性", "biodiversity", "自然", "nature", "森林", "forest",
    ]),
    ("人権", "厳密語", [
        "人権デューデリジェンス", "human rights due diligence", "CSDDD", "強制労働", "forced labor",
        "modern slavery", "児童労働", "child labor", "生活賃金", "living wage",
    ]),
    ("人権", "一般語", [
        "人権", "human rights", "労働", "labor", "labour",
    ]),
    ("健康", "厳密語", [
        "砂糖税", "sugar tax", "HFSS", "ultra-processed food", "UPF", "健康表示", "nutrition labeling",
        "alcohol regulation", "責任ある飲酒", "responsible drinking", "non-alcoholic", "ノンアルコール",
    ]),
    ("健康", "一般語", [
        "健康", "health", "アルコール", "alcohol",
    ]),
    ("人的資本", "厳密語", [
        "人的資本開示", "human capital disclosure", "ISO 30414", "DEI", "pay equity", "賃金透明性",
        "wage transparency", "employee wellbeing",
    ]),
    ("人的資本", "一般語", [
        "人的資本", "human capital", "従業員", "employee", "workforce",
        "レイオフ", "layoff", "layoffs", "人員削減", "リストラ",
    ]),
    ("責任あるマーケティング", "厳密語", [
        "グリーンウォッシュ", "greenwashing", "green claims", "環境表示規制", "misleading advertising",
        "advertising standards",
    ]),
    ("責任あるマーケティング", "一般語", [
        "マーケティング", "advertising", "広告",
    ]),
    ("地政学・貿易・供給網リスク", "厳密語", [
        "国有化", "nationalization", "nationalisation",
        "貿易協定", "trade agreement", "trade deal",
        "経済安全保障", "economic security",
        "輸出規制", "export control", "export restriction",
        "デカップリング", "decoupling", "デリスキング", "de-risking",
        "地政学リスク", "geopolitical risk",
        "エネルギー安全保障", "energy security",
        "サプライチェーン混乱", "supply chain disruption",
        "経済制裁", "economic sanctions",
    ]),
    ("地政学・貿易・供給網リスク", "一般語", [
        "関税", "tariff", "tariffs",
        "制裁", "sanctions",
        "戦争", "war", "紛争", "conflict",
        "侵攻", "invasion",
        "空爆", "airstrike", "air strike", "爆撃", "bombing", "bomb",
        "attack", "attacks", "strike", "strikes",
    ]),
    ("情報開示", "厳密語", [
        "CSRD", "ESRS", "SSBJ", "ISSB", "IFRS S1", "IFRS S2", "sustainability disclosure",
        "assurance", "保証", "omnibus",
    ]),
    ("ESG評価・サステナブルファイナンス", "厳密語", [
        "ESG rating", "タクソノミー", "SFDR", "sustainable finance", "green bond", "グリーンボンド",
    ]),
    ("閾値語（横断）", "厳密語", [
        "訴訟", "lawsuit", "litigation", "リコール", "product recall",
        "罰金", "fined", "施行", "enforcement", "法案",
        "merger", "acquisition", "統合",
    ]),
    ("閾値語（横断）", "一般語", [
        "recall", "fine", "penalty", "regulation", "directive", "bill",
    ]),
    ("競合企業", "厳密語", [
        "AB InBev", "ABインベブ", "Heineken", "ハイネケン", "Carlsberg", "カールスバーグ",
        "Molson Coors", "モルソン・クアーズ", "Sapporo Holdings", "サッポロ", "Diageo", "ディアジオ",
        "Pernod Ricard", "ペルノ・リカール", "Brown-Forman", "ブラウン・フォーマン", "Bacardi", "バカルディ",
        "The Coca-Cola Company", "コカ・コーラ", "PepsiCo", "ペプシコ",
        "Keurig Dr Pepper", "キューリグ・ドクター・ペッパー", "Asahi Group Holdings", "アサヒ",
        "Kirin Holdings", "キリン", "ITO EN", "伊藤園", "Coca-Cola Europacific Partners", "CCEP",
        "Nestlé", "ネスレ", "Danone", "ダノン", "Unilever", "ユニリーバ", "Mondelēz", "モンデリーズ",
    ]),
    ("基準設定機関・国際機関", "厳密語", [
        "SBTi", "CDP", "GRI", "ISSB", "TCFD", "TNFD", "SBTN", "RE100", "ISO 30414", "GHG Protocol",
        "SSBJ", "WBCSD",
        "UNEP", "WHO", "世界保健機関", "ILO", "国際労働機関", "IMO", "国際海事機関", "FAO", "食糧農業機関",
        "WMO", "世界気象機関", "OECD", "経済協力開発機構", "IPCC", "UNFCCC", "World Bank", "世界銀行",
        "IEA", "国際エネルギー機関", "WEF", "世界経済フォーラム", "UNDRR", "国連防災機関",
    ]),
    ("自社", "厳密語", [
        "Suntory", "サントリー", "Suntory Global Spirits", "サントリービバレッジ＆フード",
        "Suntory Beverage & Food", "サントリービバレッジウエルネス",
    ]),
    ("業界主要企業", "厳密語", [
        "Mars", "マース", "General Mills", "ゼネラル・ミルズ", "味の素", "Ajinomoto",
        "Tesco", "テスコ", "Cargill", "カーギル", "花王", "Kao",
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
            })

    if not rows:
        print("追加対象なし（すべて登録済み）")
        return

    CHUNK = 200
    for i in range(0, len(rows), CHUNK):
        client.insert("filter_keywords", rows[i:i + CHUNK])
    print(f"{len(rows)}件を登録しました")

    total = client.select("filter_keywords", {"select": "keyword_id", "status": "eq.有効"})
    print(f"filter_keywords 有効件数: {len(total)}件")


if __name__ == "__main__":
    sys.exit(main())
