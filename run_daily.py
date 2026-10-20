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
    8. warm_dashboard_cache.py              記事一覧ダッシュボードの共有キャッシュを先回りして温める
                                            （api_server.pyのユーザーリクエストが重いDB再取得を
                                            踏まないようにするため）

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

# 2026-10-15追加: "crawl"ステップは、原因不明のまま全507target中190〜282件で
# プロセスごと停止する事象が本番で続けて発生したため(Pythonの例外は一切残らず、
# App Serviceのメモリ/CPUメトリックも張り付いていない＝外部要因の疑いが強いが未特定)、
# 1回の巨大な実行ではなく下記のチャンク単位に分割して実行する(run_crawl_step_with_resume参照)。
CRAWL_CHUNK_SIZE = 100  # 1チャンクあたりの処理target数
CRAWL_CHUNK_TIMEOUT_SEC = 3600  # 1チャンクあたりの上限（100件なら通常はこれよりかなり短時間で終わる想定）
CRAWL_MAX_CHUNKS = 10  # 安全装置（507件 ÷ 100件 ≈ 6チャンクで足りる想定に余裕を持たせた上限）

# (ステップ名, コマンド引数, タイムアウト秒)
STEPS = [
    # 2026-10-20: 21600→25200秒。Zyte方式95対象をPass1で処理するようにしたため(TinyFish Fetch、
    # 1対象あたり約40秒の見込みで約1時間増)。上限に足りないと、DB順で後ろの対象が毎日取りこぼされる
    ("crawl", ["article_crawler.py"], 25200),
    # 2026-10-19追加: crawl(チャンク)が時間切れで止まると、Pass1で失敗した記事の再試行(Pass2)が
    # 実行されないまま失われていた。notesに残した再試行待ちを、時間予算つきで引き継ぐ。
    # 終わらなかった分は次回へ繰り越し、72時間を超えたものは失敗として確定する
    # 時間予算は件数から自動算出（30分〜2時間。article_crawler.compute_backlog_budget）。
    # ステップのタイムアウトは、その上限(7200秒)に後始末の余裕を足した値
    ("crawl_pass2_backlog", ["article_crawler.py", "--pass2-backlog"], 7800),
    ("reference_list", ["reference_list_monitor.py"], 900),
    ("api_articles", ["api_article_crawler.py"], 900),
    ("filter_coverage_check", ["check_filter_keyword_coverage.py"], 300),
    ("crawl_health_check", ["crawl_health_check.py"], 300),
    ("analyze", ["article_analyzer.py", "9999", "5"], 5400),
    ("competitor_crawl", ["competitor_crawler.py"], 5400),
    ("selector", ["sustainability_article_selector.py", "--top-n-per-theme", "10"], 3600),
    # 記事一覧ダッシュボード(api_server.py)の共有キャッシュを先回りして温める。
    # データが実際に更新されるのはこのバッチだけなので、ここで温めておけば
    # ユーザーの最初のアクセスが重いDB再取得を踏まずに済む(2026-09-29)
    ("warm_dashboard_cache", ["warm_dashboard_cache.py"], 300),
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


def run_crawl_step_with_resume(name: str, overall_timeout_sec: int) -> dict:
    """2026-10-15追加: "crawl"ステップ専用。CRAWL_CHUNK_SIZE件ずつの小さなチャンクに
    分けて`article_crawler.py <件数> --resume`を繰り返し起動する。--resumeは
    本日(UTC)既にcrawl_logsへ記録済みのtargetを自動的にスキップするため、
    あるチャンクが(原因不明のまま)落ちても、次のチャンクがその続きから再開できる。

    各チャンク終了後、そのチャンクのログに"クロール対象: 0件"が含まれていれば
    （article_crawler.py側が残りtarget数として出力する行）本日分は完了とみなし終了する。
    それ以外は、チャンク数の上限(CRAWL_MAX_CHUNKS)または合計経過時間の上限
    (overall_timeout_sec、従来の単発実行時と同じ6時間)に達するまで次のチャンクを続ける。

    戻り値のokは、全チャンクが正常終了(returncode=0)した場合のみTrue。
    途中のチャンクが異常終了しても最終的に全target処理が完了していればデータの欠落は
    無いが、原因不明の停止自体は見逃さないよう通知対象にするため、ここではFalseのままにする"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    overall_started = time.monotonic()
    chunk_results = []
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    for chunk_i in range(1, CRAWL_MAX_CHUNKS + 1):
        remaining_budget = overall_timeout_sec - (time.monotonic() - overall_started)
        if remaining_budget <= 0:
            print(f"[{name}] 合計タイムアウト（{overall_timeout_sec}秒）に達したため打ち切ります")
            break
        chunk_timeout = min(CRAWL_CHUNK_TIMEOUT_SEC, remaining_budget)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"{ts}_{name}_chunk{chunk_i}.log"
        cmd = [str(PYTHON), "article_crawler.py", str(CRAWL_CHUNK_SIZE), "--resume"]

        print(f"[{name}] チャンク{chunk_i}開始（上限{chunk_timeout:.0f}秒）", flush=True)
        chunk_started = time.monotonic()
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                result = subprocess.run(cmd, cwd=BASE, stdout=f, stderr=subprocess.STDOUT,
                                         timeout=chunk_timeout, env=env)
            ok = result.returncode == 0
            error = None if ok else f"returncode={result.returncode}"
        except subprocess.TimeoutExpired:
            ok = False
            error = f"タイムアウト（{chunk_timeout:.0f}秒）"
        except Exception as e:
            ok = False
            error = f"{type(e).__name__}: {e}"
        chunk_elapsed = time.monotonic() - chunk_started
        print(f"[{name}] チャンク{chunk_i} {'完了' if ok else '異常終了'}"
              f"（{chunk_elapsed:.0f}秒） ログ: {log_path}")
        chunk_results.append({"chunk": chunk_i, "ok": ok, "elapsed_sec": chunk_elapsed,
                               "log_path": str(log_path), "error": error})

        try:
            chunk_log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            chunk_log_text = ""
        if "クロール対象: 0件" in chunk_log_text:
            print(f"[{name}] 本日の残りtarget 0件を確認したため終了します")
            break
    else:
        print(f"[{name}] チャンク数の上限（{CRAWL_MAX_CHUNKS}）に達したため打ち切ります")

    total_elapsed = time.monotonic() - overall_started
    all_ok = all(r["ok"] for r in chunk_results) if chunk_results else False
    error = None if all_ok else "; ".join(r["error"] for r in chunk_results if not r["ok"]) or None
    return {
        "name": name, "ok": all_ok, "elapsed_sec": total_elapsed,
        "log_path": "; ".join(r["log_path"] for r in chunk_results),
        "error": error, "chunks": chunk_results,
    }


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


def effective_skip(skip: set) -> set:
    """--skip-*で指定されたステップ集合に、連動してスキップすべきステップを加える。
    crawl_pass2_backlogはcrawlの後始末なので、crawlをスキップするジョブ
    (競合クロール用のdaily-competitor-crawl等。個別のスキップ指定はrun.shに書かれている)では
    指定が無くても動かさない（デプロイ用スクリプトを変えずに済む）"""
    skip = set(skip)
    if "crawl" in skip:
        skip.add("crawl_pass2_backlog")
    return skip


def main():
    parser = argparse.ArgumentParser(description="日次パイプライン オーケストレーション")
    for name, _, _ in STEPS:
        parser.add_argument(f"--skip-{name.replace('_', '-')}", action="store_true",
                             dest=f"skip_{name}")
    args = parser.parse_args()
    skip = effective_skip({name for name, _, _ in STEPS if getattr(args, f"skip_{name}")})

    if not acquire_lock():
        sys.exit(1)

    results = []
    try:
        for name, step_args, timeout_sec in STEPS:
            if name in skip:
                print(f"[{name}] スキップ指定によりスキップ")
                continue
            if name == "crawl":
                results.append(run_crawl_step_with_resume(name, timeout_sec))
            else:
                results.append(run_step(name, step_args, timeout_sec))
    finally:
        release_lock()

    print("─" * 40)
    for r in results:
        print(f"{r['name']}: {'OK' if r['ok'] else 'NG'}（{r['elapsed_sec']:.0f}秒）")
    notify_failures(results)


if __name__ == "__main__":
    main()
