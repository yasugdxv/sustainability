# -*- coding: utf-8 -*-
"""Weekly Strategic Question 生成CLI。

python generate_strategic_question.py evaluate --themes water,ghg,packaging
    DB保存を一切行わないdry-runモード。プロンプト品質評価・実データ検証専用。

python generate_strategic_question.py build
    本番実行。close_due_questions→generate_pending_insights→（対象週にdraftが無ければ）
    新規生成、の順で行う。weekly_email_report.py build の直前に実行する運用を想定。

python generate_strategic_question.py insights
    generate_pending_insights(...)のみを実行する運用フォールバック
    （ダッシュボードのinsight_status=errorの警告を見てPMOが手動実行する想定）
"""
import argparse
import sys
from datetime import datetime, timedelta

import pandas as pd

import strategic_question_service as sqs
from ai_client import make_openai_client
from article_crawler import SupabaseClient
from config_utils import load_config
from expand_sustainability_knowledge import THEMES

THEME_ALIASES = {
    "water": "水", "ghg": "気候変動・GHG", "climate": "気候変動・GHG",
    "packaging": "容器包装", "raw_materials": "原料調達", "materials": "原料調達",
    "biodiversity": "生物多様性", "human_rights": "人権", "health": "健康",
    "human_capital": "人的資本", "responsible_marketing": "責任あるマーケティング",
    "marketing": "責任あるマーケティング",
}


def _resolve_themes(arg_value: str) -> list:
    if not arg_value:
        return THEMES
    result = []
    for token in arg_value.split(","):
        token = token.strip()
        if not token:
            continue
        ja = THEME_ALIASES.get(token.lower(), token)
        if ja not in THEMES:
            print(f"[WARNING] 不明なテーマ指定: {token}（無視します）")
            continue
        result.append(ja)
    return result or THEMES


def cmd_evaluate(args):
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if azure_client is None:
        print("[ERROR] LLMクライアントを初期化できません")
        sys.exit(1)

    target_themes = _resolve_themes(args.themes)
    print(f"評価対象テーマ: {', '.join(target_themes)}")

    signals = sqs.collect_signals(client, config,
                                   since_days_signals=args.since_days_signals,
                                   since_days_competitor=args.since_days_competitor)
    coverage = sqs.get_strategy_coverage(client)
    bundles = sqs.cluster_candidate_signals(signals, coverage)
    bundles = [b for b in bundles if b["theme"] in target_themes]
    recent_keys = sqs.fetch_recent_dimension_keys(client, lookback_weeks=10)

    rows = []
    for bundle in bundles:
        print(f"\n=== {bundle['theme']} ===")
        print(f"  外部記事{len(bundle['articles'])}件 / 競合変更{len(bundle['competitor_changes'])}件 / "
              f"競合取組{len(bundle['competitor_initiatives'])}件 / 戦略ファクト{len(bundle['strategy_facts'])}件 "
              f"/ カバレッジ={bundle['coverage']}")
        try:
            analysis = sqs.run_strategic_analysis(azure_client, model, bundle)
        except Exception as e:
            print(f"  [分析失敗] {type(e).__name__}: {e}")
            rows.append({"theme": bundle["theme"], "coverage": bundle["coverage"],
                         "has_real_tradeoff": None, "error": f"分析失敗: {e}"})
            continue

        signal_raw = sqs._signal_strength_raw(bundle)
        stage1 = sqs.score_stage1(analysis, bundle["coverage"], signal_raw)
        row = {
            "theme": bundle["theme"], "coverage": bundle["coverage"],
            "has_real_tradeoff": analysis["has_real_tradeoff"],
            "strategic_implications": " / ".join(analysis["strategic_implications"]),
            "suntory_strategy_relation": analysis["suntory_strategy_relation"]["relation_summary"],
            "competitor_comparison": analysis["competitor_comparison"]["summary"],
            "tension_summary": analysis["strategic_tension"]["tension_summary"],
            "decision_dimension_key": analysis["strategic_tension"]["decision_dimension_key"],
            "decision_dimension_label": analysis["strategic_tension"]["decision_dimension_label"],
            "tension_strength": analysis["strategic_tension"]["tension_strength"],
            "stage1_score": round(stage1, 1),
        }
        print(f"  対立軸: {analysis['strategic_tension']['decision_dimension_label']}"
              f"（{analysis['strategic_tension']['decision_dimension_key']}）"
              f" tension={analysis['strategic_tension']['tension_strength']} stage1={stage1:.1f}")

        if not analysis["has_real_tradeoff"]:
            row["question_text"] = ""
            row["selected_or_rejected"] = "見送り（has_real_tradeoff=false）"
            rows.append(row)
            print("  → 対立軸なし、質問生成をスキップ")
            continue

        try:
            question = sqs.generate_question_from_analysis(azure_client, model, analysis)
        except Exception as e:
            print(f"  [生成失敗] {type(e).__name__}: {e}")
            row["error"] = f"生成失敗: {e}"
            rows.append(row)
            continue

        stage2 = sqs.score_stage2(bundle["theme"], analysis["strategic_tension"]["decision_dimension_key"],
                                   stage1, question, recent_keys)
        options_text = " / ".join(f"{o['option_code']}:{o['label']}" for o in question["options"])
        row.update({
            "title": question["title"],
            "question_text": question["question_text"],
            "options": options_text,
            "trade_off_quality": question["trade_off_quality"],
            "neutrality": question["neutrality"],
            "answerability": question["answerability"],
            "stage2_score": round(stage2["score"], 1),
            "selected_or_rejected": "重複のため除外" if stage2["is_duplicate"] else "選定候補",
        })
        rows.append(row)
        print(f"  質問: {question['question_text']}")
        print(f"  選択肢: {options_text}")
        print(f"  trade_off_quality={question['trade_off_quality']} neutrality={question['neutrality']} "
              f"answerability={question['answerability']} stage2={stage2['score']:.1f}"
              f"{'（重複のため除外）' if stage2['is_duplicate'] else ''}")

    survivors = [r for r in rows if r.get("stage2_score") is not None and r["selected_or_rejected"] == "選定候補"]
    if survivors:
        best = max(survivors, key=lambda r: r["stage2_score"])
        for r in rows:
            if r is best:
                r["selected_or_rejected"] = "★選定★"
        print(f"\n=== 今週選ばれるとしたら: {best['theme']} 「{best['question_text']}」 ===")
    else:
        print("\n=== 選定候補なし（全テーマ見送りまたは重複） ===")

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = f"docs/{ts}_strategic_question_evaluate.xlsx"
    pd.DataFrame(rows).to_excel(out_path, index=False)
    print(f"\n出力: {out_path}（DB保存なし、dry-run）")


def cmd_build(args):
    """1) close_due_questions（LLM無し、時間経過分を機械的にclosed化）
    2) generate_pending_insights（closed×insight_status in (pending,error)を処理）
    3) 対象週にdraft質問が無ければ、9テーマ分析→上位3テーマ質問生成→2段階ランキング→保存"""
    config = load_config()
    client = SupabaseClient(config)
    if not sqs.is_strategic_question_enabled(config):
        print("strategic_question.enabled が無効です。処理を行わず終了します。")
        return
    azure_client, model = make_openai_client(config)
    if azure_client is None:
        print("[ERROR] LLMクライアントを初期化できません")
        sys.exit(1)

    closed = sqs.close_due_questions(client)
    if closed:
        print(f"クローズした質問: {len(closed)}件")
    insight_results = sqs.generate_pending_insights(client, azure_client, model, config)
    if insight_results:
        print(f"Decision Insight処理: {len(insight_results)}件")

    cfg = config.get("strategic_question", {})
    period_end = datetime.now()
    period_start_date = (period_end - timedelta(days=7)).date().isoformat()
    period_end_date = period_end.date().isoformat()

    existing = client.select("sustainability_strategic_questions",
                              {"select": "question_id", "period_start": f"eq.{period_start_date}"})
    if existing:
        print(f"period_start={period_start_date} の質問は既に存在します（question_id={existing[0]['question_id']}）。"
              "新規生成はスキップします。")
        return

    signals = sqs.collect_signals(client, config,
                                   since_days_signals=cfg.get("since_days_signals", 14),
                                   since_days_competitor=cfg.get("since_days_competitor", 14))
    coverage = sqs.get_strategy_coverage(client)
    bundles = sqs.cluster_candidate_signals(signals, coverage)
    recent_keys = sqs.fetch_recent_dimension_keys(client, cfg.get("duplicate_lookback_weeks", 10))

    stage1_results = []
    for bundle in bundles:
        try:
            analysis = sqs.run_strategic_analysis(azure_client, model, bundle)
        except Exception as e:
            print(f"  [{bundle['theme']}] 分析失敗: {type(e).__name__}: {e}")
            continue
        if not analysis["has_real_tradeoff"]:
            continue
        signal_raw = sqs._signal_strength_raw(bundle)
        stage1_score = sqs.score_stage1(analysis, bundle["coverage"], signal_raw)
        stage1_results.append({"theme": bundle["theme"], "analysis": analysis, "stage1_score": stage1_score})

    top3 = sorted(stage1_results, key=lambda r: r["stage1_score"], reverse=True)[:3]
    print(f"has_real_tradeoff=trueのテーマ: {len(stage1_results)}件 → 上位{len(top3)}件で質問生成")

    candidates = []
    for r in top3:
        try:
            question = sqs.generate_question_from_analysis(azure_client, model, r["analysis"])
        except Exception as e:
            print(f"  [{r['theme']}] 質問生成失敗: {type(e).__name__}: {e}")
            continue
        stage2 = sqs.score_stage2(r["theme"], r["analysis"]["strategic_tension"]["decision_dimension_key"],
                                   r["stage1_score"], question, recent_keys)
        candidates.append({**r, "question": question, "stage2": stage2,
                            "stage2_score": stage2["score"]})

    best = sqs.select_best_candidate(candidates)
    if best is None:
        print("選定候補なし（全テーマ見送りまたは重複）。今週は出題を見送ります。")
        return

    question_id = sqs.save_draft_question(
        client, period_start=period_start_date, period_end=period_end_date, selected=best,
        all_candidates=[{"theme": c["theme"], "stage1_score": c["stage1_score"],
                          "stage2_score": c["stage2_score"], "is_duplicate": c["stage2"]["is_duplicate"]}
                         for c in candidates],
        response_window_days=cfg.get("response_window_days", 5),
        model_deployment=model, token_usage=None, latency_ms=None)
    print(f"質問を保存しました（question_id={question_id}, theme={best['theme']}, "
          f"question_status=draft）。次回の weekly_email_report.py build で埋め込まれます。")


def cmd_insights(args):
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if azure_client is None:
        print("[ERROR] LLMクライアントを初期化できません")
        sys.exit(1)
    processed = sqs.generate_pending_insights(client, azure_client, model, config)
    print(f"Decision Insight処理: {len(processed)}件")


def main():
    parser = argparse.ArgumentParser(description="Weekly Strategic Question 生成CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("evaluate")
    p_eval.add_argument("--themes", default="", help="カンマ区切り（例: water,ghg,packaging）。省略時は全9テーマ")
    p_eval.add_argument("--since-days-signals", type=int, default=14)
    p_eval.add_argument("--since-days-competitor", type=int, default=14)

    sub.add_parser("build")
    sub.add_parser("insights")

    args = parser.parse_args()
    {"evaluate": cmd_evaluate, "build": cmd_build, "insights": cmd_insights}[args.command](args)


if __name__ == "__main__":
    main()
