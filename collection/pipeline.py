"""日次工程を順番に実行する仕組み（共通クロール部分）。

- 各工程は独立したサブプロセスで実行し、1工程が失敗/タイムアウトしても後続の工程は続ける
- ロックファイルで多重起動を防ぐ（古いロックは異常終了の名残とみなして上書き）
- 失敗の通知は、呼び出し側（アプリ）が notify として渡す。ここではメール等に依存しない

サステナの run_daily.py は、このモジュールの acquire_lock / release_lock / run_step を使う。
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MAX_LOCK_AGE_HOURS = 6   # これを超えて残っているロックは前回異常終了とみなし上書きする


def acquire_lock(lock_path: Path, max_age_hours: float = DEFAULT_MAX_LOCK_AGE_HOURS) -> bool:
    lock_path = Path(lock_path)
    if lock_path.exists():
        age_hours = (time.time() - lock_path.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            print(f"ロックファイルが{age_hours:.1f}時間前から存在します。多重実行防止のため終了します"
                  f"（{lock_path}）。前回の実行が異常終了した場合は手動で削除してください。")
            return False
        print(f"ロックファイルは{age_hours:.1f}時間前のものです（{max_age_hours}時間超）。"
              "前回実行が異常終了したとみなし上書きします。")
    lock_path.write_text(f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()}")
    return True


def release_lock(lock_path: Path) -> None:
    try:
        Path(lock_path).unlink()
    except FileNotFoundError:
        pass


def run_step(name: str, args: list, timeout_sec: int, *, base: Path, python: Path, log_dir: Path) -> dict:
    """1工程をサブプロセスで実行し、結果の辞書を返す。base は実行時の作業フォルダ（アプリのルート）。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{ts}_{name}.log"
    cmd = [str(python), *args]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    print(f"[{name}] 開始: {' '.join(args)}", flush=True)
    started = time.monotonic()
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            result = subprocess.run(cmd, cwd=base, stdout=f, stderr=subprocess.STDOUT,
                                    timeout=timeout_sec, env=env)
        elapsed = time.monotonic() - started
        ok = result.returncode == 0
        print(f"[{name}] {'完了' if ok else '失敗'}（{elapsed:.0f}秒、returncode={result.returncode}） "
              f"ログ: {log_path}")
        error = None if ok else f"returncode={result.returncode}"
        return {"name": name, "ok": ok, "elapsed_sec": elapsed, "log_path": str(log_path), "error": error}
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        error = f"タイムアウト（{timeout_sec}秒）"
        print(f"[{name}] {error} ログ: {log_path}")
        return {"name": name, "ok": False, "elapsed_sec": elapsed, "log_path": str(log_path), "error": error}
    except Exception as e:
        elapsed = time.monotonic() - started
        error = f"{type(e).__name__}: {e}"
        print(f"[{name}] 予期しないエラー: {error}")
        return {"name": name, "ok": False, "elapsed_sec": elapsed, "log_path": str(log_path), "error": error}


def run_steps(steps: list, skip: set, *, base: Path, python: Path, log_dir: Path) -> list:
    """steps を順番に実行する。skip に含まれる工程名は飛ばす。結果の辞書のリストを返す。"""
    results = []
    for name, step_args, timeout_sec in steps:
        if name in skip:
            print(f"[{name}] スキップ指定によりスキップ")
            continue
        results.append(run_step(name, step_args, timeout_sec, base=base, python=python, log_dir=log_dir))
    return results


def print_summary(results: list) -> None:
    print("─" * 40)
    for r in results:
        print(f"{r['name']}: {'OK' if r['ok'] else 'NG'}（{r['elapsed_sec']:.0f}秒）")


def main(steps: list, *, base: Path, lock_path: Path, description: str = "共通クロール",
         notify=None, argv=None, max_lock_age_hours: float = DEFAULT_MAX_LOCK_AGE_HOURS) -> int:
    """工程の一覧を実行する入口。`--skip-<工程名>`（_ は - に読み替え）で工程を飛ばせる。
    notify(results) は、失敗した工程があるときだけ呼ぶ（アプリ側が通知方法を決める）。終了コードを返す。"""
    parser = argparse.ArgumentParser(description=description)
    for name, _, _ in steps:
        parser.add_argument(f"--skip-{name.replace('_', '-')}", action="store_true", dest=f"skip_{name}")
    args = parser.parse_args(argv)
    skip = {name for name, _, _ in steps if getattr(args, f"skip_{name}")}

    if not acquire_lock(lock_path, max_lock_age_hours):
        return 1
    try:
        results = run_steps(steps, skip, base=Path(base), python=Path(sys.executable),
                            log_dir=Path(base) / "logs" / "daily")
    finally:
        release_lock(lock_path)

    print_summary(results)
    if notify and any(not r["ok"] for r in results):
        notify(results)
    return 0
