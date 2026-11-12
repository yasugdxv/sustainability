"""日次工程の仕組み(collection/steps.py, collection/pipeline.py)と run_daily / run_collection のテスト。
外部(DB・Web・AI)には触れない。実際にサブプロセスを動かすのは、python -c の軽い命令だけ。"""
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from collection import pipeline
from collection.steps import COLLECTION_STEPS

ROOT = Path(__file__).resolve().parents[1]

# 従来の run_daily.py の工程名（--skip-<工程名> を使う WebJob があるため、変えてはいけない）
ORIGINAL_STEP_NAMES = {
    "crawl", "reference_list", "api_articles", "filter_coverage_check", "crawl_health_check", "analyze",
    "translate_prefetch", "competitor_crawl", "selector", "warm_dashboard_cache",
}


# ─── 工程の一覧 ────────────────────────────────────────────────

def test_collection_steps_modules_are_importable():
    """各工程が `-m collection.xxx` で呼べる（モジュールが存在する）。"""
    for name, args, _timeout in COLLECTION_STEPS:
        assert args[0] == "-m" and args[1].startswith("collection."), name
        assert importlib.util.find_spec(args[1]) is not None, args[1]


def test_run_daily_steps_keep_names_and_compose_collection_first():
    import run_daily
    names = [n for n, _, _ in run_daily.STEPS]
    assert set(names) == ORIGINAL_STEP_NAMES and len(names) == len(set(names))
    assert run_daily.STEPS[:len(COLLECTION_STEPS)] == COLLECTION_STEPS       # 共通の工程が先頭
    # サステナ独自の工程の相対的な順序は従来どおり
    assert names[len(COLLECTION_STEPS):] == [
        "filter_coverage_check", "analyze", "translate_prefetch", "competitor_crawl", "selector", "warm_dashboard_cache"]


def test_webjob_skip_flags_are_all_valid_step_names():
    """既存の WebJob の run.sh が指定している --skip-xxx が、すべて実在する工程名か。"""
    import run_daily
    valid = {n.replace("_", "-") for n, _, _ in run_daily.STEPS}
    checked = 0
    for sh in (ROOT / "App_Data" / "jobs").rglob("run.sh"):
        text = sh.read_text(encoding="utf-8")
        if "run_daily.py" not in text:
            continue
        for flag in re.findall(r"--skip-([a-z0-9-]+)", text):
            assert flag in valid, f"{sh.parent.name}: 存在しない工程 --skip-{flag}"
            checked += 1
    assert checked > 0


# ─── ロック ───────────────────────────────────────────────────

def test_lock_blocks_second_run_and_overrides_stale(tmp_path):
    p = tmp_path / "x.lock"
    assert pipeline.acquire_lock(p, 6) is True
    assert pipeline.acquire_lock(p, 6) is False
    old = time.time() - 7 * 3600
    os.utime(p, (old, old))
    assert pipeline.acquire_lock(p, 6) is True        # 古いロックは上書き
    pipeline.release_lock(p)
    assert not p.exists()
    pipeline.release_lock(p)                          # 無くてもエラーにならない


def test_run_daily_lock_wrappers_use_its_own_lock_path(tmp_path, monkeypatch):
    import run_daily
    monkeypatch.setattr(run_daily, "LOCK_PATH", tmp_path / "rd.lock")
    assert run_daily.acquire_lock() is True and (tmp_path / "rd.lock").exists()
    assert run_daily.acquire_lock() is False
    run_daily.release_lock()
    assert not (tmp_path / "rd.lock").exists()


# ─── 1工程の実行 ─────────────────────────────────────────────

def _step(tmp_path, args, timeout=30):
    return pipeline.run_step("t", args, timeout, base=tmp_path, python=Path(sys.executable), log_dir=tmp_path / "logs")


def test_run_step_success_failure_and_timeout(tmp_path):
    ok = _step(tmp_path, ["-c", "print('こんにちは')"])
    assert ok["ok"] and ok["error"] is None
    assert "こんにちは" in Path(ok["log_path"]).read_text(encoding="utf-8")

    ng = _step(tmp_path, ["-c", "import sys; sys.exit(3)"])
    assert not ng["ok"] and ng["error"] == "returncode=3"

    to = _step(tmp_path, ["-c", "import time; time.sleep(5)"], timeout=1)
    assert not to["ok"] and "タイムアウト" in to["error"]


def test_run_steps_skips_and_continues_after_failure(tmp_path):
    steps = [("a", ["-c", "pass"], 30), ("b", ["-c", "import sys; sys.exit(1)"], 30), ("c", ["-c", "pass"], 30)]
    r = pipeline.run_steps(steps, {"a"}, base=tmp_path, python=Path(sys.executable), log_dir=tmp_path / "logs")
    assert [x["name"] for x in r] == ["b", "c"]              # a はスキップ
    assert [x["ok"] for x in r] == [False, True]             # b が失敗しても c は実行


# ─── 入口 ───────────────────────────────────────────────────

def test_main_notifies_only_on_failure_and_releases_lock(tmp_path):
    notified = []
    ok_steps = [("a", ["-c", "pass"], 30)]
    ng_steps = [("a", ["-c", "import sys; sys.exit(1)"], 30)]
    lock = tmp_path / "m.lock"
    assert pipeline.main(ok_steps, base=tmp_path, lock_path=lock, notify=notified.append, argv=[]) == 0
    assert notified == [] and not lock.exists()
    assert pipeline.main(ng_steps, base=tmp_path, lock_path=lock, notify=notified.append, argv=[]) == 0
    assert len(notified) == 1 and notified[0][0]["ok"] is False and not lock.exists()


def test_main_returns_1_when_locked_and_supports_skip_flag(tmp_path):
    steps = [("my_step", ["-c", "pass"], 30)]
    lock = tmp_path / "m.lock"
    lock.write_text("busy")
    assert pipeline.main(steps, base=tmp_path, lock_path=lock, argv=[]) == 1
    assert lock.exists()                                      # 他の実行のロックは消さない
    lock.unlink()
    assert pipeline.main(steps, base=tmp_path, lock_path=lock, argv=["--skip-my-step"]) == 0   # _ は - に読み替え
    assert not list((tmp_path / "logs" / "daily").glob("*_my_step.log"))                        # スキップしたので実行していない


def test_run_collection_entry_and_module_run_start_without_import_errors():
    """`python run_collection.py --help` と `python -m collection.crawl_health_check --help` が起動する。"""
    for argv in (["run_collection.py", "--help"], ["-m", "collection.crawl_health_check", "--help"]):
        r = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0, (argv, (r.stdout + r.stderr)[-500:])
        assert "Error" not in r.stderr, (argv, r.stderr[-500:])
