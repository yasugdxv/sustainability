"""失敗理由を、段階ごと・記事ごとに追えるようにする記録のテスト（2026-10-27追加）

背景: 「Fetchが一部成功して、一部失敗する」サイトについて、待てば取れる失敗なのか、恒常的に取れない失敗なのかを、
記録から切り分けられなかった（最終失敗の理由しか残っていなかった）。
段階ごとの失敗理由(tier_fail)、通常クロールの失敗理由(p1_fail_reasons)、何回目の再試行で取れたか(rec_rounds)、
再試行待ちの記事ごとの直近の理由(r)と回数(n)を残す。
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
import crawl_funnel_report as fr  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

FETCH_FAIL = "RuntimeError: TinyFish Fetch: 取得失敗({'url': '...'"
AGENT_BOT = "RuntimeError: TinyFish Agent: 目標未達（Page is blocked by Cloudflare CAPTCHA）"


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", False)
    monkeypatch.setattr(ac, "ESCALATION_TIERS", ("tinyfish_fetch", "tinyfish_agent"))
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "_tinyfish_agent_site_calls", {})
    monkeypatch.setattr(ac.time, "sleep", lambda *_: None)


def _item(url="https://example.com/a1", log_id="log-1", **extra):
    return {"crawl_log_id": log_id, "target": {"crawl_target_id": "t1", "target_name": "T", "domain": "example.com"},
            "cand": {"url": url, "title": "t", "published_at": datetime(2026, 10, 27, tzinfo=timezone.utc)},
            "lookback": 7, **extra}


def _client(notes=None):
    return FakeSupabaseClient({"crawl_logs": [
        {"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
         "extraction_failures": 0, "error_message": None, "notes": notes}]})


def _funnel(client):
    return ac._split_notes(client.select("crawl_logs", {"select": "*", "crawl_log_id": "eq.log-1"})[0]["notes"])[1]


# ─── 段階ごとの失敗理由 ───────────────────────────────────────────
def test_each_tier_records_its_own_failure_reasons(monkeypatch):
    monkeypatch.setattr(ac, "extract_article", lambda url, p, v, tier=None, **kw: {
        "ok": False, "error": FETCH_FAIL if tier == "tinyfish_fetch" else AGENT_BOT, "http_status": None})
    client = _client()
    ac.escalate_pending([_item(f"https://example.com/a{i}") for i in range(3)], client, {}, True)
    f = _funnel(client)
    assert f["tier_fail"] == {"tinyfish_fetch": {"fetch_failed": 3}, "tinyfish_agent": {"bot_blocked": 3}}
    assert f["fail_reasons"] == {"bot_blocked": 3}                      # 従来の記録(最終失敗の理由)は、そのまま


def test_agent_cap_is_recorded_as_cap_skipped_not_as_a_failure(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 0)
    monkeypatch.setattr(ac, "extract_article", lambda url, p, v, tier=None, **kw: {
        "ok": False, "error": FETCH_FAIL, "http_status": None})
    client = _client()
    ac.escalate_pending([_item("https://example.com/a1"), _item("https://example.com/a2")], client, {}, True)
    f = _funnel(client)
    assert f["tier_fail"]["tinyfish_agent"] == {"cap_skipped": 2}        # 「Agentで失敗」と「Agentを試せていない」を区別する
    assert f["tier_fail"]["tinyfish_fetch"] == {"fetch_failed": 2}


# ─── 何回目の再試行で取れたか ─────────────────────────────────────
def _recover(monkeypatch):
    monkeypatch.setattr(ac, "extract_article", lambda url, p, v, tier=None, **kw: {
        "ok": True, "http_status": 200, "text": "本文" * 100, "title": "t"})
    monkeypatch.setattr(ac, "_apply_recovered_candidate", lambda *a, **k: (True, True))


def test_rec_rounds_counts_which_retry_round_recovered_the_article(monkeypatch):
    _recover(monkeypatch)
    client = _client()
    ac.escalate_pending([_item("https://example.com/a1"), _item("https://example.com/a2")], client, {}, True)
    assert _funnel(client)["rec_rounds"] == {"1": 2}                     # 初回の再試行で2件取れた


def test_rec_rounds_accumulate_across_days_and_use_the_carried_over_round_number(monkeypatch):
    _recover(monkeypatch)
    client = _client(notes="funnel=" + json.dumps({"rec_rounds": {"1": 3}}))
    # 前回までに1回再試行して取れなかった記事(n=1)が、2回目で取れた
    ac.escalate_pending([_item("https://example.com/a1", rounds=1)], client, {}, True)
    assert _funnel(client)["rec_rounds"] == {"1": 3, "2": 1}


# ─── 再試行待ちの記事ごとの記録 ───────────────────────────────────
def test_pending_entry_keeps_last_failure_class_and_round_count():
    e = ac._pending_entry(_item(last_class="bot_blocked", rounds=2))
    assert e["r"] == "bot_blocked" and e["n"] == 2
    assert "r" not in ac._pending_entry(_item()) and "n" not in ac._pending_entry(_item())


def test_unfinished_items_keep_their_reason_and_round_in_pending(monkeypatch):
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: pytest.fail("時間切れのため呼ばれないはず"))
    client = _client()
    ac.escalate_pending([_item(last_class="bot_blocked")], client, {}, True, deadline=time.monotonic() - 1)
    f = _funnel(client)
    assert f["p1_pending"][0]["r"] == "bot_blocked" and f["p1_pending"][0]["n"] == 1


def test_failed_item_pending_entry_shows_the_latest_reason(monkeypatch):
    monkeypatch.setattr(ac, "extract_article", lambda url, p, v, tier=None, **kw: {
        "ok": False, "error": FETCH_FAIL if tier == "tinyfish_fetch" else AGENT_BOT, "http_status": None})
    item = _item()
    ac.escalate_pending([item], _client(), {}, True)
    assert item["last_class"] == "bot_blocked"                           # 最後に試した段階(Agent)の理由
    assert ac._pending_entry(item)["r"] == "bot_blocked" and ac._pending_entry(item)["n"] == 1


# ─── 通常クロール(Pass1)の失敗の分類 ──────────────────────────────
@pytest.mark.parametrize("error,expected", [
    ("Bot対策(HTTP 403)を検知しました", "bot_blocked"),
    ("HTTPError: 403 Client Error: Forbidden for url: https://x.com/a", "bot_blocked"),
    ("HTTPError: 500 Server Error", "other"),
    ("http_None", "other"),
])
def test_pass1_failure_is_classified(error, expected):
    assert ac._classify_failure("pass1", error) == expected


def test_merge_funnel_merges_tier_fail_per_tier_and_adds_rec_rounds():
    merged = ac._merge_funnel({"tier_fail": {"tinyfish_fetch": {"fetch_failed": 5}}, "rec_rounds": {"1": 2}},
                              {"tier_fail": {"tinyfish_agent": {"bot_blocked": 1}}, "rec_rounds": {"1": 1, "2": 4}})
    assert merged["tier_fail"] == {"tinyfish_fetch": {"fetch_failed": 5}, "tinyfish_agent": {"bot_blocked": 1}}
    assert merged["rec_rounds"] == {"1": 3, "2": 4}


# ─── 集計と、クロール先別の切り分け ────────────────────────────────
def _log(tid, funnel, run_result="成功"):
    return {"crawl_target_id": tid, "run_result": run_result, "started_at": "2026-10-27T15:00:00+00:00",
            "notes": "funnel=" + json.dumps(funnel, ensure_ascii=False)}


def test_summarize_aggregates_new_keys():
    s = fr.summarize([
        _log("a", {"p1_fail_reasons": {"bot_blocked": 2}, "tier_fail": {"tinyfish_fetch": {"fetch_failed": 2}}, "rec_rounds": {"1": 1}}),
        _log("b", {"p1_fail_reasons": {"other": 1}, "tier_fail": {"tinyfish_fetch": {"fetch_failed": 1}, "tinyfish_agent": {"cap_skipped": 4}},
                   "rec_rounds": {"2": 2}})])
    assert s["p1_fail_reasons"] == {"bot_blocked": 2, "other": 1}
    assert s["tier_fail"]["tinyfish_fetch"] == {"fetch_failed": 3} and s["tier_fail"]["tinyfish_agent"] == {"cap_skipped": 4}
    assert s["rec_rounds"] == {"1": 1, "2": 2}
    text = fr.render(s, "テスト")
    assert "通常クロールの失敗理由" in text and "何回目の再試行で取れたか" in text and "1回目 1" in text


@pytest.mark.parametrize("tiers,tier_fail,fail_reasons,expected", [
    ({"tinyfish_fetch": [20, 18, 40.0]}, {}, {}, "再試行でほぼ取れる"),
    ({"tinyfish_fetch": [20, 8, 40.0]}, {}, {}, "一部だけ取れる"),
    ({"tinyfish_fetch": [30, 0, 60.0], "tinyfish_agent": [20, 1, 400.0]}, {"tinyfish_fetch": {"fetch_failed": 30}}, {}, "ほぼ取れない"),
    ({"tinyfish_fetch": [3, 0, 6.0]}, {}, {}, "データ不足"),
    ({"tinyfish_fetch": [30, 0, 60.0]}, {"tinyfish_fetch": {"login_required": 20, "fetch_failed": 5}}, {}, "要ログイン"),
])
def test_judge_target(tiers, tier_fail, fail_reasons, expected):
    s = fr.summarize([_log("a", {"tiers": tiers, "tier_fail": tier_fail, "fail_reasons": fail_reasons, "p1_article_failed": 5})])
    assert expected in fr.judge_target(s)


def test_render_by_target_lists_only_targets_with_failures_most_failed_first():
    logs = [_log("a", {"p1_article_failed": 3, "tiers": {"tinyfish_fetch": [3, 3, 6.0]}}),
            _log("b", {"p1_article_failed": 9, "tiers": {"tinyfish_fetch": [9, 0, 20.0]}, "tier_fail": {"tinyfish_fetch": {"fetch_failed": 9}}}),
            _log("c", {})]
    text = fr.render_by_target(logs, {"a": "サイトA", "b": "サイトB", "c": "サイトC"})
    assert text.index("サイトB") < text.index("サイトA")
    assert "サイトC" not in text
