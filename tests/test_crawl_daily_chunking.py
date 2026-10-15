"""2026-10-15追加: 日次クロールバッチが原因不明のままプロセスごと停止する事象が
本番で続けて発生した(10/13は507件中282件、10/14は507件中190件で停止、Pythonの
例外もApp Serviceのメモリ/CPU張り付きも確認できず)ことへの対策のテスト。

対策: article_crawler.py(--resumeで本日試行済みtargetをスキップ) +
run_daily.py(CRAWL_CHUNK_SIZE件ずつのチャンクに分けて繰り返し起動し、あるチャンクが
落ちても次のチャンクが続きから再開する)。"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
import run_daily as rd  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


# ─── article_crawler._get_today_attempted_target_ids() ────────────────
def test_get_today_attempted_target_ids_includes_only_today(monkeypatch):
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_target_id": "t-today-1", "started_at": now.isoformat()},
        {"crawl_target_id": "t-today-2", "started_at": now.isoformat()},
        {"crawl_target_id": "t-yesterday", "started_at": yesterday.isoformat()},
    ]})

    ids = ac._get_today_attempted_target_ids(client)

    assert ids == {"t-today-1", "t-today-2"}


# ─── article_crawler.main(skip_today_attempted=True) ──────────────────
def _patch_main_dependencies(monkeypatch, client, targets):
    monkeypatch.setattr(ac, "load_config", lambda: {})
    monkeypatch.setattr(ac, "make_proxies", lambda config: {})
    monkeypatch.setattr(ac, "SupabaseClient", lambda config: client)
    monkeypatch.setattr(ac, "close_browser", lambda: None)
    monkeypatch.setattr(ac, "downgrade_bot_blocked_targets", lambda client, targets: None)
    client.tables["crawl_targets"] = targets


def _ok_summary(crawl_log_id="log-x"):
    return {
        "run_result": "更新なし", "items_detected": 0, "new_items": 0, "updated_items": 0,
        "extraction_failures": 0, "error_message": None, "bot_block_detections": 0,
        "crawl_log_id": crawl_log_id, "pending_escalations": [],
    }


def test_main_skip_today_attempted_excludes_already_logged_targets(monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_target_id": "t1", "started_at": now.isoformat()},
    ]})
    targets = [
        {"crawl_target_id": "t1", "crawl_method": "RSS", "target_name": "既に試行済み"},
        {"crawl_target_id": "t2", "crawl_method": "RSS", "target_name": "未試行"},
    ]
    _patch_main_dependencies(monkeypatch, client, targets)
    process_calls = []
    monkeypatch.setattr(ac, "process_target", lambda target, *a, **k: (
        process_calls.append(target["crawl_target_id"]), _ok_summary())[1])

    ac.main(methods=("RSS",), skip_today_attempted=True)

    assert process_calls == ["t2"]  # t1は本日試行済みのため除外される
    assert "本日試行済み1件を除外（残り1件）" in capsys.readouterr().out


def test_main_without_skip_flag_processes_all_targets_as_before(monkeypatch):
    """既存動作の非回帰: skip_today_attempted未指定(デフォルトFalse)なら
    crawl_logsの中身によらず全target処理する"""
    now = datetime.now(timezone.utc)
    client = FakeSupabaseClient({"crawl_logs": [
        {"crawl_target_id": "t1", "started_at": now.isoformat()},
    ]})
    targets = [
        {"crawl_target_id": "t1", "crawl_method": "RSS", "target_name": "A"},
        {"crawl_target_id": "t2", "crawl_method": "RSS", "target_name": "B"},
    ]
    _patch_main_dependencies(monkeypatch, client, targets)
    process_calls = []
    monkeypatch.setattr(ac, "process_target", lambda target, *a, **k: (
        process_calls.append(target["crawl_target_id"]), _ok_summary())[1])

    ac.main(methods=("RSS",))

    assert process_calls == ["t1", "t2"]


# ─── run_daily.run_crawl_step_with_resume() ────────────────────────────
def _fake_run_writing(lines: str, returncode: int = 0, raise_timeout: bool = False):
    def fake_run(cmd, cwd, stdout, stderr, timeout, env):
        stdout.write(lines)
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, returncode)
    return fake_run


def test_run_crawl_step_with_resume_stops_when_zero_remaining(monkeypatch, tmp_path):
    monkeypatch.setattr(rd, "LOG_DIR", tmp_path)
    calls = []
    responses = [
        "クロール対象: 100件（RSS/HTML/Zyte/JSON API）\n...省略...\n",
        "本日試行済み100件を除外（残り0件）\nクロール対象: 0件（RSS/HTML/Zyte/JSON API）\n",
    ]

    def fake_run(cmd, cwd, stdout, stderr, timeout, env):
        calls.append(cmd)
        stdout.write(responses[len(calls) - 1])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    result = rd.run_crawl_step_with_resume("crawl", overall_timeout_sec=21600)

    assert len(calls) == 2
    assert result["ok"] is True
    assert len(result["chunks"]) == 2


def test_run_crawl_step_with_resume_continues_after_chunk_crash(monkeypatch, tmp_path):
    """あるチャンクが異常終了(タイムアウト等)しても、残りtargetが無くなるまで
    次のチャンクがすぐ再開すること。全体のokは異常終了があった事実を残すためFalseのまま"""
    monkeypatch.setattr(rd, "LOG_DIR", tmp_path)
    calls = []

    def fake_run(cmd, cwd, stdout, stderr, timeout, env):
        calls.append(cmd)
        if len(calls) == 1:
            stdout.write("クロール対象: 100件（RSS/HTML/Zyte/JSON API）\n[1/100] ...\n")
            raise subprocess.TimeoutExpired(cmd, timeout)
        stdout.write("本日試行済み37件を除外（残り0件）\nクロール対象: 0件（RSS/HTML/Zyte/JSON API）\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    result = rd.run_crawl_step_with_resume("crawl", overall_timeout_sec=21600)

    assert len(calls) == 2  # 1回目クラッシュ→2回目で再開・完了
    assert result["ok"] is False  # クラッシュがあった事実は残す
    assert result["chunks"][0]["ok"] is False
    assert result["chunks"][1]["ok"] is True
    assert "タイムアウト" in result["error"]


def test_run_crawl_step_with_resume_stops_at_max_chunks(monkeypatch, tmp_path):
    """"クロール対象: 0件"が一度も出ない(＝ずっと何か残っている)場合でも、
    CRAWL_MAX_CHUNKSで必ず打ち切られること(無限ループ防止)"""
    monkeypatch.setattr(rd, "LOG_DIR", tmp_path)
    monkeypatch.setattr(rd, "CRAWL_MAX_CHUNKS", 3)
    calls = []

    def fake_run(cmd, cwd, stdout, stderr, timeout, env):
        calls.append(cmd)
        stdout.write("クロール対象: 100件（RSS/HTML/Zyte/JSON API）\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    result = rd.run_crawl_step_with_resume("crawl", overall_timeout_sec=21600)

    assert len(calls) == 3
    assert len(result["chunks"]) == 3


def test_run_crawl_step_with_resume_respects_overall_timeout_budget(monkeypatch, tmp_path):
    """合計経過時間がoverall_timeout_secを超えたら、途中でも新しいチャンクを開始しないこと"""
    monkeypatch.setattr(rd, "LOG_DIR", tmp_path)
    monkeypatch.setattr(rd, "CRAWL_MAX_CHUNKS", 100)
    calls = []

    def fake_run(cmd, cwd, stdout, stderr, timeout, env):
        calls.append(cmd)
        stdout.write("クロール対象: 100件（RSS/HTML/Zyte/JSON API）\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    result = rd.run_crawl_step_with_resume("crawl", overall_timeout_sec=0)

    assert calls == []  # 最初のチェックで既に予算切れのため1回も起動しない
    assert result["ok"] is False
    assert result["chunks"] == []
