"""日次パイプライン(run_daily.py)で、記事のタイトル・要約を先回りで翻訳し、translation_cacheへ保存する。

背景(2026-10-26): 翻訳は、一覧を開いた人がいて初めて裏で始まる方式だったため、
毎日入る新着(200〜300件)は、最初の人には原文(英語)のまま見えていた。
分析が終わった直後に、日本語UIの翻訳を済ませておく（英語UIは --langs ja,en で追加できる）。

LLMの無駄遣いを避ける:
  - 最初にtranslation_cacheの全件をメモリへ読み込み、翻訳済みの記事は呼び出さない
  - 原文がすでに目的の言語（日本語UI向けの日本語、など）の場合も呼び出さない
  - 失敗した記事は保存されない。この実行の中では、1記事につき1回だけ試す（翌日の実行で再試行）
  - 時間予算(既定40分)を超えたら、残りは翌日へ回す

使い方:
    python pretranslate_articles.py                    # 直近90日・日本語UI向け
    python pretranslate_articles.py --dry-run          # LLMを呼ばず、翻訳が必要な件数だけ表示
    python pretranslate_articles.py --langs ja,en --since-days 30   # 英語UI向けも
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ai_client import make_openai_client  # noqa: E402
from config_utils import load_config  # noqa: E402
import sustainability_dashboard_core as core  # noqa: E402

DEFAULT_SINCE_DAYS = 90      # 検索画面の期間フィルター「3ヶ月」まで（それより古い記事は、開かれたときに翻訳される）
DEFAULT_LANGS = "ja"        # 英語UI向けは、件数が多く(初回は約7,000件)費用がかかるため、必要なときだけ --langs ja,en
DEFAULT_TIME_BUDGET_SEC = 2400
MAX_WORKERS = 8
BATCH = 40  # 時間予算の確認を挟む単位


def collect_todo(articles: list, langs: list) -> list:
    """翻訳が必要な(名前空間, 記事ID, 原文, 言語)の一覧。キャッシュ済み・翻訳不要のものは含めない。
    api_server.pyの一覧表示と同じ原文(タイトル、要約は summary_short → importance_reason)を使う"""
    todo = []
    for a in articles:
        for lang in langs:
            for namespace, text in (("title", a.get("title") or ""),
                                    ("summary", a.get("summary_short") or a.get("importance_reason") or "")):
                if (namespace, lang, a["article_id"]) in core._short_cache:
                    continue
                if not core._needs_translation(text, lang):
                    continue
                todo.append((namespace, a["article_id"], text, lang))
    return todo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="記事タイトル・要約の先回り翻訳")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--langs", default=DEFAULT_LANGS)
    parser.add_argument("--time-budget-sec", type=int, default=DEFAULT_TIME_BUDGET_SEC)
    parser.add_argument("--dry-run", action="store_true", help="LLMを呼ばず、翻訳が必要な件数だけを表示する")
    args = parser.parse_args(argv)
    langs = [x.strip() for x in args.langs.split(",") if x.strip()]

    config = load_config()
    client, model = make_openai_client(config)
    if client is None and not args.dry_run:
        print("[pretranslate] Azure OpenAIの設定が無いため、翻訳できません")
        return 1

    # 翻訳済みを先に読み込む（これを省くと、保存済みの記事までLLMへ投げてしまう）
    print(f"[pretranslate] 保存済みの翻訳を{core.init_translation_cache(config)}件読み込みました")
    articles = core.fetch_dashboard_articles(config, args.since_days)
    todo = collect_todo(articles, langs)
    by_kind = {}
    for ns, _, _, lang in todo:
        by_kind[f"{ns}/{lang}"] = by_kind.get(f"{ns}/{lang}", 0) + 1
    print(f"[pretranslate] 直近{args.since_days}日の記事{len(articles)}件のうち、翻訳が必要な項目 {len(todo)}件 {by_kind}")
    if args.dry_run or not todo:
        return 0

    started = time.monotonic()
    done = failed = 0
    skipped_budget = 0
    translate = {"title": core.translate_title, "summary": core.translate_summary}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for start in range(0, len(todo), BATCH):
            if time.monotonic() - started > args.time_budget_sec:
                skipped_budget = len(todo) - start
                break
            batch = todo[start:start + BATCH]
            futures = [(item, pool.submit(translate[item[0]], client, model, item[1], item[2], item[3])) for item in batch]
            for (ns, aid, text, lang), fut in futures:
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    pass
                # 失敗は保存されない（キャッシュに入らない）ので、入っていれば成功
                if (ns, lang, aid) in core._short_cache:
                    done += 1
                else:
                    failed += 1
            print(f"[pretranslate] {min(start + BATCH, len(todo))}/{len(todo)}件 処理（成功{done} / 失敗{failed}）", flush=True)

    print(f"[pretranslate] 完了: 成功{done}件 / 失敗{failed}件（翌日に再試行） / 時間予算で未着手{skipped_budget}件"
          f" / {time.monotonic() - started:.0f}秒")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
