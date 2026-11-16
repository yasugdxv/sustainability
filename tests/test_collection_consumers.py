"""クロール先(Source)を使うアプリの扱い(collection/consumers.py, crawl_targets.used_by)のテスト。外部には触れない。"""
import re
from pathlib import Path

import pytest
import requests

import article_analyzer as aa
from collection import consumers

ROOT = Path(__file__).resolve().parents[1]


def _http_error(status):
    r = requests.Response()
    r.status_code = status
    return requests.exceptions.HTTPError(response=r)


def test_used_by_defaults_to_sustainability_when_missing_or_empty():
    for t in ({}, None, {"used_by": None}, {"used_by": []}, {"crawl_target_id": "x"}):
        assert consumers.used_by(t) == {"sustainability"}


def test_is_used_by():
    t = {"used_by": ["sustainability", "erm"]}
    assert consumers.is_used_by(t, "erm") and consumers.is_used_by(t, "sustainability")
    assert not consumers.is_used_by(t, "geo")
    assert not consumers.is_used_by({"used_by": ["erm"]}, "sustainability")


def test_known_apps_and_default_are_consistent():
    assert set(consumers.DEFAULT_APPS) <= set(consumers.KNOWN_APPS)
    assert {"sustainability", "erm", "geo"} <= set(consumers.KNOWN_APPS)


class _Client:
    def __init__(self, fail_with=None):
        self.calls, self.fail_with = [], fail_with

    def select(self, table, params):
        self.calls.append((table, params))
        if self.fail_with and "used_by" in params["select"]:
            raise self.fail_with
        return [{"crawl_target_id": "t1"}]


def test_select_targets_requests_used_by():
    c = _Client()
    consumers.select_targets(c, "crawl_target_id,domain")
    assert c.calls == [("crawl_targets", {"select": "crawl_target_id,domain,used_by"})]


def test_select_targets_falls_back_when_column_missing():
    c = _Client(fail_with=_http_error(400))          # SQL未適用: 列が無い
    rows = consumers.select_targets(c, "crawl_target_id,domain")
    assert rows == [{"crawl_target_id": "t1"}]
    assert c.calls[-1][1]["select"] == "crawl_target_id,domain"      # used_by 抜きで取り直した


def test_select_targets_does_not_hide_other_failures():
    with pytest.raises(requests.exceptions.HTTPError):
        consumers.select_targets(_Client(fail_with=_http_error(500)), "crawl_target_id")


class _AnalyzerClient:
    """fetch_unanalyzed_articles 用の差し替え。articles / article_analysis / crawl_targets を返す。"""

    def __init__(self, targets, with_used_by_column=True):
        self.targets, self.with_col = targets, with_used_by_column

    def select(self, table, params):
        if table == "articles":
            return [{"article_id": f"a{i}", "crawl_target_id": tid, "title": "t", "extracted_text": "x"}
                    for i, tid in enumerate(["t_sus", "t_both", "t_erm", "t_legacy", "t_unknown"])]
        if table == "article_analysis":
            return []
        if table == "crawl_targets":
            if "used_by" in params["select"] and not self.with_col:
                raise _http_error(400)
            rows = self.targets if self.with_col else [{k: v for k, v in t.items() if k != "used_by"} for t in self.targets]
            return rows
        raise AssertionError(table)


TARGETS = [
    {"crawl_target_id": "t_sus", "used_by": ["sustainability"]},
    {"crawl_target_id": "t_both", "used_by": ["sustainability", "erm"]},
    {"crawl_target_id": "t_erm", "used_by": ["erm"]},
    {"crawl_target_id": "t_legacy"},                      # used_by が無い行 = サステナ用
]


def test_analyzer_skips_articles_of_other_apps_sources():
    ids = [a["crawl_target_id"] for a in aa.fetch_unanalyzed_articles(_AnalyzerClient(TARGETS))]
    assert "t_erm" not in ids                              # ERM専用Sourceの記事は分析しない
    assert ids == ["t_sus", "t_both", "t_legacy", "t_unknown"]     # 共有Source・既存の挙動(未登録Sourceも含む)は維持


def test_analyzer_behaves_as_before_when_column_not_applied():
    ids = [a["crawl_target_id"] for a in aa.fetch_unanalyzed_articles(_AnalyzerClient(TARGETS, with_used_by_column=False))]
    assert ids == ["t_sus", "t_both", "t_erm", "t_legacy", "t_unknown"]     # SQL未適用なら全記事（従来どおり）


def test_sql_is_idempotent_and_defaults_to_sustainability():
    sql = (ROOT / "sql" / "2026-11-16_crawl_targets_used_by.sql").read_text(encoding="utf-8")
    assert "add column if not exists used_by text[] not null default '{sustainability}'" in sql
    assert "create index if not exists" in sql
    assert not re.search(r"\bdrop\b|\bdelete\b|\btruncate\b", sql, re.I)      # データを消す文を含まない


# ─── 他アプリ専用Sourceの記事を除く（選定・選定の回復処理） ───────────────────────

def test_excluded_target_ids():
    class C:
        def select(self, table, params):
            return [{"crawl_target_id": "t_sus", "used_by": ["sustainability"]},
                    {"crawl_target_id": "t_both", "used_by": ["sustainability", "erm"]},
                    {"crawl_target_id": "t_erm", "used_by": ["erm"]},
                    {"crawl_target_id": "t_legacy"}]
    assert consumers.excluded_target_ids(C(), "sustainability") == {"t_erm"}
    assert consumers.excluded_target_ids(C(), "erm") == {"t_sus", "t_legacy"}


def test_excluded_target_ids_is_empty_when_column_not_applied():
    class C:
        def select(self, table, params):
            if "used_by" in params["select"]:
                raise _http_error(400)
            return [{"crawl_target_id": "t1"}, {"crawl_target_id": "t2"}]
    assert consumers.excluded_target_ids(C(), "sustainability") == set()      # 従来どおり全件


def test_selector_candidates_skip_other_app_articles(monkeypatch):
    import sustainability_article_selector as sas

    class C:
        def select(self, table, params):
            if table == "articles":
                return [{"article_id": "a1", "article_url_id": "u1", "crawl_target_id": "t_sus", "published_at": None},
                        {"article_id": "a2", "article_url_id": "u2", "crawl_target_id": "t_erm", "published_at": None}]
            if table == "crawl_targets":
                return [{"crawl_target_id": "t_sus", "used_by": ["sustainability"]},
                        {"crawl_target_id": "t_erm", "used_by": ["erm"]}]
            return []

    seen = {}
    monkeypatch.setattr(sas.common, "_select_in_chunks", lambda client, table, params, field, ids: seen.setdefault("ids", ids) and [])
    monkeypatch.setattr(sas.common, "resolve_cluster_root", lambda row, all_urls: row["article_url_id"])
    roots = sas.list_candidate_cluster_ids(C(), all_urls={"u1": {"article_url_id": "u1"}, "u2": {"article_url_id": "u2"}})
    assert roots == ["u1"]                      # ERM専用Sourceの記事(u2)は候補にならない
    assert seen["ids"] == ["a1"]
