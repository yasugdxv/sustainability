"""ニューラル週次メールとの突合(neural_weekly_check.py)のテスト（2026-10-23追加）

実DB・外部サイトには接続しない。判定の定義（到達点＝C以上、2段判定、分母除外、原因コード）と、
過去の読み取りミス（出典URLのセルに複数のURLが入っている）の再発防止を確認する。
"""
import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import neural_weekly_check as nw  # noqa: E402


# ─── 読み取り ───
def test_source_url_cell_with_many_urls_is_split():
    """2026-10-23に、1つのセルに最大12個のURLが入っていて、全体を1つのURLと誤読していた"""
    cell = "https://a.com/x\n\nhttps://b.com/y\r\nhttps://c.com/z  https://d.com/w"
    assert nw.split_source_urls(cell) == ["https://a.com/x", "https://b.com/y", "https://c.com/z", "https://d.com/w"]
    assert nw.split_source_urls(None) == [] and nw.split_source_urls("メモ") == []


def test_read_neural_xlsx(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None] * 9)
    ws.append(["#", "テーマ", "日付", "タイトル", "内容", "200字", "出典タイトル", "出典URL", None])
    ws.append([1, "気候変動", datetime(2026, 10, 16), "題名1", "x", "y", "Source One", "https://a.com/x\nhttps://b.com/y", None])
    ws.append([2, "自然資本", datetime(2026, 10, 15), "題名2", "x", "y", "Source Two", "https://c.com/z", None])
    p = tmp_path / "サマリーレポート_2026-10-16.xlsx"
    wb.save(p)
    rows = nw.read_neural_xlsx(p)
    assert [r["mail"] for r in rows] == ["2026-10-16"] * 2 and rows[0]["urls"] == ["https://a.com/x", "https://b.com/y"]
    assert rows[0]["url"] == "https://a.com/x" and rows[1]["theme"] == "自然資本" and rows[1]["date"] == "2026-10-15"


def test_url_variants_cover_www_scheme_and_trailing_slash():
    v = nw.url_variants("https://www.example.com/news/a")
    assert "http://example.com/news/a/" in v and "https://www.example.com/news/a" in v and "https://example.com/news/a" in v


@pytest.mark.parametrize("url,expected", [
    ("https://www.ifrs.org/news-and-events/news/2026/10/jingdong-hua/", date(2026, 10, 1)),
    ("https://x.com/news/2026/10/09/title", date(2026, 10, 9)),
    ("https://www.irena.org/News/pressreleases/2026/Sep/Renewables-Gain", date(2026, 9, 1)),
    ("https://www.meti.go.jp/shingikai/x/20261009_report.html", date(2026, 10, 9)),
    ("https://example.com/about/history", None),
    ("https://example.com/2026/13/45/x", None),
])
def test_source_date_from_url(url, expected):
    assert nw.source_date_from_url(url) == expected


def test_keywords_prefer_long_distinctive_words_and_skip_common_ones():
    kws = nw.keywords_from_title("Multilateralism: Council sets EU position ahead of climate change and biodiversity summits")
    assert kws[0] == "multilateralism" and "biodiversity" in kws and "council" not in nw._STOPWORDS
    assert nw.keywords_from_title("New report 2026") == []


# ─── 判定 ───
@pytest.mark.parametrize("level,status,expected", [
    ("S", "処理済", ("S", nw.J_CAPTURED)), ("C", "処理済", ("C", nw.J_CAPTURED)),
    ("D", "処理済", ("D", nw.J_D)),
    (None, "フィルタ除外", ("(フィルタ除外)", nw.J_FILTERED)),
    (None, "要確認", ("(未分析)", nw.J_UNANALYZED)),
])
def test_rank_judgement_reaches_at_c_or_above(level, status, expected):
    assert nw.rank_judgement(level, status) == expected


def test_best_analysis_takes_the_highest_rank():
    assert nw.best_analysis([{"importance_level": "D", "analysis_status": "処理済"}, {"importance_level": "B", "analysis_status": "処理済"}])["level"] == "B"
    assert nw.best_analysis([]) == {"level": None, "status": None}


def test_exclusions_before_crawl_start_and_out_of_scope():
    rules = {"crawl_start": "2026-08-01", "out_of_scope_keywords": ["hydrogen"], "out_of_scope_themes": ["その他"]}
    art = {"title": "x", "src_title": "Hydrogen hub opens", "theme": "気候変動"}
    assert nw.exclusion_reason(art, date(2026, 7, 20), rules)[0] == nw.J_EXCL_PRE
    assert nw.exclusion_reason(art, date(2026, 9, 1), rules)[0] == nw.J_EXCL_SCOPE
    assert nw.exclusion_reason({**art, "src_title": "Water", "theme": "その他"}, date(2026, 9, 1), rules)[0] == nw.J_EXCL_SCOPE
    assert nw.exclusion_reason({**art, "src_title": "Water"}, date(2026, 9, 1), rules) is None
    assert nw.exclusion_reason(art, None, {**rules, "out_of_scope_keywords": []}) is None      # 公表日が不明なら、除外しない


def _t(method="HTML", include="/news/", created="2026-06-29T00:00:00+00:00", domain="example.com", etype="ニュース・一覧ページ"):
    return {"crawl_target_id": "t1", "target_name": "T", "domain": domain, "crawl_method": method, "include_paths": include,
            "endpoint_type": etype, "created_at": created, "target_url": f"https://{domain}/news/"}


A = {"url": "https://example.com/reports/x"}


@pytest.mark.parametrize("targets,live,runs,expected", [
    ([], True, None, "target_missing"),
    ([_t("廃止")], True, None, "target_disabled"),
    ([_t("重複廃止")], True, None, "target_disabled"),
    ([_t("HTML", "/news/")], True, None, "path_excluded"),                              # /reports/x は、/news/ の外
    ([_t("Zyte", "/news/")], True, None, "path_excluded"),
    ([_t("RSS", "/feed/")], True, None, "feed_coverage"),                               # RSSでは、include_pathsは使われない
    ([_t("HTML", "/reports/", created="2026-10-20T00:00:00+00:00")], True, None, "registered_after"),
    ([_t("HTML", "/reports/")], False, None, "blocked"),
    ([_t("HTML", "/reports/")], True, 1, "crawl_gap"),
    ([_t("HTML", "/reports/")], True, 3, "listing_coverage"),
    ([_t("HTML", "/reports/")], None, None, "listing_coverage"),
    ([_t("API", "/safety-gate/", etype="API・データ")], True, 3, "api_target"),
])
def test_classify_miss(targets, live, runs, expected):
    assert nw.classify_miss(A, targets, date(2026, 9, 18), live, runs) == expected


def test_every_reason_code_has_a_label_and_an_owner():
    for code in ("target_missing", "target_disabled", "path_excluded", "registered_after", "blocked", "crawl_gap",
                 "feed_coverage", "listing_coverage", "api_target", "unknown"):
        assert all(nw.REASONS[code])


# ─── KPI・履歴 ───
def _row(j, code="", ahead=None):
    return {"judgement": j, "reason_code": code, "days_ahead": ahead}


def test_compute_kpis_counts_denominator_coverage_and_freshness():
    rows = [_row(nw.J_CAPTURED, ahead=4), _row(nw.J_CAPTURED, ahead=2), _row(nw.J_EVENT), _row(nw.J_D), _row(nw.J_FILTERED),
            _row(nw.J_MISSED, "target_missing"), _row(nw.J_MISSED, "blocked"), _row(nw.J_MISSED, "blocked"),
            _row(nw.J_EXCL_PRE), _row(nw.J_EXCL_SCOPE)]
    k = nw.compute_kpis(rows, "2026-10-16")
    assert (k["total"], k["denominator"], k["excluded_pre_crawl"], k["excluded_out_of_scope"]) == (10, 8, 1, 1)
    assert k["captured_strict"] == 2 and k["coverage_strict"] == 0.25 and k["coverage_with_event"] == 0.375
    assert (k["collected_but_D"], k["collected_but_filtered"], k["not_collected"]) == (1, 1, 3)
    assert k["avg_days_ahead"] == 3.0 and k["reasons"] == {"target_missing": 1, "blocked": 2}


def test_compute_kpis_with_nothing_does_not_divide_by_zero():
    assert nw.compute_kpis([], "2026-10-16")["coverage_strict"] == 0


def test_history_is_appended_and_a_rerun_of_the_same_mail_replaces_its_row(tmp_path):
    h = tmp_path / "logs" / "history.csv"
    nw.append_history(h, nw.compute_kpis([_row(nw.J_CAPTURED, ahead=3)], "2026-10-09"))
    nw.append_history(h, nw.compute_kpis([_row(nw.J_MISSED, "blocked")], "2026-10-16"))
    nw.append_history(h, nw.compute_kpis([_row(nw.J_CAPTURED, ahead=5)], "2026-10-16"))          # 再実行
    rows = list(csv.DictReader(open(h, encoding="utf-8-sig")))
    assert [r["mail_date"] for r in rows] == ["2026-10-09", "2026-10-16"] and rows[1]["captured_strict"] == "1"


# ─── 突合の流れ(DBを模した偽物) ───
class FakeClient:
    """article_urls / articles / article_analysis / crawl_logs を、最小限に模す"""

    def __init__(self, urls, analyses, logs=()):
        self.urls = urls          # {url: first_detected_at}
        self.analyses = analyses  # {url: (level, status)}
        self.logs = list(logs)

    def select(self, table, params=None):
        params = params or {}
        if table == "article_urls" and "article_url" in params.keys():
            wanted = {v.strip('"') for v in params["article_url"][4:-1].split('","')}
            return [{"article_url_id": f"id-{u}", "article_url": u, "first_detected_at": ts} for u, ts in self.urls.items() if u in wanted]
        if table == "articles" and "article_url_id" in params:
            ids = params["article_url_id"][4:-1].split(",")
            return [{"article_id": f"a-{i[3:]}", "crawl_target_id": "t1"} for i in ids]
        if table == "article_analysis" and "article_id" in params and params["article_id"].startswith("in."):
            ids = params["article_id"][4:-1].split(",")
            return [{"article_id": i, "importance_level": self.analyses[i[2:]][0], "analysis_status": self.analyses[i[2:]][1]} for i in ids if i[2:] in self.analyses]
        if table == "crawl_logs":
            return self.logs
        return []


def _art(url, title="Water policy", no=1, d="2026-10-09", mail="2026-10-16", urls=None):
    return {"mail": mail, "no": no, "theme": "水", "date": d, "title": "題名", "src_title": title, "urls": urls or [url], "url": url}


def test_check_mail_end_to_end(monkeypatch):
    monkeypatch.setattr(nw, "live_fetch_ok", lambda *a, **k: (False, "403"))
    monkeypatch.setattr(nw, "find_event_candidates", lambda *a, **k: [])
    client = FakeClient(
        urls={"https://a.com/news/c": "2026-10-10T00:00:00+00:00", "https://a.com/news/d": "2026-10-11T00:00:00+00:00",
              "https://a.com/news/f": "2026-10-12T00:00:00+00:00", "https://m.com/multi2": "2026-10-13T00:00:00+00:00"},
        analyses={"https://a.com/news/c": ("B", "処理済"), "https://a.com/news/d": ("D", "処理済"),
                  "https://a.com/news/f": (None, "フィルタ除外"), "https://m.com/multi2": ("S", "処理済")})
    targets = [{"crawl_target_id": "t1", "target_name": "A", "domain": "a.com", "crawl_method": "RSS", "include_paths": "/", "endpoint_type": "RSS",
                "created_at": "2026-06-29T00:00:00+00:00", "target_url": "https://a.com/feed"}]
    arts = [_art("https://a.com/news/c", no=1), _art("https://a.com/news/d", no=2), _art("https://a.com/news/f", no=3),
            _art("https://a.com/news/zzz", no=4),                                                    # 未収集(対象はある・取得不可)
            _art("https://none.com/x", no=5),                                                        # 対象未登録
            _art("https://old.com/2026/05/x", no=6, d="2026-05-20"),                                 # クロール開始前
            _art("https://m.com/multi1", no=7, urls=["https://m.com/multi1", "https://m.com/multi2"])]   # 2つ目のURLが収集済み
    rows = {r["no"]: r for r in nw.check_mail(client, arts, targets, {**nw.DEFAULT_RULES}, None, True, live=True)}
    assert rows[1]["judgement"] == nw.J_CAPTURED and rows[1]["reach_rank"] == "B" and rows[1]["stage1"] == "URL一致" and rows[1]["days_ahead"] == 6
    assert rows[2]["judgement"] == nw.J_D and rows[2]["fix_owner"] == "重要度判定・フィルタ"
    assert rows[3]["judgement"] == nw.J_FILTERED
    assert rows[4]["judgement"] == nw.J_MISSED and rows[4]["reason_code"] == "blocked" and rows[4]["live_ok"] is False
    assert rows[5]["judgement"] == nw.J_MISSED and rows[5]["reason_code"] == "target_missing"
    assert rows[6]["judgement"] == nw.J_EXCL_PRE
    assert rows[7]["judgement"] == nw.J_CAPTURED                       # セル内の別のURLで、収集済み
    k = nw.compute_kpis(list(rows.values()), "2026-10-16")
    assert k["denominator"] == 6 and k["captured_strict"] == 2 and k["not_collected"] == 2


def test_event_candidate_at_c_or_above_counts_as_captured_by_another_medium(monkeypatch):
    monkeypatch.setattr(nw, "live_fetch_ok", lambda *a, **k: (True, None))
    monkeypatch.setattr(nw, "find_event_candidates", lambda *a, **k: [{"media": "reuters.com", "url": "u", "title": "same event", "level": "A"}])
    rows = nw.check_mail(FakeClient({}, {}), [_art("https://z.com/x")], [], {**nw.DEFAULT_RULES}, None, True, live=True)
    assert rows[0]["judgement"] == nw.J_EVENT and rows[0]["reach_rank"] == "A" and "reuters.com" in rows[0]["stage2"]


def test_crawl_completeness_is_the_daily_share_of_targets_processed():
    jst_day = lambda d, h: f"2026-10-{d:02d}T{h:02d}:00:00+09:00"
    logs = [{"crawl_target_id": f"t{i}", "started_at": jst_day(14, 3)} for i in range(5)]            # 10/14 は、10対象中5件
    logs += [{"crawl_target_id": f"t{i}", "started_at": jst_day(15, 3)} for i in range(10)]          # 10/15 は、全件
    logs += [{"crawl_target_id": "t0", "started_at": jst_day(15, 5)}]                                # 同じ対象の2回目は、数えない

    class C:
        def select(self, table, params=None):
            return logs

    r = nw.crawl_completeness(C(), "2026-10-16", expected_targets=10, days=4)
    assert r["per_day"] == {"10-14": 5, "10-15": 10} and r["avg"] == round((0.5 + 1.0) / 4, 3)       # 10/16 以前の2日は、ログなし=0


# ─── 事象一致(言語に依存しない突合) 2026-10-29追加 ───
# 背景: 第2段が、出典タイトルの英単語だけで探していたため、日本語タイトルの同名記事(Sustainable Japan)を、
# 1件も拾えず、事象一致が、96件中3件になっていた。日本語⇔英語でも、固有名詞・略称・数字でつながるようにした。
def test_lang_free_tokens_join_japanese_and_english_by_names_acronyms_and_numbers():
    jp = nw.lang_free_tokens("【国際】ISO、カーボンニュートラル規格ISO14068発行。オフセットより削減・除去優先")
    en = nw.lang_free_tokens("ISO 14068:2026 Climate change management — Carbon neutrality")
    assert {"iso", "iso14068"} & jp and "iso" in en
    assert "#2026" in en and "~カー" in jp and "the" not in en


class WindowClient:
    """articles / article_analysis(収集日時の窓で絞る)を、最小限に模す"""

    def __init__(self, docs):
        self.docs = docs      # [(title, url, fetched_at, summary, level)]

    def select(self, table, params=None):
        if int((params or {}).get("offset", 0)) > 0:
            return []
        if table == "articles":
            return [{"article_id": f"a{i}", "title": t, "fetched_url": u, "fetched_at": f} for i, (t, u, f, _s, _l) in enumerate(self.docs)]
        if table == "article_analysis":
            return [{"article_id": f"a{i}", "summary_short": s, "importance_level": lv} for i, (_t, _u, _f, s, lv) in enumerate(self.docs)]
        return []


def _neural(**kw):
    base = {"mail": "2026-10-02", "no": 1, "theme": "気候", "date": "2026-09-30", "title": "【国際】ISO、カーボンニュートラル規格ISO14068発行。オフセットより削減・除去優先",
            "summary": "国際標準化機構(ISO)は9月11日、カーボンニュートラルの達成・実証に関する国際規格ISO14068を発行した。", "src_title": "ISO 14068:2026 Climate change management — Carbon neutrality",
            "urls": ["https://www.iso.org/standard/14068"], "url": "https://www.iso.org/standard/14068"}
    return {**base, **kw}


DOCS = [
    ("【国際】ISO、カーボンニュートラル規格ISO14068発行。オフセットより削減・除去優先 | Sustainable Japan", "https://sustainablejapan.jp/2026/09/30/iso14068/", "2026-09-30T01:00:00+00:00", "ISOは9月11日、ISO14068を発行した", "A"),
    ("ISO publishes ISO 14068 carbon neutrality standard", "https://example-media.com/iso-14068-carbon-neutrality", "2026-10-01T01:00:00+00:00", "ISOがカーボンニュートラル規格を発行", "B"),
    ("Starbucks reports quarterly earnings", "https://example-media.com/starbucks-earnings", "2026-09-30T02:00:00+00:00", "スターバックスの決算", "C"),
    ("Old article about ISO", "https://example-media.com/old-iso", "2026-08-01T00:00:00+00:00", "古い記事", "B"),
]


def test_find_event_candidates_matches_across_languages_and_ignores_unrelated_and_old_articles():
    nw._WINDOW_CACHE.clear()
    cands = nw.find_event_candidates(WindowClient(DOCS), _neural(), date(2026, 9, 30))
    urls = [c["url"] for c in cands]
    assert urls[0] == "https://sustainablejapan.jp/2026/09/30/iso14068/" and cands[0]["score"] >= nw.EVENT_MIN_SCORE
    assert "https://example-media.com/iso-14068-carbon-neutrality" in urls                     # 英語の記事も、略称・数字でつながる
    assert not any("starbucks" in u or "old-iso" in u for u in urls)                              # 無関係・古い記事は、入らない


def test_find_event_candidates_excludes_the_neural_source_url_itself():
    nw._WINDOW_CACHE.clear()
    docs = DOCS + [("ISO 14068", "https://www.iso.org/standard/14068", "2026-09-30T03:00:00+00:00", "規格", "A")]
    assert all(c["url"] != "https://www.iso.org/standard/14068" for c in nw.find_event_candidates(WindowClient(docs), _neural(), date(2026, 9, 30)))


def _cand(media, level, score=0.5):
    return {"media": media, "url": f"https://{media}/x", "title": "t", "level": level, "score": score, "first_detected_at": "2026-09-30T01:00:00+00:00"}


def test_independent_media_takes_priority_and_own_media_is_counted_separately(monkeypatch):
    monkeypatch.setattr(nw, "live_fetch_ok", lambda *a, **k: (True, None))
    rules = {**nw.DEFAULT_RULES}
    monkeypatch.setattr(nw, "find_event_candidates", lambda *a, **k: [_cand("sustainablejapan.jp", "B"), _cand("example-media.com", "A")])
    r1 = nw.check_mail(FakeClient({}, {}), [_art("https://z.com/x")], [], rules, None, True, live=True)[0]
    assert r1["judgement"] == nw.J_EVENT and r1["reach_rank"] == "A" and r1["alt_own"]["level"] == "B"
    monkeypatch.setattr(nw, "find_event_candidates", lambda *a, **k: [_cand("sustainablejapan.jp", "B")])
    r2 = nw.check_mail(FakeClient({}, {}), [_art("https://z.com/x")], [], rules, None, True, live=True)[0]
    assert r2["judgement"] == nw.J_EVENT_OWN and r2["reach_rank"] == "B" and r2["alt_indep"] is None
    assert r2["reason_code"] == "target_missing"                       # URLでは未収集。クロール改善の手がかりとして、原因は残す


def test_weak_or_low_rank_alternatives_do_not_count(monkeypatch):
    monkeypatch.setattr(nw, "live_fetch_ok", lambda *a, **k: (True, None))
    rules = {**nw.DEFAULT_RULES}
    monkeypatch.setattr(nw, "find_event_candidates", lambda *a, **k: [_cand("example-media.com", "A", score=0.2), _cand("sustainablejapan.jp", "D")])
    r = nw.check_mail(FakeClient({}, {}), [_art("https://z.com/x")], [], rules, None, True, live=True)[0]
    assert r["judgement"] == nw.J_MISSED and "example-media.com" in r["stage2"]       # 類似度が低い・ランクDは、数えず、候補として表示だけ


def test_compute_kpis_reports_coverage_with_own_media_separately():
    rows = [_row(nw.J_CAPTURED), _row(nw.J_EVENT), _row(nw.J_EVENT_OWN, "target_missing"), _row(nw.J_MISSED, "blocked")]
    k = nw.compute_kpis(rows, "2026-10-16")
    assert (k["captured_strict"], k["captured_event_candidate"], k["captured_own_media"]) == (1, 1, 1)
    assert k["coverage_strict"] == 0.25 and k["coverage_with_event"] == 0.5 and k["coverage_with_own_media"] == 0.75
    assert k["reasons"] == {"blocked": 1} and k["reasons_url_miss"] == {"target_missing": 1, "blocked": 1}
