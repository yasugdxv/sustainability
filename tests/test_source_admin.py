"""Sourceの used_by 変更・追加の検査(collection/source_admin.py)と manage_sources.py のテスト。DBには触れない。"""
import json

import pytest

from collection import source_admin as sa

T = [
    {"crawl_target_id": "t1", "domain": "www.example.com", "target_url": "https://www.example.com/news/", "used_by": ["sustainability"]},
    {"crawl_target_id": "t2", "domain": "example.com", "target_url": "https://example.com/press", "used_by": ["sustainability", "erm"]},
    {"crawl_target_id": "t3", "domain": "other.org", "target_url": "https://other.org/"},          # used_by 無し = サステナ
]


def test_normalize_apps_rejects_unknown_and_sloppy_names():
    assert sa.normalize_apps(["erm", "erm", "geo"]) == ["erm", "geo"]
    assert sa.normalize_apps("erm") == ["erm"]
    for bad in (["ERM"], ["erm "], ["emr"], [""]):
        with pytest.raises(sa.SourceAdminError):
            sa.normalize_apps(bad)


def test_find_targets_by_id_domain_and_url_ignores_www_and_trailing_slash():
    r = sa.find_targets(T, ids=["t3", "nope"], domains=["EXAMPLE.com", "missing.net"], urls=["http://example.com/news"])
    assert [t["crawl_target_id"] for t in r["matched"]] == ["t3", "t1", "t2"]      # 重複して数えない
    assert r["not_found"] == {"ids": ["nope"], "domains": ["missing.net"], "urls": []}


def test_plan_add_app_only_changes_rows_that_need_it():
    plan = sa.plan_used_by_change(T, add=["erm"])
    assert {p["crawl_target_id"]: p["after"] for p in plan} == {"t1": ["erm", "sustainability"], "t3": ["erm", "sustainability"]}
    assert sa.plan_used_by_change([T[1]], add=["erm"]) == []                          # 既に含む → 変更なし


def test_plan_remove_app_and_never_leaves_empty():
    assert sa.plan_used_by_change([T[1]], remove=["erm"])[0]["after"] == ["sustainability"]
    with pytest.raises(sa.SourceAdminError, match="空になります"):
        sa.plan_used_by_change([T[0]], remove=["sustainability"])
    plan = sa.plan_used_by_change([T[0]], add=["erm"], remove=["sustainability"])       # 付け替えは可能
    assert plan[0]["after"] == ["erm"]


def test_plan_requires_a_change_and_rejects_contradiction():
    with pytest.raises(sa.SourceAdminError):
        sa.plan_used_by_change(T)
    with pytest.raises(sa.SourceAdminError):
        sa.plan_used_by_change(T, add=["erm"], remove=["erm"])


def _new(**kw):
    return {"crawl_target_id": "n1", "target_name": "N", "target_url": "https://new.example.net/a", "domain": "new.example.net",
            "publisher_name": "N", "publisher_tag_id": "SJ-11-01", "endpoint_type": "RSS", "change_detection_method": "GUID／URL／公開日",
            "crawl_detail_pages": True, "authentication_required": False, "lookback_days": 7,
            "used_by": ["erm"], "crawl_method": "RSS", **kw}


def test_validate_new_targets_requires_used_by():
    r = sa.validate_new_targets([{k: v for k, v in _new().items() if k != "used_by"}], T)
    assert r["to_insert"] == [] and "used_by は必須" in r["errors"][0]


def test_validate_new_targets_splits_existing_urls_and_validates_values():
    rows = [_new(),
            _new(crawl_target_id="n2", target_url="https://example.com/press/"),               # 既存URL(正規化で一致)
            _new(crawl_target_id="n3", target_url="https://x.example.net/", used_by=["nope"]),
            _new(crawl_target_id="n4", target_url="https://y.example.net/", crawl_method="廃止"),
            _new(crawl_target_id="t1", target_url="https://z.example.net/"),                    # ID重複
            _new(crawl_target_id="n6", target_url="https://new.example.net/a")]                 # 入力内でURL重複
    r = sa.validate_new_targets(rows, T)
    assert [x["crawl_target_id"] for x in r["to_insert"]] == ["n1"]
    assert r["already_exists"][0]["existing"]["crawl_target_id"] == "t2"
    assert len(r["errors"]) == 4


def test_validate_new_targets_requires_db_not_null_columns_but_allows_false_and_zero():
    for key in ("publisher_tag_id", "endpoint_type", "change_detection_method", "crawl_detail_pages", "lookback_days"):
        r = sa.validate_new_targets([{k: v for k, v in _new().items() if k != key}], T)
        assert r["to_insert"] == [] and key in r["errors"][0], key
    ok = sa.validate_new_targets([_new(crawl_detail_pages=False, authentication_required=False, lookback_days=0)], T)
    assert len(ok["to_insert"]) == 1 and not ok["errors"]


def test_validate_new_targets_checks_publisher_tag_exists():
    assert sa.validate_new_targets([_new()], T, valid_publisher_tags={"SJ-11-01"})["to_insert"]
    r = sa.validate_new_targets([_new(publisher_tag_id="SJ-99")], T, valid_publisher_tags={"SJ-11-01"})
    assert r["to_insert"] == [] and "tag_reference" in r["errors"][0]


def test_summarize_used_by():
    assert sa.summarize_used_by(T) == {"sustainability": 2, "erm+sustainability": 1}


# ─── manage_sources.py（書き込みの有無） ─────────────────────────────────────────

class _Client:
    def __init__(self):
        self.updates, self.inserts = [], []

    def select(self, table, params):
        if table == "tag_reference":
            return [{"tag_id": "SJ-11-01"}]
        return [dict(t) for t in T]

    def update(self, table, params, patch, prefer=None):
        self.updates.append((params, patch))

    def insert(self, table, rows, prefer=None):
        self.inserts.append(rows)


def _run(monkeypatch, argv, client):
    import manage_sources as ms
    monkeypatch.setattr(ms, "SupabaseClient", lambda cfg: client)
    monkeypatch.setattr(ms, "load_config", lambda: {})
    ms.main(argv)


def test_manage_sources_is_dry_run_by_default(monkeypatch, capsys):
    c = _Client()
    _run(monkeypatch, ["add-app", "erm", "--domains", "other.org"], c)
    assert c.updates == [] and "確認のみ" in capsys.readouterr().out


def test_manage_sources_apply_writes_only_changed_rows(monkeypatch):
    c = _Client()
    _run(monkeypatch, ["add-app", "erm", "--domains", "example.com,other.org", "--apply"], c)
    assert sorted(u[0]["crawl_target_id"] for u in c.updates) == ["eq.t1", "eq.t3"]    # t2 は既に erm を含むので書かない
    assert all(set(u[1]["used_by"]) == {"erm", "sustainability"} for u in c.updates)


def test_manage_sources_unknown_app_is_rejected_before_any_write(monkeypatch):
    c = _Client()
    with pytest.raises(SystemExit):
        _run(monkeypatch, ["add-app", "ERM", "--ids", "t1", "--apply"], c)
    assert c.updates == []


def test_manage_sources_add_targets_blocks_on_errors_and_inserts_on_apply(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{k: v for k, v in _new().items() if k != "used_by"}]), encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(json.dumps([_new()]), encoding="utf-8")
    c = _Client()
    with pytest.raises(SystemExit):
        _run(monkeypatch, ["add-targets", str(bad), "--apply"], c)
    assert c.inserts == []
    _run(monkeypatch, ["add-targets", str(good)], c)
    assert c.inserts == []                                      # 確認のみ
    _run(monkeypatch, ["add-targets", str(good), "--apply"], c)
    assert len(c.inserts) == 1 and c.inserts[0][0]["used_by"] == ["erm"]
