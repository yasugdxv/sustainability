"""2026-10-26追加: 日次ジョブの追い込み実行(--catchup)・ロックの引き継ぎ・処理率の確認のテスト。

背景: 10/24は、日次ジョブの開始5分後にAzure側のコンテナ入れ替えでプロセスごと強制終了され、
その日の分析まで丸ごと止まった。止まった日は、同じUTC日のうちに、続きから再開できるようにする。"""
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import run_daily as rd  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """ロック・ログ・/procを、テスト用の場所へ差し替える"""
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(rd, "_PROC", proc)
    monkeypatch.setattr(rd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(rd, "LOCK_PATH", tmp_path / "run.lock")
    monkeypatch.setattr(rd, "_lock_suffix", "test")
    return proc


def _fake_process(proc: Path, pid: int, cmdline: str):
    (proc / str(pid)).mkdir()
    (proc / str(pid) / "cmdline").write_bytes(cmdline.encode().replace(b" ", b"\0"))


# ─── ロック ───────────────────────────────────────────────────
def test_lock_holder_alive_true_false_unknown(env, tmp_path, monkeypatch):
    _fake_process(env, 100, "python run_daily.py --skip-selector")
    _fake_process(env, 200, "python other_script.py")
    assert rd._lock_holder_alive("pid=100 started_at=x") is True
    assert rd._lock_holder_alive("pid=200 started_at=x") is False      # pidが別のプログラムに再利用された
    assert rd._lock_holder_alive("pid=300 started_at=x") is False      # もうプロセスが無い
    assert rd._lock_holder_alive("壊れた内容") is None
    monkeypatch.setattr(rd, "_PROC", tmp_path / "no_such_proc_dir")      # Windowsなど/procが無い環境
    assert rd._lock_holder_alive("pid=100 started_at=x") is None


def test_lock_left_by_killed_process_is_taken_over_immediately(env):
    rd.LOCK_PATH.write_text("pid=999 started_at=x")                       # 持ち主(pid 999)はもう居ない・まだ新しいロック
    assert rd.acquire_lock() is True
    assert f"pid={os.getpid()}" in rd.LOCK_PATH.read_text()


def test_lock_held_by_running_process_blocks_even_if_older_than_six_hours(env):
    _fake_process(env, 100, "python run_daily.py")
    rd.LOCK_PATH.write_text("pid=100 started_at=x")
    old = time.time() - 9 * 3600                                          # 9時間かかる実行は、実際にある
    os.utime(rd.LOCK_PATH, (old, old))
    assert rd.acquire_lock() is False


def test_lock_without_pid_info_falls_back_to_age(env):
    rd.LOCK_PATH.write_text("pid情報なし")
    assert rd.acquire_lock() is False                                     # 新しい → 多重起動とみなす
    old = time.time() - 7 * 3600
    os.utime(rd.LOCK_PATH, (old, old))
    assert rd.acquire_lock() is True                                      # 6時間超 → 異常終了とみなして上書き


# ─── 進捗の記録 ─────────────────────────────────────────────────
def test_progress_roundtrip_and_missing_file(env):
    assert rd.load_progress("2026-10-23") == set()
    rd.mark_step_done("2026-10-23", "crawl")
    rd.mark_step_done("2026-10-23", "analyze")
    assert rd.load_progress("2026-10-23") == {"crawl", "analyze"}
    assert rd.load_progress("2026-10-24") == set()                        # 日が違えば、別の記録


def test_progress_file_is_separate_per_job_name(env):
    rd.mark_step_done("2026-10-23", "crawl")
    rd._lock_suffix = "other-job"
    assert rd.load_progress("2026-10-23") == set()


# ─── main(): 通常実行と追い込み実行 ─────────────────────────────────
class _FixedDatetime(datetime):
    fixed = datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed if tz else cls.fixed.replace(tzinfo=None)


@pytest.fixture
def pipeline(env, monkeypatch):
    """ステップの中身を差し替えて、どのステップが実行されたかだけを記録する"""
    steps = [("crawl", ["article_crawler.py"], 100), ("analyze", ["article_analyzer.py"], 100),
             ("competitor_crawl", ["competitor_crawler.py"], 100), ("selector", ["x.py"], 100)]
    called = []
    state = {"drained": True}
    monkeypatch.setattr(rd, "STEPS", steps)
    monkeypatch.setattr(rd, "datetime", _FixedDatetime)
    monkeypatch.setattr(rd, "_set_job_name", lambda name: monkeypatch.setattr(rd, "_lock_suffix", "test"))
    monkeypatch.setattr(rd, "check_crawl_coverage", lambda: {"name": "crawl_coverage", "ok": True, "elapsed_sec": 0,
                                                              "log_path": "-", "error": None})
    monkeypatch.setattr(rd, "notify_failures", lambda results: None)

    def fake_crawl(name, timeout):
        called.append(name)
        return {"name": name, "ok": True, "elapsed_sec": 0, "log_path": "-", "error": None, "drained": state["drained"]}

    def fake_step(name, args, timeout):
        called.append(name)
        return {"name": name, "ok": True, "elapsed_sec": 0, "log_path": "-", "error": None}

    monkeypatch.setattr(rd, "run_crawl_step_with_resume", fake_crawl)
    monkeypatch.setattr(rd, "run_step", fake_step)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["run_daily.py", *argv])
        called.clear()
        rd.main()
        return list(called)
    return run, state


JOB_SKIPS = ["--skip-competitor-crawl", "--skip-selector"]


def test_normal_run_records_progress_and_catchup_then_does_nothing(pipeline):
    run, _ = pipeline
    assert run(*JOB_SKIPS) == ["crawl", "analyze"]
    assert rd.load_progress("2026-10-23") == {"crawl", "analyze"}
    assert run("--catchup", *JOB_SKIPS) == []                             # 完了済みの日は、何もしない


def test_catchup_resumes_only_the_unfinished_steps(pipeline):
    run, state = pipeline
    rd.mark_step_done("2026-10-23", "crawl")                              # crawlまで終わって、止まった日
    assert run("--catchup", *JOB_SKIPS) == ["analyze"]


def test_catchup_reruns_crawl_if_it_did_not_drain(pipeline):
    run, state = pipeline
    state["drained"] = False                                              # 時間切れ等で、残りが0件にならなかった
    assert run(*JOB_SKIPS) == ["crawl", "analyze"]
    assert rd.load_progress("2026-10-23") == {"analyze"}                  # crawlは「完了」にしない
    state["drained"] = True
    assert run("--catchup", *JOB_SKIPS) == ["crawl"]                      # 続きのクロールだけ実行
    assert run("--catchup", *JOB_SKIPS) == []


def test_catchup_does_nothing_before_the_utc_start_hour(pipeline, monkeypatch):
    run, _ = pipeline
    monkeypatch.setattr(_FixedDatetime, "fixed", datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc))   # 日付が変わった後
    assert run("--catchup", *JOB_SKIPS) == []                             # 全対象の再クロールを避ける


def test_catchup_exits_quietly_when_main_job_holds_the_lock(pipeline, env):
    run, _ = pipeline
    _fake_process(env, 100, "python run_daily.py")
    rd.LOCK_PATH.write_text("pid=100 started_at=x")
    with pytest.raises(SystemExit) as e:
        run("--catchup", *JOB_SKIPS)
    assert e.value.code == 0                                              # 本体が動いている間は、何もせず正常終了（WebJobを失敗扱いにしない）


def test_normal_run_still_exits_with_error_when_locked(pipeline, env):
    run, _ = pipeline
    _fake_process(env, 100, "python run_daily.py")
    rd.LOCK_PATH.write_text("pid=100 started_at=x")
    with pytest.raises(SystemExit) as e:
        run(*JOB_SKIPS)
    assert e.value.code == 1


# ─── 処理率の確認 ──────────────────────────────────────────────────
def _patch_crawler(monkeypatch, active, attempted):
    import article_crawler as ac

    class Client:
        def select(self, table, params):
            return [{"crawl_target_id": i} for i in active]

    monkeypatch.setattr(ac, "load_config", lambda: {})
    monkeypatch.setattr(ac, "SupabaseClient", lambda config: Client())
    monkeypatch.setattr(ac, "_get_today_attempted_target_ids", lambda client: set(attempted))


def test_coverage_ok_when_nearly_all_targets_were_processed(monkeypatch):
    _patch_crawler(monkeypatch, active=range(100), attempted=list(range(95)) + ["廃止済みの対象"])
    r = rd.check_crawl_coverage()
    assert r["ok"] is True and r["error"] is None                         # 有効な対象だけを数える（廃止済みは除く）


def test_coverage_alerts_when_the_run_stopped_midway(monkeypatch):
    _patch_crawler(monkeypatch, active=range(512), attempted=range(13))   # 10/24: 13対象で停止
    r = rd.check_crawl_coverage()
    assert r["ok"] is False and "13/512" in r["error"] and "途中停止" in r["error"]


def test_coverage_check_failure_is_not_silent(monkeypatch):
    import article_crawler as ac

    def boom(config):
        raise RuntimeError("DB接続不可")
    monkeypatch.setattr(ac, "load_config", lambda: {})
    monkeypatch.setattr(ac, "SupabaseClient", boom)
    r = rd.check_crawl_coverage()
    assert r["ok"] is False and "確認に失敗" in r["error"]
