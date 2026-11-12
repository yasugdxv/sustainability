# -*- coding: utf-8 -*-
"""記事の翻訳を日次バッチで先回りして行い、translation_cacheテーブルへ保存する。

背景: 一覧・詳細の表示時に初めて翻訳する方式だと、初回アクセスのユーザーが翻訳待ち
（または原文表示）になる。クロール・タグ付け(analyze)の後にここでまとめて翻訳しておけば、
ユーザーは翻訳済みの状態で見られる。

翻訳方向は両方向を扱う（日→英 / 英→日。どちらが必要かは原文の言語で自動判定）:
  - 日本語UI(ja): 英語記事の タイトル・要約・本文 を日本語へ
  - 英語UI(en)  : 日本語の 表示タイトル(display_title)・一覧要約・詳細要約 を英語へ
                  （AI生成物は日本語なので、英語UIでは基本的に全記事が対象）
本文は量が多いため、重要度が --body-ranks のものだけを対象にする（既定 S,A,B）。

保存先はtranslation_cache（DB）。起動済みのapi_server.pyは
core.refresh_translation_cache()で差分を取り込む（最大約2分遅れ）。

使い方:
    python translate_prefetch.py                      # 直近7日、ja/en両方
    python translate_prefetch.py --since-days 30
    python translate_prefetch.py --langs ja           # 日本語UI向けだけ
    python translate_prefetch.py --dry-run            # 件数だけ数えて翻訳しない
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ai_client import make_openai_client  # noqa: E402
from article_crawler import load_config  # noqa: E402
import sustainability_dashboard_core as core  # noqa: E402


def build_tasks(articles: list, langs: list, body_ranks: set) -> list:
    """未翻訳(キャッシュに無く、かつ原文の言語が対象言語と異なる)の翻訳タスクを列挙する。
    戻り値: (kind, article_id, text, lang) のリスト。kindは
    display_title/title/summary/summary_detail/body"""
    tasks = []
    for a in articles:
        aid = a["article_id"]
        display = a.get("display_title") or ""
        summary_raw = a.get("summary_short") or a.get("importance_reason") or ""
        for lang in langs:
            # タイトル: 表示タイトルがあればそれ、無ければ原題（api_server._to_ui_articleと同じ選択）
            if display:
                title_kind, title_text = "display_title", display
            else:
                title_kind, title_text = "title", a.get("title") or ""
            candidates = [(title_kind, title_text), ("summary", summary_raw),
                          ("summary_detail", a.get("summary_detail") or "")]
            for kind, text in candidates:
                if text and core._needs_translation(text, lang) \
                        and (kind, lang, aid) not in core._short_cache:
                    tasks.append((kind, aid, text, lang))
            # 詳細画面で原題を併記するため、表示タイトルがある記事は原題も訳しておく
            if display and a.get("title") and core._needs_translation(a["title"], lang) \
                    and ("title", lang, aid) not in core._short_cache:
                tasks.append(("title", aid, a["title"], lang))
            body = a.get("extracted_text") or ""
            if a.get("importance_level") in body_ranks and body and core._needs_translation(body, lang) \
                    and (lang, aid) not in core._body_cache:
                tasks.append(("body", aid, body, lang))
    return tasks


def run_task(azure_client, model: str, task: tuple) -> bool:
    """1件翻訳して成功したか返す（失敗時はcore側がキャッシュせず原文を返すので、
    キャッシュに入ったかどうかで成否を判定する）"""
    kind, aid, text, lang = task
    fn = {
        "display_title": core.translate_display_title,
        "title": core.translate_title,
        "summary": core.translate_summary,
        "summary_detail": core.translate_summary_detail,
        "body": core.translate_body,
    }[kind]
    fn(azure_client, model, aid, text, lang)
    cache_key = (lang, aid) if kind == "body" else (kind, lang, aid)
    cache = core._body_cache if kind == "body" else core._short_cache
    return cache_key in cache


def main():
    parser = argparse.ArgumentParser(description="記事翻訳の先回りバッチ（ja/en両方向）")
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--langs", default="ja,en")
    parser.add_argument("--body-ranks", default="S,A,B")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None, help="動作確認用: 先頭N件のみ")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    langs = [l for l in args.langs.split(",") if l in ("ja", "en")]
    body_ranks = {r for r in args.body_ranks.split(",") if r}

    loaded = core.init_translation_cache(config)  # DB接続設定と既存キャッシュの読み込み
    print(f"[translate_prefetch] 既存の翻訳キャッシュ {loaded}件を読み込みました")
    articles = core.fetch_dashboard_articles(config, args.since_days)
    tasks = build_tasks(articles, langs, body_ranks)
    if args.limit:
        tasks = tasks[:args.limit]
    by_kind = {}
    for t in tasks:
        by_kind[(t[0], t[3])] = by_kind.get((t[0], t[3]), 0) + 1
    print(f"[translate_prefetch] 記事{len(articles)}件中 翻訳対象 {len(tasks)}件 {dict(sorted(by_kind.items()))}")
    if args.dry_run or not tasks:
        return 0

    azure_client, model = make_openai_client(config)
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futures = {ex.submit(run_task, azure_client, model, t): t for t in tasks}
        for i, f in enumerate(as_completed(futures), 1):
            try:
                success = f.result()
            except Exception as e:
                success = False
                print(f"  失敗: {futures[f][:2]} {type(e).__name__}: {e}")
            ok += success
            failed += (not success)
            if i % 100 == 0 or i == len(tasks):
                print(f"  {i}/{len(tasks)}件完了（成功{ok} / 失敗{failed}）")
    print(f"[translate_prefetch] 完了: 成功{ok}件 / 失敗{failed}件（失敗分は次回バッチで再試行）")
    # 一部失敗しても日次パイプライン全体は止めない（成功分は保存済み）。全件失敗のみ異常扱い
    return 1 if tasks and ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
