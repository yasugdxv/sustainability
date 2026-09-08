"""
競合サステナビリティモニタリング: コンテンツ分類・構造化モジュール

competitor_crawler.py が取得した1情報源分の本文をLLMで分類し、以下のいずれかに仕分ける。
    TARGET / KPI / ACTUAL / ESG_RATING / INITIATIVE / OTHER
1本文から複数レコードが抽出されることを許容する（例: 目標が複数併記されているページ等）。

TARGET/KPI/ACTUAL/ESG_RATINGは competitor_target_records へ、INITIATIVEは
competitor_initiatives へ保存する（保存自体は competitor_change_detector.py が担当し、
このモジュールはLLM抽出結果のdictを返すところまでを担当する）。

sustainability_expert_common.call_llm_structured() をそのまま再利用する。
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sustainability_expert_common as common  # noqa: E402

# 分類・抽出に使う本文の上限文字数。competitor_sourcesの実ページ（サントリーの
# sustainability/esg/theme*等）で実測したところ最大6,665文字あり、旧値6,000だと
# ページ末尾が切り詰められ、クロール毎に本文が微妙に異なる場合に抽出結果が不安定化する
# 一因になっていた（2026-08-24、一次開示照合導入時に判明）。実測値に対し十分な余裕を
# 持たせる
MAX_TEXT_CHARS = 16000

# サステナビリティ記事ダッシュボード側(api_server.py THEME_META)と同じ9テーマ体系。
# 競合の目標・KPI・実績・ESG評価・取組事例にも同じ体系でタグ付けし、将来の統合をしやすくする
THEMES = [
    "水", "気候変動・GHG", "容器包装", "原料調達", "生物多様性",
    "人権", "健康", "人的資本", "責任あるマーケティング",
]

# サステナ目標カテゴリ（goal_categories、記事側tag_referenceとは独立した別体系）。
# load_goal_categories(client)で読み込むまではNone
_GOAL_CATEGORIES = None
_GOAL_CATEGORY_IDS = None
_GOAL_CATEGORY_PROMPT_TEXT = None


def load_goal_categories(client) -> None:
    """goal_categoriesテーブルを読み込み、分類プロンプト・JSON Schemaで使う形に整形しておく。
    competitor_crawler.main()の先頭で1回呼べばよい（article_filter.load_keywords()と同じ位置づけ）"""
    global _GOAL_CATEGORIES, _GOAL_CATEGORY_IDS, _GOAL_CATEGORY_PROMPT_TEXT
    rows = client.select("goal_categories", {
        "select": "goal_category_id,major_theme,code,name,definition",
        "status": "eq.有効", "order": "display_order",
    })
    _GOAL_CATEGORIES = rows
    _GOAL_CATEGORY_IDS = [r["goal_category_id"] for r in rows]

    by_theme: dict = {}
    for r in rows:
        by_theme.setdefault(r["major_theme"], []).append(r)
    lines = []
    for theme, items in by_theme.items():
        lines.append(f"[{theme}]")
        for it in items:
            lines.append(f"  {it['goal_category_id']} {it['code']}{it['name']}: {it['definition']}")
    _GOAL_CATEGORY_PROMPT_TEXT = "\n".join(lines)


def _build_classify_system_prompt() -> str:
    if _GOAL_CATEGORY_PROMPT_TEXT is None:
        raise RuntimeError("competitor_classifier.load_goal_categories(client) を先に呼んでください")
    return f"""あなたは競合企業のサステナビリティ情報を監視する専門家です。
与えられた公式サイト本文から、以下6分類のいずれかに該当するレコードをすべて抽出してください。

- TARGET: 将来目標・コミットメント
- KPI: 評価・進捗管理用の指標
- ACTUAL: 年次・四半期等の実績値
- ESG_RATING: ESG評価、スコア、ランク、選定結果
- INITIATIVE: プロジェクト、施策、実証、提携、技術導入等
- OTHER: 上記いずれにも該当しない場合（抽出不要な場合は records を空配列にする）

重要: 見出しやセクションタイトルをそのままレコードとして抽出しないこと。
「〜に向けた環境ターゲット設定」「〜の長期ビジョン」「環境目標2030におけるGHG削減目標」のように、
具体的な達成内容（数値・期限・対象範囲等）を伴わず、単に「目標を設定している」「ビジョンを掲げている」
とだけ述べている抽象的な見出し・前置き文はTARGETとして抽出しない（該当箇所にrecordを作らない）。
TARGET/KPIとして抽出してよいのは、具体的な達成内容が本文に明記されている場合のみ（例:
「2030年までにGHG排出量50%削減」「工場の水使用原単位を35%削減」等）。同じ見出しの配下に
複数の具体的な目標が列挙されている場合は、それぞれを個別のレコードとして抽出すること
（見出し1つにつき1レコードにまとめない）。

1つの本文から複数レコードが抽出されることを許容する。TARGET/KPI/ACTUAL/ESG_RATINGについては、
本文に明記された情報のみをstructured_fieldsに埋め、無い項目はnullにする（推測で補完しない）。
INITIATIVEについては title/summary のみを埋める。

重要（抽出の一貫性）: 本文中に具体的な数値・年限・対象範囲が明記されている場合は、
迷わず正確にそのままstructured_fieldsへ書き写すこと。同じ内容を後から抽象化・要約したり、
一部の数値だけを省略したりしないこと（例: 「2030年までに100%」と明記されているのに
target_yearやtarget_valueをnullのままにしない）。本文の記載が変わっていないのに抽出結果が
実行ごとに変わることは、後続の変更検知処理が「変更があった」と誤判定する原因になる。

themesには、以下9テーマのうち該当するものを1〜3個選んで付与すること（該当が無ければ空配列）。
{', '.join(THEMES)}

TARGET/KPI/ACTUAL/ESG_RATING/INITIATIVEについては、themesのうち主目的となる1テーマを選び、
そのテーマ配下のサステナ目標カテゴリ(goal_category_id)を1つだけ選ぶこと（1レコード＝1カテゴリ、
複数選ばない）。該当が薄い場合はそのテーマの「その他」を選ぶ。OTHERの場合やthemesが空の場合は
goal_category_idをnullにする。

evidence_quote（自己検証用、TARGET/KPI/ACTUAL/ESG_RATINGのみ）: structured_fieldsに埋めた
数値・年限・対象範囲等を裏付ける、本文からの引用または近い言い回しを1〜2文で書くこと。
structured_fieldsの各項目は、このevidence_quoteの中に必ず裏付けが見つかる内容だけを埋めること
（裏付けが無い項目はnullのままにする）。INITIATIVE/OTHERは空文字列でよい。

# サステナ目標カテゴリ一覧
{_GOAL_CATEGORY_PROMPT_TEXT}

出力はJSON1個のみ:
{{
  "records": [
    {{
      "record_type": "TARGET",
      "title": "レコードを一言で表す見出し",
      "structured_fields": {{
        "target_value": null, "numeric_value": null, "unit": null, "reduction_rate": null,
        "base_year": null, "target_year": null, "interim_target_year": null,
        "target_company": null, "target_region": null, "target_site": null,
        "target_product": null, "target_material": null, "target_packaging": null,
        "scope": null, "boundary": null, "kpi_definition": null, "achievement_status": null,
        "actual_fiscal_year": null, "actual_value": null, "esg_score": null, "esg_rank": null,
        "selection_status": null
      }},
      "summary": "レコードの要約(1〜2文)",
      "evidence_quote": "本文からの裏付け引用",
      "themes": ["水"],
      "goal_category_id": "GC-01-01"
    }}
  ]
}}
"""


_FIELD_TYPE = ["string", "number", "null"]
STRUCTURED_FIELDS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "target_value": {"type": _FIELD_TYPE}, "numeric_value": {"type": _FIELD_TYPE},
        "unit": {"type": _FIELD_TYPE}, "reduction_rate": {"type": _FIELD_TYPE},
        "base_year": {"type": _FIELD_TYPE}, "target_year": {"type": _FIELD_TYPE},
        "interim_target_year": {"type": _FIELD_TYPE}, "target_company": {"type": _FIELD_TYPE},
        "target_region": {"type": _FIELD_TYPE}, "target_site": {"type": _FIELD_TYPE},
        "target_product": {"type": _FIELD_TYPE}, "target_material": {"type": _FIELD_TYPE},
        "target_packaging": {"type": _FIELD_TYPE}, "scope": {"type": _FIELD_TYPE},
        "boundary": {"type": _FIELD_TYPE}, "kpi_definition": {"type": _FIELD_TYPE},
        "achievement_status": {"type": _FIELD_TYPE}, "actual_fiscal_year": {"type": _FIELD_TYPE},
        "actual_value": {"type": _FIELD_TYPE}, "esg_score": {"type": _FIELD_TYPE},
        "esg_rank": {"type": _FIELD_TYPE}, "selection_status": {"type": _FIELD_TYPE},
    },
}


def _build_classify_schema() -> dict:
    if _GOAL_CATEGORY_IDS is None:
        raise RuntimeError("competitor_classifier.load_goal_categories(client) を先に呼んでください")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["records"],
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["record_type", "title", "structured_fields", "summary",
                                 "evidence_quote", "themes", "goal_category_id"],
                    "properties": {
                        "record_type": {
                            "type": "string",
                            "enum": ["TARGET", "KPI", "ACTUAL", "ESG_RATING", "INITIATIVE", "OTHER"],
                        },
                        "title": {"type": "string"},
                        "structured_fields": STRUCTURED_FIELDS_SCHEMA,
                        "summary": {"type": "string"},
                        "evidence_quote": {"type": "string"},
                        "themes": {"type": "array", "items": {"type": "string", "enum": THEMES}},
                        "goal_category_id": {"type": ["string", "null"], "enum": _GOAL_CATEGORY_IDS + [None]},
                    },
                },
            },
        },
    }


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify_and_extract(azure_client, model: str, company_name: str, text: str) -> list:
    """本文からTARGET/KPI/ACTUAL/ESG_RATING/INITIATIVE/OTHERのレコード一覧を抽出する。
    OTHERは呼び出し側で無視してよい（構造化DBへは保存しない）。
    goal_category_idはその親テーマがthemesに含まれていない場合、防御的にnullへ落とす
    （LLMの選択ミスがtag整合性を壊さないようにする）"""
    user_prompt = f"# 対象企業\n{company_name}\n\n# 本文\n{text[:MAX_TEXT_CHARS]}\n"
    # temperature=0: 同じページを別日にクロールした際、内容が変わっていないのに
    # 抽出結果だけがブレて変更検知側で誤検知を起こすのを避けるため、抽出は極力決定的に行う
    result = common.call_llm_structured(
        azure_client, model, _build_classify_system_prompt(), user_prompt,
        _build_classify_schema(), "CompetitorContentClassification", temperature=0)
    records = result["data"]["records"]

    by_id = {r["goal_category_id"]: r for r in (_GOAL_CATEGORIES or [])}
    for rec in records:
        gc = by_id.get(rec.get("goal_category_id"))
        if gc and gc["major_theme"] not in (rec.get("themes") or []):
            rec["goal_category_id"] = None
    return records
