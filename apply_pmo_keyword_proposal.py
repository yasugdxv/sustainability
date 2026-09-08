# -*- coding: utf-8 -*-
"""PMOから提供された「260901_小分類×キーワード案_v0_1.xlsx」を取り込み、
filter_keywords / filter_keyword_tag_map / tag_reference.filter_coverage_policy に反映する。

Excel仕様（PMO作成、シート先頭コメント参照）:
    表記: 無印＝厳密語（単独で通過）、*付き＝一般語（他語との組合せで判定）
    既存語（紐付けのみ）: 2026-08-31版フィルターキーワード一覧に収録済みの語。
        filter_keywordsへの新規insertはせず、既存行を検索してfilter_keyword_tag_mapへの
        紐付けのみ行う
    追加語（日本語/英語）: 新規収録。filter_keywordsへinsertした上で紐付ける
    方針案: required/optional → tag_reference.filter_coverage_policy='required'
            （どちらも本スクリプトで語彙を紐付けるため、以後required運用で問題ない）
            不要 → filter_coverage_policy='exempt'

冪等性: filter_keywordsは(keyword_text, keyword_group, tier)のユニーク制約があるため、
新規追加語は既存チェック後にinsertする。filter_keyword_tag_mapは(keyword_id, tag_id)の
ユニーク制約があるため、同様に既存ペアをスキップする。keyword_groupは1タグ1グループ
（タグIDそのもの）とし、既存391語の広いグループ分けとは独立させる
（同じ語でもタグごとに文脈が異なる場合があるため、タグ単位で管理する）。

使い方:
    python apply_pmo_keyword_proposal.py --dry-run   # 変更内容の確認のみ
    python apply_pmo_keyword_proposal.py             # 実際に反映
"""
import argparse
import re
import sys

import pandas as pd

from article_crawler import load_config, SupabaseClient

EXCEL_PATH = r"C:\Users\269811\Downloads\260901_小分類×キーワード案_v0_1.xlsx"
_EN_PAT = re.compile(r"^[A-Za-z0-9 \-.'&]+$")


def _lang(word: str) -> str:
    return "en" if _EN_PAT.match(word.strip()) else "ja"


def _parse_cell(cell) -> list:
    """「取水規制、water withdrawal*」のようなセルを[(text, tier), ...]に分解する"""
    if not isinstance(cell, str) or not cell.strip():
        return []
    out = []
    for token in cell.split("、"):
        token = token.strip()
        if not token:
            continue
        if token.endswith("*"):
            out.append((token[:-1].strip(), "一般語"))
        else:
            out.append((token, "厳密語"))
    return out


def main():
    parser = argparse.ArgumentParser(description="PMOキーワード案の取り込み")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    df = pd.read_excel(EXCEL_PATH, header=4)
    df = df[df["タグID"].notna()]
    print(f"対象行数: {len(df)}")

    config = load_config()
    client = SupabaseClient(config)

    existing_kw = client.select("filter_keywords", {"select": "keyword_id,keyword_text,keyword_group,tier"})
    existing_by_text = {}
    existing_by_triple = set()
    for r in existing_kw:
        existing_by_text.setdefault(r["keyword_text"].strip(), []).append(r["keyword_id"])
        existing_by_triple.add((r["keyword_text"].strip(), r["keyword_group"], r["tier"]))

    existing_links = set()
    for r in client.select("filter_keyword_tag_map", {"select": "keyword_id,tag_id"}):
        existing_links.add((r["keyword_id"], r["tag_id"]))

    new_keyword_rows, new_link_rows, missing_existing = [], [], []
    policy_updates = {"required": [], "exempt": []}

    for _, row in df.iterrows():
        tag_id = row["タグID"].strip()
        policy = str(row["方針案"]).strip()
        policy_updates["exempt" if policy == "不要" else "required"].append(tag_id)

        # 既存語: 紐付けのみ
        for text, tier in _parse_cell(row.get("既存語（紐付けのみ）")):
            kw_ids = existing_by_text.get(text)
            if not kw_ids:
                missing_existing.append((tag_id, text))
                continue
            for kw_id in kw_ids:
                if (kw_id, tag_id) not in existing_links:
                    new_link_rows.append({"keyword_id": kw_id, "tag_id": tag_id})
                    existing_links.add((kw_id, tag_id))

        # 追加語: 新規insert + 紐付け
        for col, lang_hint in (("追加語（日本語）", "ja"), ("追加語（英語）", "en")):
            for text, tier in _parse_cell(row.get(col)):
                triple = (text, tag_id, tier)
                if triple not in existing_by_triple:
                    new_keyword_rows.append({
                        "keyword_text": text, "keyword_group": tag_id, "tier": tier,
                        "language": _lang(text) or lang_hint,
                        "notes": "260901_小分類×キーワード案_v0_1.xlsx より取り込み",
                    })
                    existing_by_triple.add(triple)
                # keyword_idはinsert後に判明するため、リンクはinsert結果を見てから作る

    print(f"新規キーワード: {len(new_keyword_rows)}件 / 既存語の紐付け: {len(new_link_rows)}件"
          f" / 既存語だが見つからず: {len(missing_existing)}件")
    print(f"filter_coverage_policy → required: {len(policy_updates['required'])}件,"
          f" exempt: {len(policy_updates['exempt'])}件")
    if missing_existing:
        print("見つからなかった既存語（先頭10件）:", missing_existing[:10])

    if args.dry_run:
        return

    # 1) 新規キーワードをinsertし、直後にfilter_keyword_tag_mapへの紐付け行を作る
    if new_keyword_rows:
        for i in range(0, len(new_keyword_rows), 200):
            chunk = new_keyword_rows[i:i + 200]
            inserted = client.insert("filter_keywords", chunk, prefer="return=representation")
            for r in inserted:
                new_link_rows.append({"keyword_id": r["keyword_id"], "tag_id": r["keyword_group"]})

    # 2) タグ紐付け
    for i in range(0, len(new_link_rows), 200):
        client.insert("filter_keyword_tag_map", new_link_rows[i:i + 200], prefer="return=minimal")

    # 3) filter_coverage_policy更新
    for tag_id in policy_updates["required"]:
        client.update("tag_reference", {"tag_id": f"eq.{tag_id}"}, {"filter_coverage_policy": "required"})
    for tag_id in policy_updates["exempt"]:
        client.update("tag_reference", {"tag_id": f"eq.{tag_id}"}, {"filter_coverage_policy": "exempt"})

    print(f"\n完了: キーワード新規{len(new_keyword_rows)}件 / 紐付け{len(new_link_rows)}件 / "
          f"policy更新{len(policy_updates['required']) + len(policy_updates['exempt'])}件")


if __name__ == "__main__":
    sys.exit(main())
