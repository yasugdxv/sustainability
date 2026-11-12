"""
Selector Operational Recovery（selectorの未評価Backlog解消と運用監視）

目的: Tier0適格(best_rankがS/A/B)なのに、selectorの成功評価を一度も受けていない事象クラスタを
「Backlog」として明示し、上限付きで少しずつ消化できるようにする。あわせて、現在のselectorの状態
（最終成功日時・Backlog件数・失敗など）を既存データ（expert_runs）だけから導出する。

このモジュールが変更しないもの（意図的に触れない）:
  記事の事前フィルタ／LLM記事スコアリング／S〜Dのランク閾値／TIER0_RANK_FLOOR／certainty_stage／
  クラスタ構築／list_weekly_picks（週次レポートの選定ロジック）／コンテンツ生成

DB書き込み: このモジュール自体はDBへ書き込まない（読み取りのみ）。LLM呼び出しもしない。
実際の評価は、既存の sustainability_article_selector.select_cluster(s) が行う。
"""
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sustainability_article_selector as sas
import sustainability_expert_common as common

BASE = Path(__file__).parent
LOCK_PATH = BASE / "selector_recovery.lock"
RUN_LOG_DIR = BASE / "logs" / "selector_recovery"

# ─── 設定値（コード中にmagic numberを散らさないため、ここに集約する） ──────────────
# config.json の sustainability_expert.selector_recovery で上書きできる。
DEFAULTS = {
    # 1回の実行で処理する上限。実績: 約90秒/クラスタ（60〜115秒）。週次の既存フローは約124クラスタ
    # ≒186分。Decision Record D9（360分で警告）に収まるよう、Backlogは約1時間分を上限にする
    "recent_limit": 150,
    "backlog_limit": 40,
    # Recentの枠が余った場合、その分をBacklogに回す（Backlogの飢餓防止の逆方向。Recentは常に優先）
    "spillover_unused_recent_slots": True,
    # Recent Queue（既存のTier0選定と同じ。週次の意味づけを変えない）
    "recent_top_n_per_theme": 10,
    "recent_since_days": 8,
    # 失敗の扱い
    "max_failures": 3,                 # この回数失敗したクラスタは needs_review にして自動では再試行しない
    "retry_cooldown_hours": 24,        # 直近の失敗からこの時間は再試行しない（連打防止）
    "permanent_error_markers": ["content_filter", "ResponsibleAIPolicyViolation", "context_length_exceeded"],
    # 状態判定（Operations/--status用）
    "stale_days": 7,                   # Decision Record D3（最新成功が7日以内なら利用可）と同じ
    "backlog_warn_total": 500,
    "backlog_warn_oldest_days": 90,
    "failed_lookback_hours": 48,
    # 見積り用（実績からの概算。実測値ではない）
    "est_seconds_per_cluster": 90,
    "est_tokens_per_cluster": 17420,
    "lock_max_age_hours": 8,           # 420分(D10)を超えたロックは異常終了とみなす
}

STATE_EVALUATED = "evaluated"
STATE_UNEVALUATED = "unevaluated"
STATE_RETRYABLE = "retryable"
STATE_COOLDOWN = "cooldown"
STATE_NEEDS_REVIEW = "needs_review"
SELECTABLE_STATES = {STATE_UNEVALUATED, STATE_RETRYABLE}


def get_config(config: dict | None = None) -> dict:
    """DEFAULTSにconfig.jsonの上書きを重ねた設定を返す"""
    override = ((config or {}).get("sustainability_expert") or {}).get("selector_recovery") or {}
    return {**DEFAULTS, **{k: v for k, v in override.items() if k in DEFAULTS}}


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ReadOnlyClient:
    """select以外を持たないクライアント。dry-run/statusで、DB書き込みが構造的に起きないようにする"""

    def __init__(self, client):
        self._client = client

    def select(self, table: str, params: dict) -> list:
        return self._client.select(table, params)

    def __getattr__(self, name):
        raise RuntimeError(f"読み取り専用クライアントでは {name} を使えません")


# ─── Tier0適格クラスタ ────────────────────────────────────────────
def build_eligible_index(client, all_urls: dict, floor: set | None = None) -> tuple[dict, dict]:
    """全期間で、best_rankがfloor（既定TIER0_RANK_FLOOR）に含まれるクラスタの索引を作る。
    除外条件は既存のTier0選定と同じ: 自社発（SJ-12）の記事、suppressed_duplicate。
    戻り値: (適格クラスタ索引, 全クラスタのrank別件数)
      索引: {cluster_id: {"best_rank", "best_key", "published_at"(クラスタ内の最新公開日 or None)}}"""
    floor = sas.TIER0_RANK_FLOOR if floor is None else floor
    own_target_ids = {
        t["crawl_target_id"] for t in client.select("crawl_targets", {"select": "crawl_target_id,publisher_tag_id"})
        if t.get("publisher_tag_id") == sas.OWN_COMPANY_TAG_ID
    }
    articles = client.select("articles", {"select": "article_id,article_url_id,crawl_target_id,published_at",
                                          "is_current": "eq.true"})
    articles = [a for a in articles if a.get("crawl_target_id") not in own_target_ids]
    analysis = {a["article_id"]: a for a in client.select(
        "article_analysis", {"select": "article_id,importance_level,importance_total_score,representative_role",
                             "is_current": "eq.true"})}

    clusters = {}
    for a in articles:
        an = analysis.get(a["article_id"], {})
        if an.get("representative_role") == "suppressed_duplicate":
            continue
        url_row = all_urls.get(a["article_url_id"], {"article_url_id": a["article_url_id"]})
        root = common.resolve_cluster_root(url_row, all_urls)
        c = clusters.setdefault(root, {"best_rank": None, "best_key": None, "published_at": None})
        score, level = an.get("importance_total_score"), an.get("importance_level")
        key = (sas.RANK_ORDER.get(level, 5), -(score if score is not None else -1))  # 既存の式と同じ
        if c["best_key"] is None or key < c["best_key"]:
            c["best_key"], c["best_rank"] = key, level
        pub = _parse_ts(a.get("published_at"))
        if pub and (c["published_at"] is None or pub > c["published_at"]):
            c["published_at"] = pub

    rank_counts = {}
    for c in clusters.values():
        rank_counts[c["best_rank"]] = rank_counts.get(c["best_rank"], 0) + 1
    return {cid: c for cid, c in clusters.items() if c["best_rank"] in floor}, rank_counts


# ─── 評価状態（expert_runsから導出。新しいテーブルは作らない） ──────────────────────
def load_select_history(client, cfg: dict) -> dict:
    """task_type='select'のexpert_runsをクラスタ単位に集約する（output_jsonは取得しない）。
    {cluster_id: {"success": bool, "success_at", "failures": int, "failure_statuses": [...],
                  "last_failure_at", "last_attempt_at", "last_attempt_status", "permanent": bool}}"""
    rows = client.select("expert_runs", {"select": "article_cluster_id,status,created_at,error_message",
                                         "task_type": "eq.select"})
    return summarize_history(rows, cfg["permanent_error_markers"])


def summarize_history(rows: list, permanent_markers: list) -> dict:
    hist = {}
    for r in rows:
        cid = r.get("article_cluster_id")
        if not cid:
            continue
        h = hist.setdefault(cid, {"success": False, "success_at": None, "failures": 0, "failure_statuses": [],
                                  "last_failure_at": None, "last_attempt_at": None,
                                  "last_attempt_status": None, "permanent": False})
        ts = _parse_ts(r.get("created_at"))
        status = r.get("status")
        if h["last_attempt_at"] is None or (ts and ts > h["last_attempt_at"]):
            h["last_attempt_at"], h["last_attempt_status"] = ts, status
        if status == "success":
            h["success"] = True
            if ts and (h["success_at"] is None or ts > h["success_at"]):
                h["success_at"] = ts
        else:
            h["failures"] += 1
            h["failure_statuses"].append(status)
            if ts and (h["last_failure_at"] is None or ts > h["last_failure_at"]):
                h["last_failure_at"] = ts
            msg = r.get("error_message") or ""
            if status == "error" and any(m in msg for m in permanent_markers):
                h["permanent"] = True  # 入力そのものが原因の失敗（再試行しても同じ）
    return hist


def classify_cluster(hist_row: dict | None, cfg: dict, now: datetime) -> str:
    """成功評価を一度も持たないものだけがBacklog。expert_runsに行があるだけでは評価済みにしない。"""
    if hist_row is None:
        return STATE_UNEVALUATED
    if hist_row["success"]:
        return STATE_EVALUATED
    if hist_row["failures"] == 0:
        return STATE_UNEVALUATED
    if hist_row["permanent"] or hist_row["failures"] >= cfg["max_failures"]:
        return STATE_NEEDS_REVIEW
    last = hist_row["last_failure_at"]
    if last and now - last < timedelta(hours=cfg["retry_cooldown_hours"]):
        return STATE_COOLDOWN
    return STATE_RETRYABLE


# ─── キュー計画 ────────────────────────────────────────────────
def plan_run(client, all_urls: dict, cfg: dict, include_backlog: bool = False, now: datetime | None = None,
             recent_ids: list | None = None, limit: int | None = None) -> dict:
    """RecentキューとBacklogキューを作る。DB書き込み・LLM呼び出しは行わない。
    Recent: 既存のTier0選定（テーマ別上位N件・直近N日）から、成功評価の無いもの。Recentが常に先。
    Backlog: 全期間のTier0適格（S/A/B）のうち、成功評価を一度も持たず、再試行可能なもの。
             since_daysでは切らない。順序は ランク(S>A>B)→スコア降順→古い順。"""
    now = now or datetime.now(timezone.utc)
    eligible, rank_counts = build_eligible_index(client, all_urls)
    history = load_select_history(client, cfg)

    if recent_ids is None:
        recent_ids = sas.list_theme_prioritized_cluster_ids(
            client, all_urls, top_n=cfg["recent_top_n_per_theme"], since_days=cfg["recent_since_days"] or None)

    def state_of(cid):
        return classify_cluster(history.get(cid), cfg, now)

    recent_states = {cid: state_of(cid) for cid in recent_ids}
    recent_unevaluated = [cid for cid in recent_ids if recent_states[cid] in SELECTABLE_STATES]
    recent_unevaluated.sort(key=lambda cid: (eligible.get(cid, {}).get("best_key") or (9, 0)))
    recent_batch = recent_unevaluated[:cfg["recent_limit"]]

    recent_set = set(recent_ids)
    backlog_states = {cid: state_of(cid) for cid in eligible if cid not in recent_set}
    backlog_all = [cid for cid, s in backlog_states.items() if s != STATE_EVALUATED]
    backlog_selectable = [cid for cid in backlog_all if backlog_states[cid] in SELECTABLE_STATES]
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    backlog_selectable.sort(key=lambda cid: (eligible[cid]["best_key"], eligible[cid]["published_at"] or far_future, cid))

    spill = max(0, cfg["recent_limit"] - len(recent_batch)) if cfg["spillover_unused_recent_slots"] else 0
    backlog_slots = cfg["backlog_limit"] + spill
    backlog_batch = backlog_selectable[:backlog_slots] if include_backlog else []

    queue = recent_batch + backlog_batch
    if limit:
        queue = queue[:limit]
        in_queue = set(queue)
        recent_batch = [c for c in recent_batch if c in in_queue]
        backlog_batch = [c for c in backlog_batch if c in in_queue]

    # Backlogの内訳（監視用）。Recent対象ではない適格クラスタのうち、成功評価が無いもの
    by_rank = {r: sum(1 for c in backlog_all if eligible[c]["best_rank"] == r) for r in ("S", "A", "B")}
    dated = [eligible[c]["published_at"] for c in backlog_all if eligible[c]["published_at"]]
    state_counts = {s: sum(1 for c in backlog_all if backlog_states[c] == s)
                    for s in (STATE_UNEVALUATED, STATE_RETRYABLE, STATE_COOLDOWN, STATE_NEEDS_REVIEW)}
    schema_invalid_only = sum(1 for c in backlog_all
                              if history.get(c) and history[c]["failures"] > 0 and not history[c]["success"]
                              and set(history[c]["failure_statuses"]) == {"schema_invalid"})

    funnel = {
        "eligible_clusters": len(eligible),
        "recent_candidates": len(recent_ids),
        "recent_already_evaluated": sum(1 for s in recent_states.values() if s == STATE_EVALUATED),
        "recent_unevaluated": len(recent_unevaluated),
        "backlog_candidates": len(backlog_all),
        "backlog_selectable_now": len(backlog_selectable),
        "submitted_to_selector": len(queue),
        "submitted_recent": len(recent_batch),
        "submitted_backlog": len(backlog_batch),
    }
    return {
        "generated_at": now.isoformat(), "include_backlog": include_backlog, "config": cfg,
        "funnel": funnel, "rank_counts_all_clusters": {str(k): v for k, v in rank_counts.items()},
        "eligible_by_rank": {r: sum(1 for c in eligible.values() if c["best_rank"] == r) for r in ("S", "A", "B")},
        "backlog": {"total": len(backlog_all), "by_rank": by_rank, "by_state": state_counts,
                    "schema_invalid_only": schema_invalid_only,
                    "oldest_cluster_date": min(dated).isoformat() if dated else None},
        "recent_batch": recent_batch, "backlog_batch": backlog_batch, "queue": queue,
        "backlog_batch_by_rank": {r: sum(1 for c in backlog_batch if eligible[c]["best_rank"] == r) for r in ("S", "A", "B")},
    }


def estimate(plan: dict, batch_size: int = 1) -> dict:
    cfg, n = plan["config"], plan["funnel"]["submitted_to_selector"]
    return {
        "clusters": n,
        "llm_calls": math.ceil(n / max(batch_size, 1)),
        "estimated_minutes": round(n * cfg["est_seconds_per_cluster"] / 60),
        "estimated_tokens": n * cfg["est_tokens_per_cluster"],
        "basis": "過去実績からの概算（約90秒/クラスタ、約17,420トークン/クラスタ）。実測値ではなく、金額は契約単価が不明のため算出しない",
    }


# ─── 状態判定（Operations / --status 用） ──────────────────────────
def get_selector_status(client, cfg: dict, now: datetime | None = None, plan: dict | None = None) -> dict:
    """expert_runsから導出するselectorの健全性。Healthy / Backlog growing / Selector stale / Failed / Unknown"""
    now = now or datetime.now(timezone.utc)
    rows = client.select("expert_runs", {"select": "article_cluster_id,status,created_at", "task_type": "eq.select"})
    if not rows:
        return {"state": "Unknown", "reason": "expert_runsにselectの実行履歴が無い", "generated_at": now.isoformat()}
    stamped = [(_parse_ts(r.get("created_at")), r.get("status")) for r in rows if _parse_ts(r.get("created_at"))]
    successes = [t for t, s in stamped if s == "success"]
    last_success = max(successes) if successes else None
    last_attempt = max(t for t, _ in stamped)
    window = now - timedelta(hours=cfg["failed_lookback_hours"])
    recent = [s for t, s in stamped if t >= window]
    recent_ok = sum(1 for s in recent if s == "success")
    latest_run_day = max(t for t, _ in stamped).date()
    latest_run_rows = [s for t, s in stamped if t.date() == latest_run_day]

    backlog = (plan or {}).get("backlog") or {}
    oldest = _parse_ts(backlog.get("oldest_cluster_date"))
    oldest_age_days = (now - oldest).days if oldest else None

    state, reason = "Healthy", "最新の成功評価が基準日数以内で、Backlogも基準以内"
    if recent and recent_ok == 0:
        state, reason = "Failed", f"直近{cfg['failed_lookback_hours']}時間の実行{len(recent)}件が全て失敗"
    elif last_success is None or now - last_success > timedelta(days=cfg["stale_days"]):
        state = "Selector stale"
        reason = ("成功評価が1件も無い" if last_success is None
                  else f"最新の成功評価が{(now - last_success).days}日前（基準{cfg['stale_days']}日）")
    elif backlog and (backlog["total"] >= cfg["backlog_warn_total"]
                      or (oldest_age_days is not None and oldest_age_days >= cfg["backlog_warn_oldest_days"])):
        state = "Backlog growing"
        reason = f"Backlog {backlog['total']}件（基準{cfg['backlog_warn_total']}件）、最古{oldest_age_days}日前"
    return {
        "state": state, "reason": reason, "generated_at": now.isoformat(),
        "last_success_at": last_success.isoformat() if last_success else None,
        "last_attempt_at": last_attempt.isoformat(),
        "evaluated_on_latest_run_day": sum(1 for s in latest_run_rows if s == "success"),
        "schema_invalid_or_failed_on_latest_run_day": sum(1 for s in latest_run_rows if s != "success"),
        "backlog": backlog or None, "oldest_backlog_age_days": oldest_age_days,
    }


# ─── 二重実行防止（run_daily.pyと同じロックファイル方式。DB変更なし） ────────────────
def acquire_lock(max_age_hours: float, path: Path = LOCK_PATH) -> bool:
    if path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            return False
    path.write_text(f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()}")
    return True


def release_lock(path: Path = LOCK_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ─── 実行ごとのFunnel記録（ファイル。DBスキーマは変更しない） ──────────────────────────
def write_run_log(plan: dict, outcomes: dict, directory: Path = RUN_LOG_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_selector_recovery.json"
    funnel = dict(plan["funnel"])
    funnel.update({
        "selector_success": outcomes.get("success", 0),
        "skipped_duplicate": outcomes.get("skipped_duplicate", 0),
        "schema_invalid": outcomes.get("schema_invalid", 0),
        "failed": outcomes.get("error", 0),
    })
    path.write_text(json.dumps({"generated_at": plan["generated_at"], "include_backlog": plan["include_backlog"],
                                "funnel": funnel, "backlog": plan["backlog"],
                                "queue_size": len(plan["queue"])}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ─── 表示 ─────────────────────────────────────────────────────
def format_plan(plan: dict, est: dict) -> str:
    f, b = plan["funnel"], plan["backlog"]
    lines = [
        "=== Selector Recovery 計画（DB書き込みなし・LLM呼び出しなし） ===",
        f"Tier0適格クラスタ合計: {f['eligible_clusters']}（S/A/B: {plan['eligible_by_rank']}）",
        f"Recent候補: {f['recent_candidates']}（評価済み {f['recent_already_evaluated']} / 未評価 {f['recent_unevaluated']}）",
        f"Backlog合計: {b['total']}（S/A/B: {b['by_rank']}）",
        f"  内訳: {b['by_state']}（schema_invalidのみで未成功: {b['schema_invalid_only']}件）",
        f"  最古クラスタの公開日: {b['oldest_cluster_date']}",
        f"今回処理するバッチ: Recent {f['submitted_recent']}件 + Backlog {f['submitted_backlog']}件"
        f"（Backlog内訳 {plan['backlog_batch_by_rank']}）"
        + ("" if plan["include_backlog"] else "  ※--include-backlog未指定のためBacklogは対象外"),
        f"見積り: LLM呼び出し {est['llm_calls']}回 / 約{est['estimated_minutes']}分 / 約{est['estimated_tokens']:,}トークン",
        f"  ({est['basis']})",
    ]
    if plan["backlog_batch"]:
        lines.append(
            "【注意】Backlogの評価結果は expert_runs.created_at が実行日になるため、週次メールの list_weekly_picks"
            "（直近7日のselect成功を対象）に『今週の候補』として混ざる（公開日では絞っていない）。"
            "週次メールの直前にBacklogを実行しない／混入を許容するかを、実行前に決めておくこと。")
    return "\n".join(lines)
