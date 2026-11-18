"""RSSの記事は、保存済み・期間外のものを、本文から取り直さない機能のテスト（2026-11-18追加）

背景: RSSの記事を毎日すべて取り直し、保存済みの記事にもZyteを課金していた。
方針: フィード型のtargetだけ、保存済みURLと、公開日が期間外の記事は、取得しない。HTMLの一覧は、更新の検知のため従来どおり。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_crawler as ac  # noqa: E402

NOW = datetime.now(timezone.utc)
RSS_TARGET = {"crawl_target_id": "t1", "endpoint_type": "RSS", "target_url": "https://example.com/feed"}
HTML_TARGET = {"crawl_target_id": "t2", "endpoint_type": "ニュース・一覧ページ", "target_url": "https://example.com/news/"}


def _cand(url, days_ago=1):
    return {"url": url, "title": "t", "published_at": NOW - timedelta(days=days_ago) if days_ago is not None else None}


def _client(known_urls):
    c = MagicMock()
    c.select.return_value = [{"fetched_url": u} for u in known_urls]
    return c


def test_rss_known_and_stale_are_skipped(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_RSS", True)
    cands = [_cand("https://example.com/a"), _cand("https://example.com/b"), _cand("https://example.com/old", days_ago=30)]
    kept, known, stale = ac._skip_known_or_stale_rss(RSS_TARGET, _client(["https://example.com/a"]), cands, 7)
    assert [c["url"] for c in kept] == ["https://example.com/b"]
    assert (known, stale) == (1, 1)


def test_rss_candidate_without_date_is_kept_unless_known(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_RSS", True)
    cands = [_cand("https://example.com/nodate", days_ago=None)]
    kept, known, stale = ac._skip_known_or_stale_rss(RSS_TARGET, _client([]), cands, 7)
    assert len(kept) == 1 and (known, stale) == (0, 0)


def test_html_target_is_not_filtered(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_RSS", True)
    cands = [_cand("https://example.com/a"), _cand("https://example.com/old", days_ago=30)]
    client = _client(["https://example.com/a"])
    kept, known, stale = ac._skip_known_or_stale_rss(HTML_TARGET, client, cands, 7)
    assert kept == cands and (known, stale) == (0, 0)
    client.select.assert_not_called()


def test_db_error_falls_back_to_fetching(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_RSS", True)
    client = MagicMock()
    client.select.side_effect = RuntimeError("timeout")
    cands = [_cand("https://example.com/a")]
    kept, known, stale = ac._skip_known_or_stale_rss(RSS_TARGET, client, cands, 7)
    assert kept == cands and known == 0     # 問い合わせに失敗したら、取りこぼさないよう取得する


def test_switch_off_restores_old_behavior(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_RSS", False)
    cands = [_cand("https://example.com/a"), _cand("https://example.com/old", days_ago=30)]
    client = _client(["https://example.com/a"])
    kept, known, stale = ac._skip_known_or_stale_rss(RSS_TARGET, client, cands, 7)
    assert kept == cands and (known, stale) == (0, 0)
    client.select.assert_not_called()


def test_urls_with_quotes_and_commas_are_quoted(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_RSS", True)
    client = _client([])
    ac._skip_known_or_stale_rss(RSS_TARGET, client, [_cand('https://example.com/a,b"c')], 7)
    q = client.select.call_args[0][1]["fetched_url"]
    assert q.startswith('in.("') and "%22" in q and q.endswith('")')


# ─── 方式「Zyte」の一覧型にも広げた分(2026-11-18): 保存して7日以内は取らない、それより古いものは取り直す ───
ZYTE_TARGET = {"crawl_target_id": "t3", "crawl_method": "Zyte", "endpoint_type": "ニュース・一覧ページ", "target_url": "https://example.com/world/"}


def _client_with_dates(rows):
    c = MagicMock()
    c.select.return_value = [{"fetched_url": u, "fetched_at": (NOW - timedelta(days=d)).isoformat()} for u, d in rows]
    return c


def test_zyte_method_skips_recently_saved_but_rechecks_old_ones(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_ZYTE_METHOD", True)
    monkeypatch.setattr(ac, "ZYTE_RECHECK_DAYS", 7)
    cands = [_cand("https://example.com/new", None), _cand("https://example.com/recent", None), _cand("https://example.com/old", None)]
    client = _client_with_dates([("https://example.com/recent", 2), ("https://example.com/old", 10)])
    kept, known, stale = ac._skip_known_or_stale_rss(ZYTE_TARGET, client, cands, 7)
    assert [c["url"] for c in kept] == ["https://example.com/new", "https://example.com/old"]   # 保存して2日: 取らない / 10日: 取り直す
    assert known == 1 and stale == 0


def test_zyte_method_latest_fetch_wins_when_several_versions_exist(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_ZYTE_METHOD", True)
    client = _client_with_dates([("https://example.com/a", 30), ("https://example.com/a", 1)])   # 版が2つ。最新は1日前
    kept, known, _ = ac._skip_known_or_stale_rss(ZYTE_TARGET, client, [_cand("https://example.com/a", None)], 7)
    assert kept == [] and known == 1


def test_zyte_method_switch_off_restores_old_behavior(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_ZYTE_METHOD", False)
    client = _client_with_dates([("https://example.com/a", 1)])
    cands = [_cand("https://example.com/a", None)]
    kept, known, stale = ac._skip_known_or_stale_rss(ZYTE_TARGET, client, cands, 7)
    assert kept == cands and (known, stale) == (0, 0)
    client.select.assert_not_called()


def test_zyte_method_unreadable_fetched_at_is_rechecked(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_ZYTE_METHOD", True)
    client = MagicMock()
    client.select.return_value = [{"fetched_url": "https://example.com/a", "fetched_at": None}]
    cands = [_cand("https://example.com/a", None)]
    kept, known, _ = ac._skip_known_or_stale_rss(ZYTE_TARGET, client, cands, 7)
    assert kept == cands and known == 0       # 日時が不明なら、取りこぼさないよう取り直す


def test_json_api_target_is_not_filtered(monkeypatch):
    monkeypatch.setattr(ac, "SKIP_KNOWN_ZYTE_METHOD", True)
    client = _client_with_dates([("https://example.com/a", 1)])
    cands = [_cand("https://example.com/a", None)]
    kept, known, stale = ac._skip_known_or_stale_rss({**ZYTE_TARGET, "crawl_method": "JSON API"}, client, cands, 7)
    assert kept == cands and (known, stale) == (0, 0)
