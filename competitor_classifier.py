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

MAX_TEXT_CHARS = 6000  # 分類・抽出に使う本文の上限文字数

# サスティナビリティ記事ダッシュボード側(api_server.py THEME_META)と同じ9テーマ体系。
# 競合の目標・KPI・実績・ESG評価・取組事例にも同じ体系でタグ付けし、将来の統合をしやすくする
THEMES = [
    "水", "気候変動・GHG", "容器包装", "原料調達", "生物多様性",
    "人権", "健康", "人的資本", "責任あるマーケティング",
]

CLASSIFY_SYSTEM_PROMPT = f"""あなたは競合企業のサステナビリティ情報を監視する専門家です。
与えられた公式サイト本文から、以下6分類のいずれかに該当するレコードをすべて抽出してください。

- TARGET: 将来目標・コミットメント
- KPI: 評価・進捗管理用の指標
- ACTUAL: 年次・四半期等の実績値
- ESG_RATING: ESG評価、スコア、ランク、選定結果
- INITIATIVE: プロジェクト、施策、実証、提携、技術導入等
- OTHER: 上記いずれにも該当しない場合（抽出不要な場合は records を空配列にする）

1つの本文から複数レコードが抽出されることを許容する。TARGET/KPI/ACTUAL/ESG_RATINGについては、
本文に明記された情報のみをstructured_fieldsに埋め、無い項目はnullにする（推測で補完しない）。
INITIATIVEについては title/summary のみを埋める。

themesには、以下9テーマのうち該当するものを1〜3個選んで付与すること（該当が無ければ空配列）。
{', '.join(THEMES)}

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
      "themes": ["水"]
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

CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["records"],
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["record_type", "title", "structured_fields", "summary", "themes"],
                "properties": {
                    "record_type": {
                        "type": "string",
                        "enum": ["TARGET", "KPI", "ACTUAL", "ESG_RATING", "INITIATIVE", "OTHER"],
                    },
                    "title": {"type": "string"},
                    "structured_fields": STRUCTURED_FIELDS_SCHEMA,
                    "summary": {"type": "string"},
                    "themes": {"type": "array", "items": {"type": "string", "enum": THEMES}},
                },
            },
        },
    },
}


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify_and_extract(azure_client, model: str, company_name: str, text: str) -> list:
    """本文からTARGET/KPI/ACTUAL/ESG_RATING/INITIATIVE/OTHERのレコード一覧を抽出する。
    OTHERは呼び出し側で無視してよい（構造化DBへは保存しない）"""
    user_prompt = f"# 対象企業\n{company_name}\n\n# 本文\n{text[:MAX_TEXT_CHARS]}\n"
    result = common.call_llm_structured(
        azure_client, model, CLASSIFY_SYSTEM_PROMPT, user_prompt,
        CLASSIFY_SCHEMA, "CompetitorContentClassification")
    return result["data"]["records"]
