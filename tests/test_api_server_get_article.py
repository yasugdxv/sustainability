# -*- coding: utf-8 -*-
"""GET /api/articles/{article_id} の回帰テスト。

2026-10-06発見: _get_articles()は既定でDEFAULT_LOOKBACK_DAYS(30日)分しか保持しない
ため、検索・カテゴリ画面で期間を30日超に広げて表示された記事の詳細を開くと、
DBには実在するのに404になっていた。全期間(ALL_TIME_SINCE_DAYS)での再検索フォール
バックを追加した修正を検証する。
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import sustainability_expert_common as common  # noqa: E402
import api_server  # noqa: E402


def _patch_no_llm_no_cache(monkeypatch):
    """翻訳・エンゲージメント・共有キャッシュを全てモック化し、実LLM呼び出し・
    実DBアクセス・ディスク上の共有キャッシュファイルへの副作用を防ぐ。"""
    monkeypatch.setattr(api_server, "_articles_cache", {})
    monkeypatch.setattr(common, "shared_cache_get", lambda key, ttl: None)
    monkeypatch.setattr(common, "shared_cache_set", lambda key, data: None)
    monkeypatch.setattr(api_server.core, "translate_title",
                         lambda client, model, aid, title, lang: title)
    monkeypatch.setattr(api_server.core, "translate_summary",
                         lambda client, model, aid, summary, lang: summary)
    monkeypatch.setattr(api_server.core, "translate_body",
                         lambda client, model, aid, text, lang: text)
    monkeypatch.setattr(api_server.core, "fetch_engagement_map", lambda config, ids: {})


def test_get_article_within_default_window_uses_fast_path(monkeypatch):
    _patch_no_llm_no_cache(monkeypatch)
    recent = {"article_id": "recent-1", "title": "Recent Article"}
    calls = []

    def fake_fetch(config, since_days):
        calls.append(since_days)
        return [recent]

    monkeypatch.setattr(api_server.core, "fetch_dashboard_articles", fake_fetch)
    client = TestClient(api_server.app)

    resp = client.get("/api/articles/recent-1")

    assert resp.status_code == 200
    assert resp.json()["id"] == "recent-1"
    # 既定ウィンドウで見つかった場合、全期間フォールバックへは進まないこと
    assert calls == [api_server.DEFAULT_LOOKBACK_DAYS]


def test_get_article_outside_default_window_falls_back_to_all_time(monkeypatch):
    """30日以内のリストには無いが、全期間リストには存在する記事(= 実際にDBには
    あるのに404になっていたケース)が、修正後は200で返ることを確認する。"""
    _patch_no_llm_no_cache(monkeypatch)
    old_article = {"article_id": "old-1", "title": "Old Article"}

    def fake_fetch(config, since_days):
        if since_days == api_server.ALL_TIME_SINCE_DAYS:
            return [old_article]
        return []  # 既定の30日分には存在しない

    monkeypatch.setattr(api_server.core, "fetch_dashboard_articles", fake_fetch)
    client = TestClient(api_server.app)

    resp = client.get("/api/articles/old-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "old-1"
    assert body["title"] == "Old Article"


def test_get_article_truly_nonexistent_still_404s(monkeypatch):
    _patch_no_llm_no_cache(monkeypatch)
    monkeypatch.setattr(api_server.core, "fetch_dashboard_articles", lambda config, since_days: [])
    client = TestClient(api_server.app)

    resp = client.get("/api/articles/does-not-exist")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "記事が見つかりません"}
