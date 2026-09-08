# -*- coding: utf-8 -*-
"""Weekly Strategic Question機能の前提（Phase 0）: サステナビリティ戦略KBの拡充。

knowledge/sustainability_expert/source_registry.csv に登録済みの公式ソースURL
（サントリー公式サステナビリティサイト、26件）のうち、sustainability_strategy_facts
テーブルに未取込のものを取得し、構造化ファクトとして抽出する。

3層構造:
    Source (source_registry.csv)
      → Structured Strategy Fact (sustainability_strategy_facts テーブル)
      → Theme Context Summary (knowledge_documents.jsonl)

既存のsuntory_sustainability_expert_base.json（headline_targets、水・気候・容器の
3テーマ・計7件のみ）は薄すぎるため、公開情報の範囲でこれを補う。ただし自社の
公式戦略として社内外に見える内容のため、記事フィルタ語彙よりも誤り放置のリスクが
高く、fetchの抽出結果を直接DBへは書き込まない（PMOレビュー用Excelを経由する）。

使い方:
    python expand_sustainability_knowledge.py fetch
        # 未取込URLを取得し、抽出案をExcelに出力する（DB書き込みなし）
    python expand_sustainability_knowledge.py apply --input docs/YYYY-MM-DD_戦略ファクト抽出案.xlsx
        # 「採用」列にマークされた行だけをsustainability_strategy_factsへupsert
    python expand_sustainability_knowledge.py summarize
        # テーマごとにfactsを集約し、knowledge_documents.jsonlのtheme_context文書を
        # テーマ固定のstable IDで1テーマ1件に更新する（追記しない）
    python expand_sustainability_knowledge.py coverage
        # テーマ別カバレッジ（sufficient/partial/insufficient）を表示する
"""
import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

import sustainability_expert_common as common
from ai_client import make_openai_client
from article_crawler import SupabaseClient, extract_article
from config_utils import load_config

BASE = Path(__file__).parent
SOURCE_REGISTRY_PATH = common.KNOWLEDGE_DIR / "source_registry.csv"
KNOWLEDGE_DOCUMENTS_PATH = common.KNOWLEDGE_DIR / "knowledge_documents.jsonl"

THEMES = ["水", "気候変動・GHG", "容器包装", "原料調達", "生物多様性",
          "人権", "健康", "人的資本", "責任あるマーケティング"]
THEME_JA_TO_EN = {
    "水": "water", "気候変動・GHG": "climate", "容器包装": "packaging",
    "原料調達": "raw_materials", "生物多様性": "biodiversity", "人権": "human_rights",
    "健康": "health", "人的資本": "human_capital", "責任あるマーケティング": "responsible_marketing",
}
STRATEGY_ELEMENT_TYPES = ("numeric_target", "direction", "policy_commitment", "major_action")

FACT_COLUMNS = ["theme", "strategy_element_type", "strategic_direction", "target_metric",
                "target_value", "baseline", "target_year", "scope", "geography",
                "major_actions", "policy_or_commitment", "source_url", "source_title",
                "effective_date"]

PROXY_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
}


# ─── Fetch: ページ取得 → LLMでFact抽出 ──────────────────────────────
FETCH_SYSTEM_PROMPT = """あなたは当社サステナビリティ専門家です。
サントリーの公式サステナビリティサイトのページ本文から、当社の戦略・目標・施策に
関する構造化ファクトを抽出してください。

守るべきこと:
- ページ本文に明記されている内容のみを抽出する。数値・年度・地域等、本文に無い情報を
  補完・推測しない
- 1ページから複数のFactが出てよい（例: 複数テーマ、複数の目標が1ページに載っている場合）
- 該当する情報が無ければ空配列を返してよい（無理に抽出しない）
- テーマ(theme)は次の9つから最も適切なものを選ぶ: {themes}
- strategy_element_typeは次の4種類から選ぶ:
  numeric_target(数値目標。target_metric/target_value/target_year等を伴う),
  direction(定性的な戦略方向性), policy_commitment(方針・コミットメント表明),
  major_action(個別の具体的施策)

出力はJSON1個のみ:
{{"facts": [
  {{"theme":"", "strategy_element_type":"", "strategic_direction":"", "target_metric":"",
    "target_value":"", "baseline":"", "target_year":null, "scope":"", "geography":"",
    "major_actions":[], "policy_or_commitment":"", "effective_date":""}}
]}}
"""

FACT_ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["theme", "strategy_element_type", "strategic_direction", "target_metric",
                 "target_value", "baseline", "target_year", "scope", "geography",
                 "major_actions", "policy_or_commitment", "effective_date"],
    "properties": {
        "theme": {"type": "string", "enum": THEMES},
        "strategy_element_type": {"type": "string", "enum": list(STRATEGY_ELEMENT_TYPES)},
        "strategic_direction": {"type": "string"},
        "target_metric": {"type": "string"},
        "target_value": {"type": "string"},
        "baseline": {"type": "string"},
        "target_year": {"type": ["integer", "null"]},
        "scope": {"type": "string"},
        "geography": {"type": "string"},
        "major_actions": {"type": "array", "items": {"type": "string"}},
        "policy_or_commitment": {"type": "string"},
        "effective_date": {"type": "string"},
    },
}
FETCH_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["facts"],
    "properties": {"facts": {"type": "array", "items": FACT_ITEM_SCHEMA}},
}


def _read_source_registry() -> list:
    with open(SOURCE_REGISTRY_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cmd_fetch(args):
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if azure_client is None:
        print("[ERROR] LLMクライアントを初期化できません")
        sys.exit(1)
    proxy_cfg = config.get("proxy", {})
    proxies = {"http": proxy_cfg.get("http_proxy"), "https": proxy_cfg.get("http_proxy")} \
        if proxy_cfg.get("enabled") else None
    verify = config.get("ssl", {}).get("verify", True)

    existing_urls = {r["source_url"] for r in
                      client.select("sustainability_strategy_facts", {"select": "source_url"})}
    registry = _read_source_registry()
    targets = [r for r in registry if r["url"] not in existing_urls]
    print(f"source_registry.csv: {len(registry)}件 / 未取込: {len(targets)}件")

    rows = []
    for r in targets:
        result = extract_article(r["url"], proxies, verify)
        if not result.get("ok"):
            print(f"  [取得失敗] {r['title']}: {result.get('error')}")
            continue
        text = (result.get("text") or "")[:6000]
        if not text.strip():
            print(f"  [本文空] {r['title']}")
            continue
        user_prompt = f"タイトル: {r['title']}\nURL: {r['url']}\n\n本文:\n{text}"
        try:
            llm_result = common.call_llm_structured(
                azure_client, model,
                FETCH_SYSTEM_PROMPT.format(themes="、".join(THEMES)),
                user_prompt, FETCH_SCHEMA, "strategy_fact_extraction")
        except Exception as e:
            print(f"  [LLM失敗] {r['title']}: {type(e).__name__}: {e}")
            continue
        facts = llm_result["data"]["facts"]
        for fact in facts:
            rows.append({
                **fact,
                "major_actions": "、".join(fact.get("major_actions") or []),
                "source_url": r["url"],
                "source_title": r["title"],
                "採用": "",
            })
        print(f"  [OK] {r['title']}: {len(facts)}件抽出")

    if not rows:
        print("抽出対象なし（全URL取込済み、または本文取得に失敗）")
        return

    ts = datetime.now().strftime("%Y-%m-%d")
    out_path = f"docs/{ts}_戦略ファクト抽出案.xlsx"
    df = pd.DataFrame(rows)[FACT_COLUMNS + ["採用"]]
    df.to_excel(out_path, index=False)
    print(f"\n出力: {out_path}（{len(rows)}件）")
    print("「採用」列に何か記入した行だけを apply コマンドで反映します。")


# ─── Apply: レビュー済みExcel → DB反映（冪等） ───────────────────────
def _fact_fingerprint(fact: dict) -> str:
    normalized = "|".join(
        str(fact.get(k) or "").strip().lower()
        for k in ("theme", "strategy_element_type", "strategic_direction",
                  "target_metric", "target_value", "target_year", "scope", "source_url")
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def cmd_apply(args):
    config = load_config()
    client = SupabaseClient(config)
    df = pd.read_excel(args.input)
    adopted = df[df["採用"].notna() & (df["採用"].astype(str).str.strip() != "")]
    print(f"レビュー対象: {len(df)}件 / 採用マーク済み: {len(adopted)}件")

    existing_fps = {r["fact_fingerprint"] for r in
                     client.select("sustainability_strategy_facts", {"select": "fact_fingerprint"})}

    rows = []
    for _, r in adopted.iterrows():
        fact = {}
        for col in FACT_COLUMNS:
            val = r.get(col)
            if pd.isna(val):
                val = None
            fact[col] = val
        if fact.get("major_actions"):
            fact["major_actions"] = [s.strip() for s in str(fact["major_actions"]).split("、") if s.strip()]
        else:
            fact["major_actions"] = []
        if fact.get("target_year") is not None:
            fact["target_year"] = int(fact["target_year"])
        fp = _fact_fingerprint(fact)
        if fp in existing_fps:
            continue
        fact["fact_fingerprint"] = fp
        rows.append(fact)
        existing_fps.add(fp)

    if not rows:
        print("新規追加対象なし（すべて登録済み）")
        return
    client.insert("sustainability_strategy_facts", rows, prefer="return=minimal")
    print(f"{len(rows)}件を登録しました（重複{len(adopted) - len(rows)}件はスキップ）")


# ─── Summarize: Facts集約 → theme_context文書を1テーマ1件で更新 ──────
SUMMARIZE_SYSTEM_PROMPT = """あなたは当社サステナビリティ専門家です。
あるテーマについて、当社の公式サステナビリティサイトから抽出された構造化ファクト群を
渡します。これをもとに、「このテーマで外部の変化を評価する際の判断軸」を60〜150字程度で
要約してください。

守るべきこと:
- 渡されたFactに無い情報を補完しない。Factが少ない/無い場合はその旨を書く
- 数値目標があれば触れてよいが、要約はFact全体を俯瞰した判断軸の説明にする
  （個々の数値の列挙ではない）

出力はJSON1個のみ: {"content": "..."}
"""
SUMMARIZE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["content"],
    "properties": {"content": {"type": "string"}},
}


def _load_jsonl_docs() -> list:
    if not KNOWLEDGE_DOCUMENTS_PATH.exists():
        return []
    with open(KNOWLEDGE_DOCUMENTS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _upsert_jsonl_doc(doc: dict) -> None:
    """document_idが同じ行を置き換える（追記ではなく1テーマ1件の更新にする）"""
    docs = [d for d in _load_jsonl_docs() if d.get("id") != doc["id"]]
    docs.append(doc)
    with open(KNOWLEDGE_DOCUMENTS_PATH, "w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def cmd_summarize(args):
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if azure_client is None:
        print("[ERROR] LLMクライアントを初期化できません")
        sys.exit(1)

    updated = 0
    for theme in THEMES:
        facts = client.select("sustainability_strategy_facts", {"select": "*", "theme": f"eq.{theme}"})
        if not facts:
            print(f"  [スキップ] {theme}: Factなし")
            continue
        user_prompt = f"テーマ: {theme}\n\nFact一覧:\n" + json.dumps(facts, ensure_ascii=False, indent=2)
        try:
            llm_result = common.call_llm_structured(
                azure_client, model, SUMMARIZE_SYSTEM_PROMPT, user_prompt,
                SUMMARIZE_SCHEMA, "theme_context_summary")
        except Exception as e:
            print(f"  [LLM失敗] {theme}: {type(e).__name__}: {e}")
            continue
        doc = {
            "id": f"STRATEGY-THEME-{THEME_JA_TO_EN[theme].upper()}",
            "title": f"{theme}の戦略文脈（構造化ファクト基盤）",
            "doc_type": "theme_context",
            "themes": [THEME_JA_TO_EN[theme]],
            "content": llm_result["data"]["content"],
            "source_url": facts[0]["source_url"],
            "retrieved_at": datetime.now().strftime("%Y-%m-%d"),
        }
        _upsert_jsonl_doc(doc)
        updated += 1
        print(f"  [OK] {theme}: {len(facts)}件のFactから更新")

    print(f"\n{updated}テーマを更新しました。検索インデックスを更新します。")
    subprocess.run([sys.executable, "sustainability_knowledge_store.py", "ingest"], cwd=BASE)


# ─── Coverage: テーマ別カバレッジ行列 ────────────────────────────────
def compute_coverage(client) -> dict:
    facts = client.select("sustainability_strategy_facts", {"select": "theme,strategy_element_type"})
    by_theme = defaultdict(set)
    for f in facts:
        by_theme[f["theme"]].add(f["strategy_element_type"])

    coverage = {}
    for theme in THEMES:
        types = by_theme.get(theme, set())
        if not types:
            coverage[theme] = "insufficient"
        elif types & {"numeric_target", "direction", "policy_commitment"}:
            coverage[theme] = "sufficient"
        else:
            coverage[theme] = "partial"
    return coverage


def cmd_coverage(args):
    config = load_config()
    client = SupabaseClient(config)
    facts = client.select("sustainability_strategy_facts", {"select": "theme,strategy_element_type"})
    by_theme = defaultdict(set)
    for f in facts:
        by_theme[f["theme"]].add(f["strategy_element_type"])

    coverage = compute_coverage(client)
    print(f"{'テーマ':16s} {'カバレッジ':12s} 内訳（strategy_element_type）")
    for theme in THEMES:
        types = sorted(by_theme.get(theme, set()))
        print(f"{theme:16s} {coverage[theme]:12s} {types}")


def main():
    parser = argparse.ArgumentParser(description="サステナビリティ戦略KB拡充")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--input", required=True)
    sub.add_parser("summarize")
    sub.add_parser("coverage")
    args = parser.parse_args()

    {"fetch": cmd_fetch, "apply": cmd_apply, "summarize": cmd_summarize,
     "coverage": cmd_coverage}[args.command](args)


if __name__ == "__main__":
    main()
