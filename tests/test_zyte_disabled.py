"""Zyte停止中（2026-10-19〜の既定）の挙動のテスト

背景: ZyteのAPIアカウントが停止されていた（応答: 403 /auth/account-suspended）ため、
Zyteを一時停止し、TinyFish Fetch→TinyFish Agentだけで運用する。再開は環境変数
CRAWL_ZYTE_ENABLED=true（コード変更不要）。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _zyte_disabled(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", False)
    monkeypatch.setattr(ac, "ESCALATION_TIERS", ac._active_tiers(("tinyfish_fetch", "zyte", "tinyfish_agent"), False))
    monkeypatch.setattr(ac, "_ZYTE_METHOD_TIERS", ac._build_zyte_method_tiers(False))
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    monkeypatch.setattr(ac, "_tinyfish_agent_call_count", 0)
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 10)
    monkeypatch.setattr(ac, "ARTICLE_FETCH_SLEEP", 0)


def _target(method="HTML", tid="t-1"):
    return {"crawl_target_id": tid, "target_name": "サイト", "publisher_name": "サイト",
            "crawl_method": method, "target_url": "https://example.com/news", "lookback_days": 7}


# ─── スイッチと段階の定義 ───
@pytest.mark.parametrize("raw,expected", [(None, False), ("", False), ("0", False), ("false", False),
                                          ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True)])
def test_zyte_switch_is_off_by_default_and_turns_on_only_with_explicit_value(raw, expected):
    env = {} if raw is None else {"CRAWL_ZYTE_ENABLED": raw}
    assert ac._env_flag("CRAWL_ZYTE_ENABLED", default=False, environ=env) is expected


def test_article_tiers_drop_zyte_when_disabled_and_keep_order():
    three = ("tinyfish_fetch", "zyte", "tinyfish_agent")
    assert ac._active_tiers(three, False) == ("tinyfish_fetch", "tinyfish_agent")
    assert ac._active_tiers(three, True) == three
    assert ac._active_tiers(("zyte", "tinyfish_fetch", "tinyfish_agent"), False) == ("tinyfish_fetch", "tinyfish_agent")


def test_method_tiers_when_disabled_json_api_is_empty_and_zyte_method_uses_tinyfish_only():
    off = ac._build_zyte_method_tiers(False)
    assert off["Zyte"] == ("tinyfish_fetch", "tinyfish_agent")
    assert off["JSON API"] == ()            # Zyte無しでは取得できない（Agentでは構造を保てない）
    on = ac._build_zyte_method_tiers(True)
    assert on["Zyte"] == ("tinyfish_fetch", "zyte", "tinyfish_agent") and on["JSON API"] == ("zyte", "tinyfish_agent")


# ─── Zyteを呼ばない（ネットワークにも出ない） ───
def test_call_zyte_raises_immediately_without_any_network_call(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("Zyte停止中にネットワークへ出てはいけない")

    monkeypatch.setattr(ac.requests, "post", no_network)
    with pytest.raises(ac.ZyteDisabledError):
        ac._call_zyte("https://example.com", {}, True, browser=False)


def test_escalation_chain_skips_zyte_and_goes_to_tinyfish_fetch(monkeypatch):
    monkeypatch.setattr(ac, "_requests_get_with_retry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")))
    monkeypatch.setattr(ac, "_fetch_with_zyte", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Zyteを呼んだ")))
    monkeypatch.setattr(ac, "_call_tinyfish_fetch", lambda *a, **k: ("<html>tf</html>", "https://x", 200))

    html, _, status, raw = ac._fetch_with_escalation("https://example.com", {}, True)

    assert html == "<html>tf</html>" and status == 200 and raw is None


def test_escalate_pending_goes_tinyfish_fetch_then_agent_only(monkeypatch):
    calls = []
    monkeypatch.setattr(ac, "extract_article",
                        lambda url, proxies, verify, tier=None, **k: calls.append(tier) or {"ok": False, "error": "x"})
    row = {"crawl_log_id": "log-1", "items_detected": 0, "new_items": 0, "updated_items": 0,
           "extraction_failures": 0, "error_message": None, "notes": None}
    item = {"target": _target(), "cand": {"url": "https://x/1", "title": None, "published_at": None},
            "crawl_log_id": "log-1", "lookback": 7, "is_bot_block": False}

    result = ac.escalate_pending([item], FakeSupabaseClient({"crawl_logs": [row]}), {}, True)

    assert calls == ["tinyfish_fetch", "tinyfish_agent"] and result["final_failed"] == 1


# ─── JSON API方式: Zyte停止中は取得できないことを、失敗として見える形で記録する ───
def test_json_api_target_is_recorded_as_failure_not_silently_skipped_while_zyte_is_off():
    client = FakeSupabaseClient({"crawl_logs": []})

    result = ac.process_target(_target("JSON API"), client, {}, True)

    assert result["run_result"] == "失敗" and "Zyte停止中" in result["error_message"]
    assert not result.get("pending_listing_escalation") and result["pending_escalations"] == []
    row = client.inserted["crawl_logs"][0]
    assert row["run_result"] == "失敗" and "CRAWL_ZYTE_ENABLED" in row["error_message"]


def test_zyte_method_target_is_crawled_in_pass1_with_tinyfish_fetch_not_deferred_to_pass2(monkeypatch):
    """Zyte停止中のZyte方式は、Pass1でTinyFish Fetchを直接使う（Pass2頼みだと、チャンクの時間切れと
    バックログの予算切れで、95対象のうち85対象が一覧取得すら行われなかった。10/19〜20実測）"""
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    list_tiers, extract_tiers, saved = [], [], []
    cands = [{"url": f"https://z.example/a/{i}", "title": f"記事{i}", "published_at": None} for i in range(3)]
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                        lambda target, proxies, verify, tier: list_tiers.append(tier) or (cands, 200, False, None, False))
    monkeypatch.setattr(ac, "extract_article", lambda url, *a, tier=None, **k: extract_tiers.append(tier) or
                        {"ok": True, "title": "題", "text": "本文" * 50, "published_at": None, "final_url": None})
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: saved.append(a[3]) or (True, True))

    result = ac.process_target(_target("Zyte"), client, {}, True)

    assert list_tiers == ["tinyfish_fetch"]                              # 一覧取得はTinyFish Fetch
    assert extract_tiers == ["tinyfish_fetch"] * 3                       # 記事取得もTinyFish Fetch
    assert len(saved) == 3 and result["new_items"] == 3 and result["run_result"] == "成功"
    assert not result.get("pending_listing_escalation")                  # Pass2へ回さない（Pass1で完結）
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert funnel.get("p1_tinyfish_direct") == 1 and "p1_routed_to_pass2" not in funnel


def test_zyte_method_listing_failure_is_recorded_as_failure_without_calling_the_agent(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": []})
    agent_calls = []
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                        lambda target, proxies, verify, tier: ([], None, False, "RuntimeError: TinyFish Fetch: 取得失敗", False))
    monkeypatch.setattr(ac, "_call_tinyfish_agent", lambda *a, **k: agent_calls.append(1))

    result = ac.process_target(_target("Zyte"), client, {}, True)

    assert result["run_result"] == "失敗" and "TinyFish Fetch" in result["error_message"]
    assert not result.get("pending_listing_escalation") and agent_calls == []   # 一覧取得でAgentを呼ばない（高コスト）
    assert client.inserted["crawl_logs"][0]["run_result"] == "失敗"           # 「更新なし」に隠さない


def test_zyte_method_article_failures_become_pending_for_the_normal_retry_path(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    cands = [{"url": "https://z.example/a/1", "title": "記事", "published_at": None}]
    monkeypatch.setattr(ac, "_list_candidates_with_tier", lambda *a, **k: (cands, 200, False, None, False))
    monkeypatch.setattr(ac, "extract_article", lambda *a, **k: {"ok": False, "error": "timeout", "http_status": 0})

    result = ac.process_target(_target("Zyte"), client, {}, True)

    assert len(result["pending_escalations"]) == 1                      # 記事の取得失敗は、再試行待ち（Fetch→Agent、上限あり）
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert funnel["p1_pending"][0]["u"] == "https://z.example/a/1" and funnel["p1_tinyfish_direct"] == 1


def test_when_zyte_is_enabled_again_zyte_method_goes_back_to_pass2_deferral(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", True)
    monkeypatch.setattr(ac, "_ZYTE_METHOD_TIERS", ac._build_zyte_method_tiers(True))
    client = FakeSupabaseClient({"crawl_logs": []})

    result = ac.process_target(_target("Zyte"), client, {}, True)

    assert result["pending_listing_escalation"]["tiers"] == ("tinyfish_fetch", "zyte", "tinyfish_agent")


def test_crawl_step_timeout_leaves_room_for_the_zyte_method_targets():
    import run_daily
    _, _, timeout = next(st for st in run_daily.STEPS if st[0] == "crawl")
    assert timeout >= 25200


# ─── Agentの上限が一覧取得にも効く（従来は記事の再試行にしか効いていなかった） ───
def test_listing_agent_calls_respect_the_run_cap_and_are_counted(monkeypatch):
    monkeypatch.setattr(ac, "TINYFISH_AGENT_RUN_CAP", 1)
    agent_calls = []

    def fake_list(target, proxies, verify, tier):
        if tier == "tinyfish_agent":
            agent_calls.append(target["crawl_target_id"])
        return [], None, False, "取得失敗", False

    monkeypatch.setattr(ac, "_list_candidates_with_tier", fake_list)
    tiers = ("tinyfish_fetch", "tinyfish_agent")

    first = ac._resolve_tiered_listing(_target(tid="t-1"), {}, True, tiers)
    second = ac._resolve_tiered_listing(_target(tid="t-2"), {}, True, tiers)

    assert agent_calls == ["t-1"]                                   # 上限1件: 2件目はAgentを呼ばない
    assert ac._tinyfish_agent_call_count == 1
    assert first[4] == "取得失敗"
    assert second[4] == "取得失敗（TinyFish Agentは上限に達したため未実施）"   # 失敗理由に、上限で未実施だったことが残る


def test_listing_agent_not_called_when_tinyfish_fetch_succeeds(monkeypatch):
    seen = []
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                        lambda target, proxies, verify, tier: seen.append(tier) or ([{"url": "https://x"}], 200, False, None, False))

    tier, candidates, *_ = ac._resolve_tiered_listing(_target(), {}, True, ("tinyfish_fetch", "tinyfish_agent"))

    assert tier == "tinyfish_fetch" and seen == ["tinyfish_fetch"] and ac._tinyfish_agent_call_count == 0


# ─── 異常検出: Zyteの403 ───
def test_detect_anomalies_names_zyte_account_suspension_when_403_repeats():
    report = {"budget_sec": 1000, "elapsed_sec": 10.0, "agent_cap": 30, "expired": 0, "error": None,
              "phase_sec": {"articles": 10.0, "listing": 0.0}, "listing": [],
              "articles": {"attempted_items": 8, "final_failed": 0, "unfinished": 0, "tiers": {},
                           "errors": {"zyte|HTTPError: 403 Client Error: Forbidden for url: https:/": 8}}}
    anomalies = ac.detect_anomalies(report)
    assert any("Zyteが403を8件" in a and "billing" in a for a in anomalies)
    report["articles"]["errors"] = {"zyte|HTTPError: 403 Client Error: Forbidden for url: https:/": 4}
    assert not any("Zyteが403" in a for a in ac.detect_anomalies(report))     # 5件未満は出さない


def test_report_counts_zyte_method_targets_handled_in_pass1_with_tinyfish_fetch():
    import crawl_funnel_report as rep
    logs = [{"crawl_target_id": str(i), "run_result": "成功", "notes": ac._compose_notes("", {"p1_tinyfish_direct": 1})}
            for i in range(3)] + [{"crawl_target_id": "x", "run_result": "更新なし", "notes": None}]

    s = rep.summarize(logs)

    assert s["p1_tinyfish_direct_targets"] == 3
    assert "Pass1でTinyFish Fetchを直接使った対象(Zyte停止中のZyte方式・一覧取得の再試行を含む) 3対象" in rep.render(s, "x")


# ─── Zyte停止中: HTML/RSSの一覧取得が通常HTTPで失敗した場合（2026-10-20） ───
def _normal_listing_fails(monkeypatch, error="HTTPError: 403 Client Error: Forbidden"):
    monkeypatch.setattr(ac, "_list_candidates_for_target",
                        lambda *a, **k: ([], 403, False, error, False, False, False))


def _ok_extracted():
    return {"ok": True, "title": "題", "text": "本文" * 50, "published_at": None, "final_url": None}


def test_html_listing_failure_is_retried_on_the_spot_with_tinyfish_fetch(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": [], "articles": []})
    _normal_listing_fails(monkeypatch)
    list_tiers, extract_tiers, saved = [], [], []
    cands = [{"url": f"https://h.example/a/{i}", "title": f"記事{i}", "published_at": None} for i in range(2)]
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                        lambda target, proxies, verify, tier: list_tiers.append(tier) or (cands, 200, False, None, False))
    monkeypatch.setattr(ac, "extract_article", lambda url, *a, tier=None, **k: extract_tiers.append(tier) or _ok_extracted())
    monkeypatch.setattr(ac, "save_article", lambda *a, **k: saved.append(a[3]) or (True, True))

    result = ac.process_target(_target("HTML"), client, {}, True)

    assert list_tiers == ["tinyfish_fetch"] and extract_tiers == ["tinyfish_fetch"] * 2
    assert len(saved) == 2 and result["run_result"] == "成功"
    assert not result.get("pending_listing_escalation")                     # Pass2頼みにしない
    funnel = ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1]
    assert funnel["p1_listing_fallback"] == "tinyfish_fetch"


def test_html_listing_failure_records_both_reasons_when_tinyfish_also_fails(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": []})
    _normal_listing_fails(monkeypatch, "HTTPError: 403 Forbidden")
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                        lambda *a, **k: ([], None, False, "RuntimeError: TinyFish Fetch: 取得失敗", False))

    result = ac.process_target(_target("HTML"), client, {}, True)

    assert result["run_result"] == "失敗" and not result.get("pending_listing_escalation")
    msg = client.inserted["crawl_logs"][0]["error_message"]
    assert "403 Forbidden" in msg and "TinyFish Fetchでも失敗" in msg       # 失敗理由が残る（「更新なし」に隠さない）


def test_rss_listing_failure_is_recorded_as_failure_with_reason_and_no_tinyfish_call(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": []})
    _normal_listing_fails(monkeypatch, "HTTPError: 403 Forbidden")
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("RSSにTinyFishを使ってはいけない")))

    result = ac.process_target(_target("RSS"), client, {}, True)

    assert result["run_result"] == "失敗" and not result.get("pending_listing_escalation")
    msg = client.inserted["crawl_logs"][0]["error_message"]
    assert "403 Forbidden" in msg and "RSSはZyte停止中のためフォールバックできません" in msg


def test_listing_404_is_unchanged_by_the_fallback(monkeypatch):
    client = FakeSupabaseClient({"crawl_logs": []})
    monkeypatch.setattr(ac, "_list_candidates_for_target",
                        lambda *a, **k: ([], 404, False, "HTTPError: 404 Client Error: Not Found", True, False, False))
    monkeypatch.setattr(ac, "_list_candidates_with_tier",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("404でTinyFishを呼んではいけない")))

    result = ac.process_target(_target("HTML"), client, {}, True)

    assert result["run_result"] == "失敗" and "404" in result["error_message"]


def test_with_zyte_enabled_html_listing_failure_still_defers_to_pass2(monkeypatch):
    monkeypatch.setattr(ac, "ZYTE_ENABLED", True)
    client = FakeSupabaseClient({"crawl_logs": []})
    _normal_listing_fails(monkeypatch)

    result = ac.process_target(_target("HTML"), client, {}, True)

    assert result["pending_listing_escalation"] and result["run_result"] == "更新なし"   # Zyte有効時は従来どおり
    assert ac._split_notes(client.inserted["crawl_logs"][0]["notes"])[1] == {"p1_listing_failed": 1}
