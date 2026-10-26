"""2026-10-26追加: 記事タイトル・要約の先回り翻訳(pretranslate_articles.py)と、翻訳の失敗・先読みのテスト。

背景: 翻訳が、一覧を開いた人がいて初めて始まる方式で、新着は原文(英語)のまま見えていた。
また、一時的な失敗の原文が、翻訳済みとして保存され、その記事が、ずっと翻訳されなくなる問題があった。"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import pretranslate_articles as pt  # noqa: E402
import run_daily  # noqa: E402
import sustainability_dashboard_core as core  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """モジュール内の翻訳キャッシュを、テストごとに空にする"""
    monkeypatch.setattr(core, "_short_cache", {})
    monkeypatch.setattr(core, "_short_failed_at", {})
    monkeypatch.setattr(core, "_db_checked_at", {})
    monkeypatch.setattr(core, "_translation_db_client", None)


class FakeLLM:
    """chat.completions.create を数えるだけの、偽のLLMクライアント"""

    def __init__(self, reply="翻訳結果", fail=False):
        self.calls, self.reply, self.fail = 0, reply, fail
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429 Too Many Requests")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


# ─── 失敗は保存しない ──────────────────────────────────────────────
def test_failed_translation_is_not_cached_and_not_retried_within_cooldown():
    llm = FakeLLM(fail=True)
    assert core.translate_title(llm, "m", "a1", "Hello world", "ja") == "Hello world"
    assert ("title", "ja", "a1") not in core._short_cache                # 原文を、翻訳済みとして保存しない
    assert core.translate_title(llm, "m", "a1", "Hello world", "ja") == "Hello world"
    assert llm.calls == 1                                                # 直後の再呼び出しでは、LLMを呼ばない


def test_failed_translation_is_retried_after_cooldown(monkeypatch):
    llm = FakeLLM(fail=True)
    core.translate_title(llm, "m", "a1", "Hello world", "ja")
    core._short_failed_at[("title", "ja", "a1")] -= core.SHORT_RETRY_COOLDOWN_SEC + 1
    llm.fail = False
    assert core.translate_title(llm, "m", "a1", "Hello world", "ja") == "翻訳結果"
    assert core._short_cache[("title", "ja", "a1")] == "翻訳結果"


def test_cached_or_unneeded_translations_never_call_the_llm():
    llm = FakeLLM()
    core._short_cache[("title", "ja", "a1")] = "既に翻訳済み"
    assert core.translate_title(llm, "m", "a1", "Hello world", "ja") == "既に翻訳済み"
    assert core.translate_title(llm, "m", "a2", "日本語のタイトル", "ja") == "日本語のタイトル"   # 日本語UIで日本語の原文
    assert core.translate_summary(llm, "m", "a3", "English summary", "en") == "English summary"  # 英語UIで英語の原文
    assert llm.calls == 0


# ─── DBの翻訳の先読み ──────────────────────────────────────────────
class FakeDB:
    def __init__(self, rows):
        self.rows, self.queries = rows, 0

    def select(self, table, params):
        self.queries += 1
        ids = params["article_id"][len("in.("):-1].split(",")
        return [r for r in self.rows if r["article_id"] in ids and r["target_lang"] == params["target_lang"].replace("eq.", "")]


def test_prefetch_loads_batch_results_into_memory_and_respects_cooldown(monkeypatch):
    db = FakeDB([{"namespace": "title", "target_lang": "ja", "article_id": "a1", "text": "日本語題名"},
                 {"namespace": "summary", "target_lang": "ja", "article_id": "a1", "text": "日本語要約"}])
    monkeypatch.setattr(core, "_translation_db_client", db)
    assert core.prefetch_translations_from_db(["a1", "a2"], "ja") == 2
    assert core._short_cache[("title", "ja", "a1")] == "日本語題名"
    queries = db.queries
    assert core.prefetch_translations_from_db(["a1", "a2"], "ja") == 0   # 確認済みの記事は、しばらく再問い合わせしない
    assert db.queries == queries


def test_prefetch_does_nothing_without_db_and_never_raises(monkeypatch):
    assert core.prefetch_translations_from_db(["a1"], "ja") == 0

    class Broken:
        def select(self, *a, **k):
            raise RuntimeError("DB接続不可")
    monkeypatch.setattr(core, "_translation_db_client", Broken())
    assert core.prefetch_translations_from_db(["a1"], "ja") == 0


# ─── 先回り翻訳の対象の決め方 ────────────────────────────────────────
ARTICLES = [
    {"article_id": "en1", "title": "English title", "summary_short": "日本語の要約"},
    {"article_id": "ja1", "title": "日本語の題名", "summary_short": "日本語の要約"},
    {"article_id": "done", "title": "Cached title", "summary_short": "要約"},
]


def test_collect_todo_skips_cached_and_already_target_language():
    core._short_cache[("title", "ja", "done")] = "翻訳済み"
    core._short_cache[("title", "en", "done")] = "Cached title"
    todo = pt.collect_todo(ARTICLES, ["ja", "en"])
    keys = {(ns, aid, lang) for ns, aid, _, lang in todo}
    assert ("title", "en1", "ja") in keys                                # 英語の題名 → 日本語UIで必要
    assert ("title", "ja1", "ja") not in keys                            # 日本語の題名 → 日本語UIでは不要
    assert ("title", "ja1", "en") in keys                                # 日本語の題名 → 英語UIで必要
    assert ("summary", "en1", "ja") not in keys                          # 要約は日本語 → 日本語UIでは不要
    assert ("summary", "en1", "en") in keys
    assert not any(aid == "done" and ns == "title" for ns, aid, _, _ in todo)   # 翻訳済みは、含めない


def test_main_dry_run_never_calls_llm(monkeypatch, capsys):
    monkeypatch.setattr(pt, "load_config", lambda: {})
    llm = FakeLLM()
    monkeypatch.setattr(pt, "make_openai_client", lambda config: (llm, "m"))
    monkeypatch.setattr(core, "init_translation_cache", lambda config: 0)
    monkeypatch.setattr(core, "fetch_dashboard_articles", lambda config, days: ARTICLES)
    assert pt.main(["--dry-run"]) == 0
    assert llm.calls == 0
    assert "翻訳が必要な項目" in capsys.readouterr().out


def test_main_translates_only_missing_items_and_counts_failures(monkeypatch, capsys):
    monkeypatch.setattr(pt, "load_config", lambda: {})
    llm = FakeLLM()
    monkeypatch.setattr(pt, "make_openai_client", lambda config: (llm, "m"))
    monkeypatch.setattr(core, "init_translation_cache", lambda config: 0)
    monkeypatch.setattr(core, "fetch_dashboard_articles", lambda config, days: ARTICLES[:1])
    assert pt.main(["--langs", "ja"]) == 0
    assert llm.calls == 1                                                # en1の題名(英語→日本語)の1回だけ
    assert core._short_cache[("title", "ja", "en1")] == "翻訳結果"
    assert "成功1件 / 失敗0件" in capsys.readouterr().out
    assert pt.main(["--langs", "ja"]) == 0                               # 2回目は、翻訳済みのため、LLMを呼ばない
    assert llm.calls == 1


def test_main_failure_is_counted_and_not_cached(monkeypatch, capsys):
    monkeypatch.setattr(pt, "load_config", lambda: {})
    monkeypatch.setattr(pt, "make_openai_client", lambda config: (FakeLLM(fail=True), "m"))
    monkeypatch.setattr(core, "init_translation_cache", lambda config: 0)
    monkeypatch.setattr(core, "fetch_dashboard_articles", lambda config, days: ARTICLES[:1])
    assert pt.main(["--langs", "ja"]) == 0
    assert "失敗1件" in capsys.readouterr().out
    assert ("title", "ja", "en1") not in core._short_cache


def test_main_without_llm_config_fails(monkeypatch):
    monkeypatch.setattr(pt, "load_config", lambda: {})
    monkeypatch.setattr(pt, "make_openai_client", lambda config: (None, None))
    assert pt.main([]) == 1


# ─── 日次パイプラインへの組み込み ─────────────────────────────────────
def test_translate_step_runs_after_analyze_and_not_in_the_competitor_job():
    names = [n for n, _, _ in run_daily.STEPS]
    assert names.index("translate") == names.index("analyze") + 1
    _, _, timeout = next(s for s in run_daily.STEPS if s[0] == "translate")
    assert pt.DEFAULT_TIME_BUDGET_SEC < timeout                          # 強制終了される前に、自分で止まる
    competitor_job = {"crawl", "reference_list", "api_articles", "filter_coverage_check", "analyze", "selector"}
    assert "translate" in run_daily.effective_skip(competitor_job)
    assert "translate" not in run_daily.effective_skip({"competitor_crawl", "selector"})   # 記事系ジョブでは動く
