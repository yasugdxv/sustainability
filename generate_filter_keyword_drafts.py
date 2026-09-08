# -*- coding: utf-8 -*-
"""filter_keyword未対応タグ(filter_coverage_policy='unreviewed')に対する、
LLMによるフィルタキーワード案の下書き生成（PMOレビュー用）。

背景: tag_reference.tag_meaning/tag_criteriaは自然文の判定基準でありキーワード
リストではないため、機械的な単純抽出では安全なキーワードを作れない。一方、
article_filter.pass_filter()はLLM分析(article_analyzer.py)の前段の粗い関連性
フィルタに過ぎず、多少キーワードが広すぎても実害は「LLM分析に余分な記事が回る
（コスト増）」で済み、「本来拾うべき記事を落とす」リスクの方がずっと重い。
そのため、LLMにタグ定義から下書きキーワード（日英対）を生成させ、**そのまま
本番投入せず**PMOレビュー用のExcelとして出力する（既存の
260731_追加フィルターキーワード_回答.docx等と同じ「PMOレビューを経て確定する」
運用を踏襲）。

本スクリプトはfilter_keywords/filter_keyword_tag_map/tag_reference.filter_coverage_policy
のいずれも書き換えない（レビュー用の下書き出力のみ）。承認後の本番投入は別途、
承認結果を読み込むスクリプトで対応する。

使い方:
    python generate_filter_keyword_drafts.py                # unreviewed全件
    python generate_filter_keyword_drafts.py --limit 5       # 動作確認用に5件だけ
    python generate_filter_keyword_drafts.py --tag-id TH-08-05  # 特定タグのみ
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

from ai_client import make_openai_client
from article_crawler import load_config, SupabaseClient

SYSTEM_PROMPT = """あなたはサステナビリティ関連ニュースの軽量キーワードフィルタ設計を支援します。
このフィルタは「メディア・データ提供機関」カテゴリの記事に限定して適用される、LLMによる
本格的なタグ付け分析の**前段**の粗い関連性チェックです。キーワードが1つでもタイトルまたは
本文冒頭に一致すれば、その記事は「関連あり」としてLLM分析へ回されます。

このフィルタの性質上、キーワードが多少広め・粗めでも実害は軽微です（LLM分析に余分な記事が
回るだけ＝コスト増）。逆に、キーワードが狭すぎたり存在しなかったりすると、本来拾うべき
記事がLLM分析に一切回らず、事後に検知できない捕捉漏れになります。**広めに倒して構いません。**

与えられたタグ定義（タグ名・軸・階層・意味・付与基準）を読み、以下を判断してください。

1. suggested_policy: このタグについて専用の関連性キーワードを用意すべきか。
   - "required": このタグのトピックについて書かれた実際のニュース記事なら、そのトピックを
     特徴づける語彙（固有名詞、専門用語、地名、企業名等を含む）が高い確率でタイトルまたは
     本文に現れると考えられる場合。
   - "exempt": タグ自体が抽象的なメタラベル（記事フォーマット分類、情報の一次/二次区分等）で、
     トピック関連性を判定するキーワードという概念に馴染まない場合。
   迷ったら"required"側に倒してください（対応不要と誤判定するより、広めのキーワードを
   提案する方が安全なため）。

2. suggested_policyが"required"の場合:
   - specific_keywords: そのタグに強く特徴的な語彙を2〜6個、日本語(ja)と英語(en)のペアで
     （英語記事にも対応するため両方必須。固有名詞で日英同一表記の場合はja/en同じ値でよい）
   - generic_keywords: より一般的な語彙があれば0〜3個（日本語文字列のリスト。英訳不要）

3. reasoning: 判断理由を1文で。

厳密にJSON形式のみで回答してください。説明文は不要です。
{
  "suggested_policy": "required" または "exempt",
  "specific_keywords": [{"ja": "...", "en": "..."}, ...],
  "generic_keywords": ["...", ...],
  "reasoning": "..."
}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    return json.loads(text)


def build_user_prompt(tag: dict) -> str:
    lines = [
        f"タグID: {tag['tag_id']}",
        f"軸: {tag['tag_axis']}",
        f"階層: {tag['tag_level']}",
        f"タグ名: {tag['tag_name']}",
    ]
    if tag.get("parent_tag_name"):
        lines.append(f"親タグ: {tag['parent_tag_name']}")
    if tag.get("tag_meaning"):
        lines.append(f"意味: {tag['tag_meaning']}")
    if tag.get("tag_criteria"):
        lines.append(f"付与基準: {tag['tag_criteria']}")
    return "\n".join(lines)


def generate_draft(azure_client, model: str, tag: dict) -> dict:
    resp = azure_client.responses.create(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=build_user_prompt(tag),
    )
    data = _extract_json(resp.output_text)
    return {
        "tag_id": tag["tag_id"],
        "tag_axis": tag["tag_axis"],
        "tag_level": tag["tag_level"],
        "tag_name": tag["tag_name"],
        "tag_meaning": tag.get("tag_meaning", ""),
        "tag_criteria": tag.get("tag_criteria", ""),
        "suggested_policy": data.get("suggested_policy", ""),
        "specific_keywords_ja": "、".join(k.get("ja", "") for k in data.get("specific_keywords", [])),
        "specific_keywords_en": ", ".join(k.get("en", "") for k in data.get("specific_keywords", [])),
        "generic_keywords": "、".join(data.get("generic_keywords", [])),
        "reasoning": data.get("reasoning", ""),
    }


def _process_one(azure_client, model, tag):
    try:
        return generate_draft(azure_client, model, tag)
    except Exception as e:
        return {
            "tag_id": tag["tag_id"], "tag_axis": tag["tag_axis"], "tag_level": tag["tag_level"],
            "tag_name": tag["tag_name"], "tag_meaning": tag.get("tag_meaning", ""),
            "tag_criteria": tag.get("tag_criteria", ""),
            "suggested_policy": "エラー", "specific_keywords_ja": "", "specific_keywords_en": "",
            "generic_keywords": "", "reasoning": f"{type(e).__name__}: {e}",
        }


def main():
    parser = argparse.ArgumentParser(description="フィルタキーワード下書き生成（PMOレビュー用）")
    parser.add_argument("--limit", type=int, default=None, help="動作確認用に対象タグ数を制限する")
    parser.add_argument("--tag-id", type=str, default=None, help="特定タグIDのみ対象にする")
    parser.add_argument("--max-workers", type=int, default=5)
    args = parser.parse_args()

    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if azure_client is None:
        print("[ERROR] LLMクライアントを初期化できません（config.jsonのazure_openai設定を確認してください）")
        sys.exit(1)

    all_tags = client.select("tag_reference", {
        "select": "tag_id,tag_axis,tag_level,tag_name,tag_meaning,tag_criteria,parent_tag_id,filter_coverage_policy",
        "status": "eq.有効",
    })
    tag_name_by_id = {t["tag_id"]: t["tag_name"] for t in all_tags}
    for t in all_tags:
        t["parent_tag_name"] = tag_name_by_id.get(t.get("parent_tag_id"))

    if args.tag_id:
        targets = [t for t in all_tags if t["tag_id"] == args.tag_id]
    else:
        targets = [t for t in all_tags if t["filter_coverage_policy"] == "unreviewed"]
    if args.limit:
        targets = targets[:args.limit]

    print(f"対象タグ数: {len(targets)}件")
    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {executor.submit(_process_one, azure_client, model, t): t for t in targets}
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if i % 20 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)} 件完了")

    df = pd.DataFrame(results)
    order = {t["tag_id"]: i for i, t in enumerate(targets)}
    df["_order"] = df["tag_id"].map(order)
    df = df.sort_values("_order").drop(columns=["_order"])

    df = df.rename(columns={
        "tag_id": "タグID", "tag_axis": "軸", "tag_level": "階層", "tag_name": "タグ名",
        "tag_meaning": "意味", "tag_criteria": "付与基準",
        "suggested_policy": "LLM提案ポリシー", "specific_keywords_ja": "提案キーワード(厳密語・日本語)",
        "specific_keywords_en": "提案キーワード(厳密語・英語)", "generic_keywords": "提案キーワード(一般語)",
        "reasoning": "判断理由",
    })
    df.insert(len(df.columns), "PMO判定(required/exempt/保留)", "")
    df.insert(len(df.columns), "PMOコメント", "")

    ts = datetime.now().strftime("%Y%m%d")
    out_path = f"docs/{ts}_filter_keyword_drafts.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="キーワード案レビュー", index=False)
        sheet = writer.sheets["キーワード案レビュー"]
        widths = [10, 8, 8, 20, 36, 40, 12, 30, 30, 20, 40, 18, 30]
        for i, w in enumerate(widths):
            sheet.column_dimensions[chr(ord("A") + i)].width = w
        sheet.freeze_panes = "A2"

    print(f"\n提案件数: {len(df)}")
    print(df["LLM提案ポリシー"].value_counts())
    print(f"出力先: {out_path}")


if __name__ == "__main__":
    main()
