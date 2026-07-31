"""
world_monitor_dashboard 起動・スモークテストスクリプト（APIバックエンドのみ）
使い方: python projects/world_monitor_dashboard/.claude/skills/run-world-monitor-dashboard/smoke.py

world_monitor_dashboardの実体は api_server.py（FastAPI）。
フロントエンド(eco-digest-spark)はこのスクリプトでは起動しない（npm run devで別途確認する）。
/api/categories への応答を健全性確認とする。
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parents[5]  # c:/Users/.../python/
APP_DIR = REPO_ROOT / "projects" / "world_monitor_dashboard"
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
PORT = 8500
URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = f"{URL}/api/categories"


def wait_for_ready(timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def main():
    print(f"[smoke] Starting api_server.py: port={PORT}")
    env = dict(os.environ, API_PORT=str(PORT))
    proc = subprocess.Popen(
        [str(PYTHON), "api_server.py"],
        cwd=str(APP_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        print("[smoke] Waiting for ready (up to 30s)...")
        ok = wait_for_ready(30)
        if not ok:
            print("[smoke] FAIL: timeout — api_server.py did not respond")
            sys.exit(1)

        with urllib.request.urlopen(HEALTH_URL, timeout=5) as r:
            body = r.read(200).decode(errors="replace")
        assert body.strip().startswith("["), f"Unexpected response: {body[:100]}"
        print(f"[smoke] API OK: {HEALTH_URL}")
        print("[smoke] PASS")

    finally:
        proc.terminate()
        proc.wait()
        print("[smoke] Process terminated")


if __name__ == "__main__":
    main()
