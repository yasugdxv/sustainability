"""
日次パイプライン オーケストレーションスクリプト

情報収集〜LLM分析〜選定判定までを1本にまとめて日次実行するためのスクリプト。
このスクリプト自体はまだ自動実行の仕組み（Windowsタスクスケジューラ／cron等）へ
登録されていない（本番のホスティング先が未確定なため）。まずはこのスクリプトを
手動またはお好みのスケジューラから起動できる状態にする。

実行順序（各ステップは独立したサブプロセスとして実行し、1ステップが失敗/タイムアウト
しても後続ステップは継続する。article_crawler.pyのprocess_target()が既に1ソース単位で
同じ方針を取っているため、パイプライン全体でもこれを踏襲する）:
    1. article_crawler.py                  通常クロール（RSS/HTML/ブラウザ操作）
    2. reference_list_monitor.py           参照リスト差分検知（UN_SC/OFAC_SDN/eCFR/UK制裁）
    3. api_article_crawler.py              規制系記事API（Federal Register/openFDA/UK FSA/
                                            EU Safety Gate/RASFF）
    4. check_filter_keyword_coverage.py    フィルタ語彙のtag_referenceカバレッジ検証
                                            （holesがあればステップ失敗として通知。
                                            記事は既にスタブ保存方式のため記事は失われない）
    5. article_analyzer.py                 LLMタグ付け・重要度判定（全件対象、並列5）
    6. competitor_crawler.py               競合サイトクロール
    7. sustainability_article_selector.py  記事選定（テーマ別上位N件をLLM判定、expert_runsへ記録。
                                            週次メールのlist_weekly_picks()が参照する判定結果を
                                            日次で最新化しておく）

ジョブ重複実行防止: ロックファイル(run_daily.lock)を使用する。既存ロックが
MAX_LOCK_AGE_HOURS以内なら多重起動とみなして即終了する（前回実行が正常終了していれば
ロックはfinallyで必ず削除されるため、残っているのは異常終了の疑いが強い）。

失敗通知: 1ステップでも失敗/タイムアウトした場合、weekly_email_report.send_email()を
再利用してメール通知する（config.jsonのemail.smtp_hostが未設定なら、既存の仕様通り
cache/へのプレビューHTML保存にフォールバックする＝実送信は行われない）。

使い方:
    python run_daily.py                     # 全ステップ実行
    python run_daily.py --skip-selector      # 選定ステップだけ除外（他ステップも同様に--skip-<step>）
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
# 現在このスクリプトを実行しているPythonインタプリタをそのまま子プロセスにも使う
# （Windows開発環境の.venvとAzure App Service(Linux)のantenvでパス構造が異なるため、
# 環境依存のパスをハードコードせずsys.executableで解決する）
PYTHON = Path(sys.executable)
LOG_DIR = BASE / "logs" / "daily"
# Azure WebJobsはジョブごとにWEBJOBS_NAME環境変数を自動設定する。これを使ってロックファイルを
# ジョブ単位で分離し、実行するステップの異なる複数WebJob（記事クロール系/競合クロール系）が
# お互いの実行をブロックしないようにする（手動/ローカル実行時はWEBJOBS_NAME未設定のため共通名を使う）。
_lock_suffix = os.environ.get("WEBJOBS_NAME", "default")
LOCK_PATH = BASE / f"run_daily_{_lock_suffix}.lock"
MAX_LOCK_AGE_HOURS = 6  # これを超えて残っているロックは前回異常終了とみなし上書きする

# (ステップ名, コマンド引数, タイムアウト秒)
STEPS = [
    ("crawl", ["article_crawler.py"], 3600),
    ("reference_list", ["reference_list_monitor.py"], 900),
    ("api_articles", ["api_article_crawler.py"], 900),
    ("filter_coverage_check", ["check_filter_keyword_coverage.py"], 300),
    ("analyze", ["article_analyzer.py", "9999", "5"], 5400),
    ("competitor_crawl", ["competitor_crawler.py"], 1800),
    ("selector", ["sustainability_article_selector.py", "--top-n-per-theme", "10"], 3600),
]


def acquire_lock() -> bool:
    if LOCK_PATH.exists():
        age_hours = (time.time() - LOCK_PATH.stat().st_mtime) / 3600
        if age_hours < MAX_LOCK_AGE_HOURS:
            print(f"ロックファイルが{age_hours:.1f}時間前から存在します。多重実行防止のため終了します"
                  f"（{LOCK_PATH}）。前回の実行が異常終了した場合は手動で削除してください。")
            return False
        print(f"ロックファイルは{age_hours:.1f}時間前のものです（{MAX_LOCK_AGE_HOURS}時間超）。"
              "前回実行が異常終了したとみなし上書きします。")
    LOCK_PATH.write_text(f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat()}")
    return True


def release_lock():
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def run_step(name: str, args: list, timeout_sec: int) -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{ts}_{name}.log"
    cmd = [str(PYTHON), *args]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    print(f"[{name}] 開始: {' '.join(args)}", flush=True)
    started = time.monotonic()
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            result = subprocess.run(cmd, cwd=BASE, stdout=f, stderr=subprocess.STDOUT,
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


def notify_failures(results: list) -> None:
    failed = [r for r in results if not r["ok"]]
    if not failed:
        return
    try:
        sys.path.insert(0, str(BASE))
        from config_utils import load_config
        import weekly_email_report as wer

        config = load_config()
        lines = [f"日次パイプラインで{len(failed)}件のステップが失敗しました"
                 f"（{datetime.now(timezone.utc).isoformat()}）。", ""]
        for r in results:
            status = "OK" if r["ok"] else f"NG（{r['error']}）"
            lines.append(f"- {r['name']}: {status}　{r['elapsed_sec']:.0f}秒　ログ: {r['log_path']}")
        body_text = "\n".join(lines)
        html = f"<pre style=\"font-family:monospace;white-space:pre-wrap;\">{wer._esc(body_text)}</pre>"
        subject = f"【日次パイプライン異常】{len(failed)}件のステップが失敗"
        send_result = wer.send_email(subject, html, config)
        print(f"失敗通知: {send_result}")
    except Exception as e:
        print(f"失敗通知の送信自体に失敗しました: {type(e).__name__}: {e}")


def main():
    parser = argparse.ArgumentParser(description="日次パイプライン オーケストレーション")
    for name, _, _ in STEPS:
        parser.add_argument(f"--skip-{name.replace('_', '-')}", action="store_true",
                             dest=f"skip_{name}")
    args = parser.parse_args()
    skip = {name for name, _, _ in STEPS if getattr(args, f"skip_{name}")}

    if not acquire_lock():
        sys.exit(1)

    results = []
    try:
        for name, step_args, timeout_sec in STEPS:
            if name in skip:
                print(f"[{name}] スキップ指定によりスキップ")
                continue
            results.append(run_step(name, step_args, timeout_sec))
    finally:
        release_lock()

    print("─" * 40)
    for r in results:
        print(f"{r['name']}: {'OK' if r['ok'] else 'NG'}（{r['elapsed_sec']:.0f}秒）")
    notify_failures(results)


if __name__ == "__main__":
    main()
