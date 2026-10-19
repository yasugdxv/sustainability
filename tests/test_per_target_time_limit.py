"""1対象あたりの時間上限のテスト（2026-10-19追加）

背景: 通常クロールで、McKinsey(RSS)のように候補を順番に全件取得する遅い対象が、1回約38分
（以前は約85分）かかり、1チャンク(1時間)の半分以上を使っていた。上限を超えたら、残りの候補を
再試行待ち(Pass2・バックログ)へ回して、次の対象へ進む。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
import crawl_funnel_report as rep  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ac, "ARTICLE_FETCH_SLEEP", 0)
    monkeypatch.setattr(ac, "PER_TARGET_BUDGET_SEC", 600)
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")


def _target(name="サイト"):
    return {"crawl_target_id": "t-1", "target_name": name, "publisher_name": name,
            "crawl_method": "RSS", "target_url": "https://example.com/feed", "lookback_days": 7}


def _cands(n):
    return [{"url": f"https://example.com/a/{i}", "title": f"記事{i}", "published_at": None} for i in range(n)]


def _slow_clock(monkeypatch, per_call_sec):
    """time.monotonicを、呼ぶたびにper_call_sec秒進める疑似時計にする（実際には待たない）"""
    now = [0.0]
    monkeypatch.setattr(ac.time, "monotonic", lambda: now.__setitem__(0, now[0] + per_call_sec) or now[0])


def _run(monkeypatch, client, cands, extract_calls):
    monkeypatch.setattr(ac, "_list_candidates_for_target", lambda *a, **k: (cands, 200, False, None, False, False, False))
    monkeypatch.setattr(ac, "extract_article",
                        lambda url, *a, **k: extract_calls.append(url) or {"ok": False, "error": "timeout", "http_status": 0})
    return ac.process_target(_target(), client, {}, True)


def test_slow_target_stops_fetching_after_the_budget_and_defers_the_rest(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    _slow_clock(monkeypatch, 400)           # 判定のたびに400秒進む → 2件目の前の判定で上限(600秒)超え
    calls = []

    result = _run(monkeypatch, client, _cands(6), calls)

    assert len(calls) < 6                                    # 全件は取得しない（暴走を止める）
    assert result["budget_deferred"] == 6 - len(calls)       # 取得しなかった分は、すべて再試行待ちへ
    pending_urls = {p["cand"]["url"] for p in result["pending_escalations"]}
    assert all(f"https://example.com/a/{i}" in pending_urls for i in range(len(calls), 6))
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert funnel["p1_budget_deferred"] == result["budget_deferred"]
    assert funnel["p1_budget_skipped_known"] == 0
    assert len(funnel["p1_pending"]) == len(result["pending_escalations"])   # DBにも残る（Pass2に届かなくても失われない）


def test_deferred_candidates_already_in_db_are_skipped_not_sent_to_paid_retry(monkeypatch):
    known = [{"article_id": f"k{i}", "fetched_url": f"https://example.com/a/{i}"} for i in (3, 4)]
    client = FakeSupabaseClient({"crawl_logs": [], "articles": known})
    _slow_clock(monkeypatch, 400)
    calls = []

    result = _run(monkeypatch, client, _cands(6), calls)

    deferred_urls = {p["cand"]["url"] for p in result["pending_escalations"]}
    assert "https://example.com/a/3" not in deferred_urls and "https://example.com/a/4" not in deferred_urls
    assert result["budget_skipped_known"] == 2               # 既知の記事は、再試行に回さない
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert funnel["p1_budget_skipped_known"] == 2


def test_fast_target_is_unaffected_and_has_no_budget_fields(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    _slow_clock(monkeypatch, 1)             # 十分速い（全体でも上限に届かない）
    calls = []

    result = _run(monkeypatch, client, _cands(6), calls)

    assert len(calls) == 6 and result["budget_deferred"] == 0 and result["budget_skipped_known"] == 0
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert "p1_budget_deferred" not in funnel and "p1_budget_skipped_known" not in funnel


def test_budget_zero_disables_the_limit(monkeypatch):
    monkeypatch.setattr(ac, "PER_TARGET_BUDGET_SEC", 0)
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    _slow_clock(monkeypatch, 100000)        # どれだけ遅くても
    calls = []

    result = _run(monkeypatch, client, _cands(6), calls)

    assert len(calls) == 6 and result["budget_deferred"] == 0


def test_the_first_candidate_is_always_attempted_even_with_a_tiny_budget(monkeypatch):
    monkeypatch.setattr(ac, "PER_TARGET_BUDGET_SEC", 1)
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    _slow_clock(monkeypatch, 0.4)           # 判定のたびに0.4秒進む → 1件目は上限内
    calls = []

    _run(monkeypatch, client, _cands(10), calls)

    assert len(calls) >= 1


def test_db_lookup_failure_defers_the_candidate_instead_of_losing_it():
    class Broken:
        def select(self, *a, **k):
            raise RuntimeError("DB down")

    deferred, known = ac._defer_remaining_candidates(Broken(), _target(), _cands(3), "log-1", 7)

    assert len(deferred) == 3 and known == 0                 # 判定できないときは、取りこぼさない側に倒す


def test_budget_note_in_pass1_line_only_when_the_limit_was_hit():
    assert ac._budget_note({}) == ""
    assert ac._budget_note({"budget_deferred": 0, "budget_skipped_known": 0}) == ""
    assert "600秒" in ac._budget_note({"budget_deferred": 5, "budget_skipped_known": 0})
    note = ac._budget_note({"budget_deferred": 5, "budget_skipped_known": 3})
    assert "5件を繰越" in note and "既知3件は省略" in note


def test_report_counts_targets_that_hit_the_time_limit():
    notes = ac._compose_notes("", {"p1_budget_deferred": 7, "p1_budget_skipped_known": 20})
    logs = [{"crawl_target_id": "a", "run_result": "更新なし", "notes": notes},
            {"crawl_target_id": "b", "run_result": "更新なし", "notes": ac._compose_notes("", {"p1_budget_deferred": 3})},
            {"crawl_target_id": "c", "run_result": "更新なし", "notes": None}]

    s = rep.summarize(logs)

    assert s["budget_targets"] == 2 and s["budget_deferred"] == 10 and s["budget_skipped_known"] == 20
    assert "1対象の時間上限に達した対象 2対象（再試行待ちへ繰越 10件、既知のため省略 20件）" in rep.render(s, "x")
