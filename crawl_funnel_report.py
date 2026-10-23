"""日次クロールのファネル集計（crawl_logs.notes のfunnel記録を読んで集計する。読み取り専用）。

使い方:
    python crawl_funnel_report.py                 # 昨日(日本時間)
    python crawl_funnel_report.py 2026-10-16      # 指定日(日本時間の暦日)
    python crawl_funnel_report.py 2026-10-16 2026-10-18   # 期間(両端を含む)

集計内容:
  - 実行対象数 / 結果別件数
  - Pass1失敗: 一覧取得失敗のtarget数、記事単位の失敗件数
  - Pass2の各段階（TinyFish Fetch / Zyte / TinyFish Agent）の投入数・抽出成功数
  - Pass2未到達（Pass1で失敗を記録したが、段階の記録が無いtarget。チャンクの時間切れ等）
  - 最終失敗件数
funnel記録を入れる前の日付のログには、記録が無いため0件になる。
"""
import sys
from collections import Counter, defaultdict
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from article_crawler import (SupabaseClient, _split_notes, choose_tier_order,  # noqa: E402
                             load_config)

JST = timezone(timedelta(hours=9))
TIERS = ("tinyfish_fetch", "zyte", "tinyfish_agent")


def summarize(logs: list) -> dict:
    """crawl_logsの行リストからファネルを集計する（DBに依存しない純粋関数）"""
    out = {
        "targets": len({l["crawl_target_id"] for l in logs}), "logs": len(logs),
        "results": Counter(l["run_result"] for l in logs),
        "p1_listing_failed_targets": 0, "p1_routed_targets": 0, "p1_tinyfish_direct_targets": 0, "p1_listing_fallback_targets": 0, "p1_article_failed": 0,
        "pending_outstanding": 0, "pending_overflow": 0, "budget_deferred": 0, "budget_skipped_known": 0, "budget_targets": 0, "pass2_done": 0, "pass2_expired": 0,
        "listing_retry": Counter(), "tiers": {t: [0, 0, 0.0] for t in TIERS},
        "final_failed": 0, "pass2_not_reached": 0, "with_funnel": 0,
        "fail_reasons": Counter(), "agent_skipped": 0,      # 2026-10-22追加: 最終失敗の理由(記事数)
        "include_nomatch": [],      # 2026-10-23追加: 条件に合う記事が0件なのに、記事らしいリンクが一覧にある対象 [(対象ID, {n, top})]
    }
    for l in logs:
        _, f = _split_notes(l.get("notes"))
        if not f:
            continue
        out["with_funnel"] += 1
        if f.get("p1_listing_failed"):
            out["p1_listing_failed_targets"] += 1
        if f.get("p1_routed_to_pass2"):
            out["p1_routed_targets"] += 1
        if f.get("p1_tinyfish_direct"):
            out["p1_tinyfish_direct_targets"] += 1
        if f.get("p1_listing_fallback"):
            out["p1_listing_fallback_targets"] += 1
        if f.get("listing_retry"):
            out["listing_retry"][f["listing_retry"]] += 1
        out["p1_article_failed"] += f.get("p1_article_failed", 0)
        out["pending_outstanding"] += len(f.get("p1_pending") or [])
        if f.get("p1_budget_deferred") or f.get("p1_budget_skipped_known"):
            out["budget_targets"] += 1
            out["budget_deferred"] += int(f.get("p1_budget_deferred") or 0)
            out["budget_skipped_known"] += int(f.get("p1_budget_skipped_known") or 0)
        out["pending_overflow"] += int(f.get("p1_pending_overflow") or 0)
        if f.get("pass2") == "done":
            out["pass2_done"] += 1
        elif f.get("pass2") == "expired" or f.get("listing_retry") == "expired":
            out["pass2_expired"] += 1
        out["final_failed"] += f.get("final_failed", 0)
        if f.get("include_nomatch"):
            out["include_nomatch"].append((l["crawl_target_id"], f["include_nomatch"]))
        out["fail_reasons"].update(f.get("fail_reasons") or {})
        out["agent_skipped"] += int(f.get("agent_skipped") or 0)
        for t, vals in (f.get("tiers") or {}).items():
            if t in out["tiers"]:
                out["tiers"][t][0] += vals[0]
                out["tiers"][t][1] += vals[1]
                out["tiers"][t][2] += vals[2] if len(vals) > 2 else 0  # 秒数は2026-10-19以降の記録のみ
        # Pass1で記事単位の失敗があるのに段階の記録が無い = Pass2未到達
        # （一覧取得失敗は「更新なし」の暫定行のまま、listing_retryが無い場合も未到達）
        # 再試行待ちが残っている(p1_pending)、または一覧取得が未完了 = Pass2未完了
        if f.get("p1_pending") or \
                ((f.get("p1_listing_failed") or f.get("p1_routed_to_pass2")) and not f.get("listing_retry")):
            out["pass2_not_reached"] += 1
    return out


def tier_profile(summary: dict, min_samples: int = 30) -> dict:
    """集計結果から、段階ごとの成功率(p)と1回あたりの平均秒数(t)を返す。
    試行数がmin_samples未満、または秒数の記録が無い段階は、p/tをNoneにする"""
    profile = {}
    for tier, (attempted, ok, sec) in summary["tiers"].items():
        enough = attempted >= min_samples
        profile[tier] = {"p": ok / attempted if enough else None,
                         "t": sec / attempted if enough and sec else None, "n": attempted}
    return profile


def render(s: dict, label: str) -> str:
    lines = [f"■ クロールファネル（{label}）",
             f"  実行ログ {s['logs']}件 / 対象 {s['targets']}件（うちfunnel記録あり {s['with_funnel']}件）",
             "  結果別: " + "、".join(f"{k} {v}" for k, v in s["results"].most_common()),
             "  [Pass1] 一覧取得の失敗(Pass2へ回送) " + str(s["p1_listing_failed_targets"])
             + "対象 / 記事単位の失敗 " + str(s["p1_article_failed"]) + "件",
             f"  [Pass1] 最初からPass2へ直送した対象(Zyte/JSON API方式) {s['p1_routed_targets']}対象"
             f" / Pass1でTinyFish Fetchを直接使った対象(Zyte停止中のZyte方式・一覧取得の再試行を含む) "
             f"{s['p1_tinyfish_direct_targets']}対象（うちHTMLの一覧取得をTinyFish Fetchで再試行 {s['p1_listing_fallback_targets']}対象）"]
    if s["listing_retry"]:
        lines.append("  [Pass2: 一覧取得リトライ] " + "、".join(f"{k} {v}" for k, v in s["listing_retry"].items()))
    for t in TIERS:
        a, o, sec = s["tiers"][t]
        avg = f"、平均{sec / a:.0f}秒/件" if a and sec else ""
        lines.append(f"  [Pass2: {t}] 投入 {a}件 → 抽出成功 {o}件" + (f"（成功率{o / a:.0%}{avg}）" if a else ""))
    lines.append(f"  最終失敗 {s['final_failed']}件 / Pass2未完了のtarget {s['pass2_not_reached']}件"
                 f"（再試行待ちの記事 {s['pending_outstanding']}件が繰越中、"
                 f"保存上限超過で再試行不能 {s['pending_overflow']}件）")
    if s["include_nomatch"]:
        lines.append(f"  [警告] 条件(include_paths)に合う記事が0件なのに、記事らしいリンクが一覧にある対象 "
                     f"{len(s['include_nomatch'])}件（設定ミス・サイト構成の変更の疑い）")
    if s["fail_reasons"]:
        lines.append("  [最終失敗の理由(記事数)] " + "、".join(f"{k} {v}" for k, v in s["fail_reasons"].most_common())
                     + (f" / うちAgentの上限で未実施 {s['agent_skipped']}件" if s["agent_skipped"] else ""))
    lines.append(f"  Pass2完了 {s['pass2_done']}対象 / 期限切れで失敗確定 {s['pass2_expired']}対象")
    lines.append(f"  1対象の時間上限に達した対象 {s['budget_targets']}対象（再試行待ちへ繰越 {s['budget_deferred']}件、"
                 f"既知のため省略 {s['budget_skipped_known']}件）")
    prof = tier_profile(s)
    order = choose_tier_order(prof)
    ready = all(prof[t][k] is not None for t in ("tinyfish_fetch", "zyte") for k in ("p", "t"))
    lines.append("  [段階の順番] " + (f"期待時間が小さい順の推奨 = {' → '.join(order)}"
                                     "（参考: 2段目は1段目の失敗分のみのため成功率が低く出る）"
                                     if ready else "成功率・秒数の記録が不足（各30件以上必要）のため、既定の順を維持"))
    return "\n".join(lines)


def fetch_logs(client, start_jst: datetime, end_jst: datetime) -> list:
    return client.select("crawl_logs", {
        "select": "crawl_target_id,run_result,notes,started_at",
        "started_at": f"gte.{start_jst.astimezone(timezone.utc).isoformat()}",
        "and": f"(started_at.lt.{end_jst.astimezone(timezone.utc).isoformat()})",
    })


def main(argv: list):
    today = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    if len(argv) >= 2:
        first = datetime.strptime(argv[1], "%Y-%m-%d").replace(tzinfo=JST)
        last = datetime.strptime(argv[2], "%Y-%m-%d").replace(tzinfo=JST) if len(argv) >= 3 else first
    else:
        first = last = today - timedelta(days=1)
    client = SupabaseClient(load_config())
    logs = fetch_logs(client, first, last + timedelta(days=1))
    by_day = defaultdict(list)
    for l in logs:
        ts = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], l["started_at"].replace("Z", "+00:00"))  # 小数秒を6桁に（Python3.10対策）
        d = datetime.fromisoformat(ts).astimezone(JST).strftime("%Y-%m-%d")
        by_day[d].append(l)
    names = None
    for d in sorted(by_day):
        summary = summarize(by_day[d])
        print(render(summary, d))
        if summary["include_nomatch"]:
            if names is None:
                names = {t["crawl_target_id"]: t["target_name"] for t in client.select(
                    "crawl_targets", {"select": "crawl_target_id,target_name"})}
            for tid, info in summary["include_nomatch"][:30]:
                print(f"    - {names.get(tid, tid)[:30]}: 除外された記事らしいリンク{info.get('n')}件 例: {', '.join(info.get('top') or [])}")
        print()
    if len(by_day) > 1:
        print(render(summarize(logs), f"{first:%Y-%m-%d}〜{last:%Y-%m-%d} 合計"))
    if not logs:
        print("該当期間のcrawl_logsがありません。")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv)
