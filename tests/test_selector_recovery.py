"""Selector Operational Recovery（selector_recovery.py）のテスト。実DB・実LLMには一切アクセスしない。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import selector_recovery as rec  # noqa: E402
import sustainability_article_selector as sas  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
CFG = {**rec.DEFAULTS, "recent_limit": 3, "backlog_limit": 2, "max_failures": 3, "retry_cooldown_hours": 24}
NO_SPILL = {**CFG, "spillover_unused_recent_slots": False}


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _world(specs: list, runs: list = ()):
    """specs: [(クラスタ番号, ランク, スコア, 公開が何日前か)]。1クラスタ=1記事=URL1件"""
    articles, analysis, urls = [], [], {}
    for n, rank, score, days_ago in specs:
        urls[f"u{n}"] = {"article_url_id": f"u{n}", "duplicate_of_article_url_id": None}
        articles.append({"article_id": f"a{n}", "article_url_id": f"u{n}", "crawl_target_id": "t1",
                         "published_at": _iso(days_ago), "is_current": True})
        analysis.append({"article_id": f"a{n}", "importance_level": rank, "importance_total_score": score,
                         "representative_role": "representative", "is_current": True})
    client = FakeSupabaseClient({
        "crawl_targets": [{"crawl_target_id": "t1", "publisher_tag_id": None}],
        "articles": articles, "article_analysis": analysis, "expert_runs": list(runs),
        "tag_reference": [], "article_tags": [],
    })
    return client, urls


def _run(cluster, status="success", days_ago=1.0, error=None):
    return {"article_cluster_id": cluster, "task_type": "select", "status": status,
            "created_at": _iso(days_ago), "error_message": error}


def _plan(client, urls, recent_ids=(), include_backlog=True, cfg=CFG, **kw):
    return rec.plan_run(client, urls, cfg, include_backlog=include_backlog, now=NOW, recent_ids=list(recent_ids), **kw)


# ─── Eligibility ───────────────────────────────────────────────
@pytest.mark.parametrize("rank,eligible", [("S", True), ("A", True), ("B", True), ("C", False), ("D", False)])
def test_eligibility_follows_tier0_rank_floor(rank, eligible):
    client, urls = _world([(1, rank, 20, 100)])
    index, _ = rec.build_eligible_index(client, urls)
    assert ("u1" in index) is eligible


def test_tier0_rank_floor_is_unchanged():
    assert sas.TIER0_RANK_FLOOR == {"S", "A", "B"}


def test_suppressed_duplicate_and_own_company_are_not_eligible():
    client, urls = _world([(1, "S", 30, 10), (2, "S", 30, 10)])
    client.tables["article_analysis"][1]["representative_role"] = "suppressed_duplicate"
    client.tables["crawl_targets"].append({"crawl_target_id": "own", "publisher_tag_id": sas.OWN_COMPANY_TAG_ID})
    client.tables["articles"][0]["crawl_target_id"] = "own"
    index, _ = rec.build_eligible_index(client, urls)
    assert index == {}


# ─── 評価状態 ───────────────────────────────────────────────────
def _state(rows, cluster="u1", cfg=CFG):
    return rec.classify_cluster(rec.summarize_history(rows, cfg["permanent_error_markers"]).get(cluster), cfg, NOW)


def test_no_expert_run_is_backlog():
    assert _state([]) == rec.STATE_UNEVALUATED


def test_schema_invalid_only_is_retryable_backlog_not_evaluated():
    assert _state([_run("u1", "schema_invalid", days_ago=3)]) == rec.STATE_RETRYABLE


@pytest.mark.parametrize("decision_row", [_run("u1", "success", 1)])
def test_successful_decision_is_not_backlog(decision_row):
    assert _state([decision_row]) == rec.STATE_EVALUATED


def test_success_after_failures_is_evaluated():
    assert _state([_run("u1", "schema_invalid", 5), _run("u1", "success", 1)]) == rec.STATE_EVALUATED


# ─── Queue ────────────────────────────────────────────────────
def test_recent_comes_first_and_backlog_has_reserved_slots():
    specs = [(1, "B", 10, 2), (2, "B", 11, 2), (3, "B", 12, 2), (4, "B", 13, 2),  # recent 4件（上限3）
             (5, "S", 30, 200), (6, "A", 25, 200), (7, "B", 20, 200)]
    client, urls = _world(specs)
    plan = _plan(client, urls, recent_ids=["u1", "u2", "u3", "u4"])
    assert plan["queue"][:3] == plan["recent_batch"] and len(plan["recent_batch"]) == 3
    assert len(plan["backlog_batch"]) == 2   # Recentが多い日でもBacklogの枠は確保される
    assert plan["queue"][3:] == plan["backlog_batch"]


def test_backlog_priority_is_rank_then_score_then_oldest():
    specs = [(1, "B", 99, 300), (2, "A", 10, 300), (3, "S", 5, 300), (4, "S", 5, 400), (5, "S", 9, 100)]
    client, urls = _world(specs)
    cfg = {**CFG, "backlog_limit": 5}
    plan = _plan(client, urls, cfg=cfg)
    # S内ではスコア降順(u5=9, 次に同点のu4/u3は古い順=u4が先)、その後A、B
    assert plan["backlog_batch"] == ["u5", "u4", "u3", "u2", "u1"]


def test_backlog_is_not_cut_by_since_days():
    client, urls = _world([(1, "S", 30, 1000)])  # 1000日前でも対象
    plan = _plan(client, urls)
    assert plan["backlog_batch"] == ["u1"]


def test_limits_are_respected_and_backlog_excluded_without_flag():
    client, urls = _world([(n, "A", 20 + n, 100) for n in range(1, 10)])
    assert len(_plan(client, urls, cfg=NO_SPILL)["backlog_batch"]) == CFG["backlog_limit"]
    assert _plan(client, urls, include_backlog=False)["backlog_batch"] == []
    assert len(_plan(client, urls, limit=1)["queue"]) == 1


def test_unused_recent_slots_spill_over_to_backlog():
    client, urls = _world([(n, "A", 20 + n, 100) for n in range(1, 10)])
    plan = _plan(client, urls, recent_ids=[])  # Recentが0件 → 枠3がBacklogへ
    assert len(plan["backlog_batch"]) == CFG["backlog_limit"] + CFG["recent_limit"]
    no_spill = _plan(client, urls, recent_ids=[], cfg={**CFG, "spillover_unused_recent_slots": False})
    assert len(no_spill["backlog_batch"]) == CFG["backlog_limit"]


def test_recent_clusters_are_not_duplicated_in_backlog():
    client, urls = _world([(1, "S", 30, 2), (2, "S", 29, 200)])
    plan = _plan(client, urls, recent_ids=["u1"])
    assert plan["recent_batch"] == ["u1"] and plan["backlog_batch"] == ["u2"]


def test_rank_c_and_d_never_enter_queue():
    client, urls = _world([(1, "C", 12, 100), (2, "D", 5, 100), (3, "B", 18, 100)])
    assert _plan(client, urls)["backlog_batch"] == ["u3"]


# ─── Idempotency ──────────────────────────────────────────────
@pytest.mark.parametrize("status_decision", ["publish_candidate", "watch_or_archive", "not_selected"])
def test_successfully_evaluated_clusters_are_not_requeued_and_rerun_is_stable(status_decision):
    client, urls = _world([(1, "S", 30, 100), (2, "S", 29, 100)],
                          runs=[{**_run("u1", "success", 5), "output_json": {"decision": status_decision}}])
    first = _plan(client, urls)
    assert first["backlog_batch"] == ["u2"]
    assert _plan(client, urls)["queue"] == first["queue"]   # 同じ状態で再計画しても同じ（増殖しない）


# ─── Failure / Retry ──────────────────────────────────────────
def test_cooldown_blocks_immediate_retry_and_retry_limit_needs_review():
    cooldown = [_run("u1", "error", days_ago=0.2, error="APIConnectionError")]
    assert _state(cooldown) == rec.STATE_COOLDOWN
    exhausted = [_run("u1", "schema_invalid", 9), _run("u1", "schema_invalid", 8), _run("u1", "error", 7)]
    assert _state(exhausted) == rec.STATE_NEEDS_REVIEW


def test_permanent_error_is_not_retried():
    rows = [_run("u1", "error", 3, error="BadRequestError: content_filter ResponsibleAIPolicyViolation")]
    assert _state(rows) == rec.STATE_NEEDS_REVIEW


def test_needs_review_and_cooldown_are_backlog_but_not_selected():
    client, urls = _world([(1, "S", 30, 100), (2, "S", 29, 100), (3, "S", 28, 100)],
                          runs=[_run("u1", "error", 0.1, "timeout"),
                                _run("u2", "schema_invalid", 9), _run("u2", "schema_invalid", 8), _run("u2", "schema_invalid", 7)])
    plan = _plan(client, urls)
    assert plan["backlog_batch"] == ["u3"]
    assert plan["backlog"]["total"] == 3 and plan["backlog"]["by_state"][rec.STATE_NEEDS_REVIEW] == 1
    assert plan["backlog"]["by_state"][rec.STATE_COOLDOWN] == 1


def test_schema_invalid_cluster_is_retried_after_cooldown_not_dropped():
    client, urls = _world([(1, "A", 20, 100)], runs=[_run("u1", "schema_invalid", days_ago=3)])
    assert _plan(client, urls)["backlog_batch"] == ["u1"]


# ─── Dry-run ──────────────────────────────────────────────────
class _WriteSpy(FakeSupabaseClient):
    def insert(self, *a, **k):
        raise AssertionError("dry-runでDB書き込みが行われた")

    update = delete = insert


def test_dry_run_plan_works_through_read_only_client_and_never_writes():
    client, urls = _world([(n, "A", 20 + n, 100) for n in range(1, 6)])
    spy = _WriteSpy(client.tables)
    ro = rec.ReadOnlyClient(spy)
    plan = _plan(ro, urls)
    assert plan["queue"] and not spy.inserted
    with pytest.raises(RuntimeError):
        ro.insert("expert_runs", [{}])
    text = rec.format_plan(plan, rec.estimate(plan))
    assert "Backlog合計" in text and "list_weekly_picks" in text   # 週次メール混入の注意が出る


def test_estimate_scales_with_batch_and_marks_basis():
    client, urls = _world([(n, "A", 20 + n, 100) for n in range(1, 6)])
    plan = _plan(client, urls, cfg=NO_SPILL)
    assert rec.estimate(plan)["llm_calls"] == 2 and rec.estimate(plan, batch_size=2)["llm_calls"] == 1
    assert "概算" in rec.estimate(plan)["basis"]


# ─── 状態判定 ───────────────────────────────────────────────────
def test_status_unknown_when_no_history():
    client, _ = _world([])
    assert rec.get_selector_status(client, CFG, now=NOW)["state"] == "Unknown"


def test_status_stale_when_last_success_is_old():
    client, _ = _world([], runs=[_run("u1", "success", days_ago=20)])
    assert rec.get_selector_status(client, CFG, now=NOW)["state"] == "Selector stale"


def test_status_failed_when_recent_attempts_all_failed():
    client, _ = _world([], runs=[_run("u1", "success", 10), _run("u2", "error", 0.2), _run("u3", "schema_invalid", 0.3)])
    assert rec.get_selector_status(client, CFG, now=NOW)["state"] == "Failed"


def test_status_backlog_growing_and_healthy():
    runs = [_run("u9", "success", days_ago=1)]
    client, urls = _world([(n, "A", 20, 10) for n in range(1, 5)], runs=runs)   # 10日前の4件
    plan = _plan(client, urls)
    growing = rec.get_selector_status(client, {**CFG, "backlog_warn_total": 3}, now=NOW, plan=plan)
    assert growing["state"] == "Backlog growing"
    assert rec.get_selector_status(client, CFG, now=NOW, plan=plan)["state"] == "Healthy"
    old_client, old_urls = _world([(1, "A", 20, 100)], runs=runs)               # 最古が基準(90日)超
    old = rec.get_selector_status(old_client, CFG, now=NOW, plan=_plan(old_client, old_urls))
    assert old["state"] == "Backlog growing" and old["oldest_backlog_age_days"] >= 99


# ─── 二重実行防止 / 記録 ───────────────────────────────────────────
def test_lock_blocks_second_run_and_stale_lock_is_overwritten(tmp_path):
    lock = tmp_path / "selector_recovery.lock"
    assert rec.acquire_lock(8, lock) is True
    assert rec.acquire_lock(8, lock) is False
    import os, time
    old = time.time() - 9 * 3600
    os.utime(lock, (old, old))
    assert rec.acquire_lock(8, lock) is True
    rec.release_lock(lock)
    assert not lock.exists()


def test_run_log_records_funnel_with_outcomes(tmp_path):
    client, urls = _world([(1, "S", 30, 100)])
    path = rec.write_run_log(_plan(client, urls), {"success": 1, "schema_invalid": 0, "error": 0}, tmp_path)
    import json
    funnel = json.loads(path.read_text(encoding="utf-8"))["funnel"]
    assert funnel["submitted_to_selector"] == 1 and funnel["selector_success"] == 1


def test_config_override_only_accepts_known_keys():
    cfg = rec.get_config({"sustainability_expert": {"selector_recovery": {"backlog_limit": 7, "unknown": 1}}})
    assert cfg["backlog_limit"] == 7 and "unknown" not in cfg
