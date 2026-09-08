# -*- coding: utf-8 -*-
"""下位軸5タグ（TH-10-01〜05）だけを対象にした軽量タグ付与パス。

新設したばかりの下位軸タグは、既存の分析済み記事のarticle_tagsに一件も付いていない
（article_analyzer.pyのフル分析はタグ追加前に実行されたため）。フル再分析（全464タグ・
7項目ルーブリック採点）をやり直すと重要度ランク等の既存結果まで変わってしまうため、
ここでは下位軸5タグの該当有無だけを判定する軽量なLLM呼び出しに絞り、article_analysis
（ランク・スコア）には一切触れず、article_tagsへの追加のみを行う。

再実行しても安全（このスクリプトが付与した下位軸タグのみを都度削除してから入れ直す）。
"""
import json
import re
import sys
import time
from article_crawler import load_config, SupabaseClient
from ai_client import make_openai_client

# Windowsコンソール(cp932)では一部記事タイトルの文字が出力できずクラッシュするため、
# 標準出力をUTF-8に切り替える（article_analyzer.pyと同じ対策）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_PATH = "sub_axis_tagging_progress.log"
SUB_AXIS_PARENT_TAG_ID = "TH-10"
# article_tags.assigned_by にはCHECK制約があり独自ラベルは使えないため、
# article_analyzer.pyと同じ"AI"を使う（削除範囲を下位軸5タグのみに絞ることで、
# 既存のフル分析タグを巻き込まないようにする）
ASSIGNED_BY = "AI"

SYSTEM_PROMPT = """あなたはサントリーグループのサステナビリティ情報分析AIです。
与えられた記事1件が、以下5つの「下位軸」タグのいずれかに該当するかを判定してください。
これらは主要9テーマには含まれない補助的なテーマ区分で、通常のニュース記事の大半には
該当しません。該当なしの場合が最も多いことを前提に、慎重に判定してください。

{tag_defs}

出力は次のキーを持つJSON1個のみ（説明文・Markdown装飾は不要。値は必ず正しいJSON文字列として
ダブルクォートで囲むこと）：
- tags: 該当するtag_idの配列（例: ["TH-10-04"]）。複数該当可、該当なしなら空配列
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


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)

    sub_axis_tags = client.select("tag_reference", {
        "select": "tag_id,tag_name,tag_meaning,tag_criteria",
        "parent_tag_id": f"eq.{SUB_AXIS_PARENT_TAG_ID}",
        "status": "eq.有効",
        "order": "display_order",
    })
    valid_tag_ids = {t["tag_id"] for t in sub_axis_tags}
    tag_defs = "\n".join(
        f"[{t['tag_id']}] {t['tag_name']}: {t['tag_meaning']}（{t['tag_criteria']}）"
        for t in sub_axis_tags
    )
    system_prompt = SYSTEM_PROMPT.format(tag_defs=tag_defs)

    analysis = client.select("article_analysis", {
        "select": "article_id", "is_current": "eq.true",
    })
    article_ids = [a["article_id"] for a in analysis]
    articles = {a["article_id"]: a for a in client.select(
        "articles", {"select": "article_id,title,extracted_text", "is_current": "eq.true"})}

    total = len(article_ids)
    log(f"対象記事: {total}件（下位軸5タグのみを判定）")

    ok, failed, matched = 0, 0, 0
    for i, article_id in enumerate(article_ids, start=1):
        article = articles.get(article_id)
        if not article:
            continue
        title = article.get("title") or ""
        body = (article.get("extracted_text") or "")[:4000]
        try:
            resp = azure_client.responses.create(
                model=model,
                instructions=system_prompt,
                input=f"タイトル: {title}\n\n本文:\n{body}",
            )
            data = _extract_json(resp.output_text)
            raw_tags = list(dict.fromkeys(data.get("tags") or []))
            tag_ids = [t for t in raw_tags if t in valid_tag_ids]

            # 下位軸5タグの範囲だけ入れ替える（他のフル分析タグは触らない）
            client.delete("article_tags", {
                "article_id": f"eq.{article_id}",
                "assigned_by": f"eq.{ASSIGNED_BY}",
                "tag_id": f"in.({','.join(valid_tag_ids)})",
            })
            if tag_ids:
                client.insert("article_tags", [{
                    "article_id": article_id,
                    "tag_id": tid,
                    "assigned_by": ASSIGNED_BY,
                    "assignment_reason": f"{model}による下位軸専用判定",
                } for tid in tag_ids], prefer="return=minimal")
                matched += 1
            ok += 1
            log(f"  [{i}/{total}] OK tags={tag_ids} {title[:40]}")
        except Exception as e:
            failed += 1
            log(f"  [{i}/{total}] ERROR {type(e).__name__}: {e} ({article_id})")

    log(f"完了: 成功={ok} 失敗={failed} 該当あり={matched}/{ok}")


if __name__ == "__main__":
    sys.exit(main())
