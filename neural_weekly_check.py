"""ニューラル週次メールとの突合（週次パフォーマンスチェック）。

目的: 毎週届くニューラルのメールの掲載記事について、当方がどこまで捕捉できているか(カバレッジ・鮮度)を測り、
捕捉できなかった記事は「なぜか」を原因コードと修正先つきでログに残して、チューニングを速く回す。

判定の定義（KPI設計・確定前の案に沿う）:
  ・到達点＝重要度ランクC以上（S/A/B/C）。D=「収集したが選定落ち」、空欄(フィルタ除外)・未分析も、未捕捉として扱う
  ・第1段(URL一致): ニューラルの出典URL(セル内に複数あれば全て)が、収集済みデータにあるか
  ・第2段(事象一致): 第1段で見つからない記事について、出典タイトルの主要語で、別媒体の収集記事を探す（候補。要・目視確認）
  ・分母から除外: ①出典の公表日がクロール開始前 ②枠外(neural_check_rules.json の機械的な基準)。除外した件数も別途報告する
  ・鮮度: 当方の初回収集日時 と ニューラルのメールの日付 の差（目標＝平均3日以上先行）。
          ※Notionの掲載日時は、このアプリのDBに無いため、収集日時で代用している（Notion連携後に差し替える）

使い方:
  python neural_weekly_check.py サマリーレポート_2026-10-16.xlsx [別のxlsx ...] [--out logs/neural_check] [--no-live]
出力: 突合Excel(サマリー/突合シート/原因別)、ログ(JSON)、履歴(history.csv。週ごとのKPIを追記)
DBは読み取りのみ（書き込みなし）。--no-live を付けなければ、未捕捉の記事URLを、実際に取得して、取得可否も確認する（Agent等の有料の手段は使わない）。
"""
import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import article_crawler as ac

RANK_REACHED = ("S", "A", "B", "C")
RANK_ORDER = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}
DEFAULT_OWN_MEDIA = ["sustainablejapan.jp"]   # ニューラルの掲載記事と、同じ記事を載せる媒体(neural_check_rules.jsonで変更できる)
DEFAULT_RULES = {"crawl_start": "2026-08-01", "out_of_scope_keywords": [], "out_of_scope_themes": [], "own_media_domains": DEFAULT_OWN_MEDIA}
LISTING_METHODS = ("HTML", "ブラウザ操作", "Zyte")
DISABLED_METHODS = ("廃止", "重複廃止", "手動")
CRAWL_WINDOW_DAYS = (0, 3)           # 記事の公表日〜後3日。記事が、一覧・フィードに載っている間に、クロールが回っていたか
CRAWL_GAP_MIN_RUNS = 2               # この間のクロールが、これ未満なら、「クロールが回っていなかった」

# 判定の値
J_CAPTURED = "捕捉(C以上)"
J_EVENT = "別媒体で捕捉(候補・要目視)"
J_EVENT_OWN = "ニューラル媒体で捕捉(Sustainable Japan等)"
# 事象一致(言語に依存しない突合)の基準。類似度は、0〜1(2026-10-29、10月の28件で、同名記事は0.33〜0.69、無関係な記事は0.30未満)
EVENT_MIN_SCORE = 0.30          # これ以上を、捕捉の候補(重要度C以上のとき、捕捉に数える)
EVENT_SHOW_SCORE = 0.15         # これ以上を、突合シートに、候補として表示する(要目視)
J_D = "収集したが選定落ち(D)"
J_FILTERED = "収集したがフィルタ除外"
J_UNANALYZED = "収集したが未分析"
J_MISSED = "未収集"
J_EXCL_PRE = "分母除外(クロール開始前)"
J_EXCL_SCOPE = "分母除外(枠外)"

# 未捕捉の原因コード → (日本語, 修正先)
REASONS = {
    "target_missing": ("対象サイトが、クロール先に未登録", "クロール設定(クロール先の追加)"),
    "target_disabled": ("クロール先が、廃止・重複廃止・手動になっている", "クロール設定(方式の見直し)"),
    "path_excluded": ("クロール先はあるが、記事のパスが、include_pathsの外", "クロール設定(include_paths・クロール先の追加)"),
    "registered_after": ("クロール先の登録が、記事の公表より後", "対応不要(過去分)"),
    "blocked": ("取得できない(Bot対策・アクセス拒否)", "TinyFish・Zyte・Agent(外部サービスの対応)"),
    "crawl_gap": ("記事の公表から数日の間に、クロールが回っていなかった", "クロール運用(完走・スケジュール)"),
    "feed_coverage": ("RSSの収録範囲・期間に、載っていない", "クロール設定(HTML・サイトマップ方式の追加)"),
    "listing_coverage": ("一覧の1ページ目・上限・JS描画で、載っていない", "クロール設定／コード(サイトマップ・ページ送り)"),
    "api_target": ("APIの対象で、この記事は、対象外", "クロール設定(クロール先の追加)"),
    "unknown": ("原因を特定できない", "要調査"),
}

_STOPWORDS = {"with", "from", "this", "that", "have", "will", "into", "over", "under", "about", "after", "before", "their", "there",
              "these", "those", "which", "while", "would", "could", "should", "global", "report", "reports", "new", "launches",
              "launch", "announces", "announced", "says", "year", "years", "week", "first", "more", "most", "than", "other",
              "press", "release", "news", "update", "2024", "2025", "2026"}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ─── 読み取り・URL ───
def split_source_urls(cell) -> list:
    """「出典URL」のセルには、複数のURLが、空白・改行区切りで入っていることがある（最大12個を確認）"""
    return [u for u in re.split(r"\s+", str(cell or "").strip()) if u.startswith("http")]


def read_neural_xlsx(path, mail_date: str = None) -> list:
    """ニューラルの週次レポート(xlsx)から、記事の一覧を読む。mail_dateは、省略時にファイル名の日付(YYYY-MM-DD)"""
    import openpyxl
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
    mail_date = mail_date or (m.group(1) if m else None)
    if not mail_date:
        raise ValueError(f"メールの日付を、ファイル名から読めません: {path}")
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    header, rows = None, []
    for r in ws.iter_rows(values_only=True):
        if r and r[0] == "#":
            header = [str(c or "").strip() for c in r]
            continue
        if header is None or not r or r[0] in (None, ""):
            continue
        d = dict(zip(header, r))
        urls = split_source_urls(d.get("出典URL"))
        rows.append({"mail": mail_date, "no": d.get("#"), "theme": str(d.get("テーマ") or ""), "date": str(d.get("日付") or "")[:10],
                     "title": str(d.get("タイトル") or ""), "summary": str(d.get("内容") or ""), "src_title": str(d.get("出典タイトル") or ""), "urls": urls,
                     "url": urls[0] if urls else ""})
    return rows


def url_variants(url: str) -> list:
    """URLの表記ゆれ(http/https、www、末尾のスラッシュ)を、まとめて検索するための候補"""
    p = urlparse(url)
    host = p.netloc.lower()
    hosts = {host, host[4:] if host.startswith("www.") else "www." + host}
    paths = {p.path, p.path.rstrip("/"), p.path.rstrip("/") + "/"} if p.path else {"", "/"}
    out = set()
    for sch in ("https", "http"):
        for h in hosts:
            for pa in paths:
                out.add(f"{sch}://{h}{pa}")
                if p.query:
                    out.add(f"{sch}://{h}{pa}?{p.query}")
    return sorted(out)


def source_date_from_url(url: str):
    """URLの中の日付(/2026/10/09/、/2026/10/、/2026/Sep/、20261009)から、出典の公表日の目安を返す（無ければNone）"""
    path = urlparse(url).path
    m = re.search(r"/((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?=/|$|\.)", path)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"/((?:19|20)\d{2})/(\d{1,2})(?=/|$|\.)", path)
        if m:
            return date(int(m.group(1)), int(m.group(2)), 1)
        m = re.search(r"/((?:19|20)\d{2})/([A-Za-z]{3})[a-z]*(?=/|$)", path)
        if m and m.group(2).lower() in _MONTHS:
            return date(int(m.group(1)), _MONTHS[m.group(2).lower()], 1)
        m = re.search(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)", path)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return None


def keywords_from_title(title: str, n: int = 3) -> list:
    """第2段(事象一致)の検索に使う、出典タイトルの主要語（英字4文字以上、ありふれた語を除き、長い順にn個）"""
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", title or "")} - _STOPWORDS
    return sorted(words, key=lambda w: (-len(w), w))[:n]


def _parse_ts(s):
    if not s:
        return None
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], str(s).replace("Z", "+00:00"))
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ─── 判定 ───
def rank_judgement(level, analysis_status) -> tuple:
    """重要度ランクから、(到達ランクの表示, 判定)。C以上(S/A/B/C)=捕捉、D=選定落ち、空欄=フィルタ除外"""
    if level in RANK_REACHED:
        return level, J_CAPTURED
    if level == "D":
        return "D", J_D
    if analysis_status == "フィルタ除外":
        return "(フィルタ除外)", J_FILTERED
    return "(未分析)", J_UNANALYZED


def load_rules(path=None) -> dict:
    rules = dict(DEFAULT_RULES)
    if path and Path(path).exists():
        rules.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return rules


def exclusion_reason(article: dict, src_date, rules: dict):
    """分母から外すか。(判定, 理由)またはNone。クロール開始前の記事は、定常運用では発生しないため除外。
    枠外は、捕捉結果を見る前に、機械的な基準(neural_check_rules.json)で判定する"""
    if src_date and src_date < date.fromisoformat(rules["crawl_start"]):
        return J_EXCL_PRE, f"出典の公表日({src_date})が、クロール開始({rules['crawl_start']})より前"
    text = f"{article.get('title', '')} {article.get('src_title', '')}".lower()
    for kw in rules.get("out_of_scope_keywords") or []:
        if kw.lower() in text:
            return J_EXCL_SCOPE, f"枠外のキーワード「{kw}」に該当"
    if article.get("theme") and article["theme"] in (rules.get("out_of_scope_themes") or []):
        return J_EXCL_SCOPE, f"枠外のテーマ「{article['theme']}」に該当"
    return None


def classify_miss(article: dict, targets: list, src_date, live_ok, crawl_runs_in_window=None) -> tuple:
    """未捕捉の記事の原因コードを返す。targetsは、その記事のホストに対応するクロール先(行)のリスト。
    live_okは、いまの取得手段(通常→Fetch)で、記事を取得できたか(Noneは未検証)"""
    if not targets:
        return "target_missing"
    active = [t for t in targets if t.get("crawl_method") not in DISABLED_METHODS]
    if not active:
        return "target_disabled"
    path = urlparse(article["url"]).path
    path_ok = []
    for t in active:
        if t["crawl_method"] in LISTING_METHODS or t.get("endpoint_type") == "サイトマップ":
            inc = [p.strip() for p in (t.get("include_paths") or "").split(",") if p.strip()]
            path_ok.append((not inc) or any(ac._path_matches(ac._strip_aem_content_prefix(path), p) for p in inc))
        else:
            path_ok.append(True)           # RSS・APIでは、include_pathsは記事の絞り込みに使われない
    if not any(path_ok):
        return "path_excluded"
    if src_date:
        created = min((_parse_ts(t.get("created_at")) for t in active if t.get("created_at")), default=None)
        if created and created.date() > src_date:
            return "registered_after"
    if live_ok is False:
        return "blocked"
    if crawl_runs_in_window is not None and crawl_runs_in_window < CRAWL_GAP_MIN_RUNS:
        return "crawl_gap"
    methods = {t["crawl_method"] for t in active}
    if methods <= {"RSS"}:
        return "feed_coverage"
    if methods <= {"API", "JSON API"}:
        return "api_target"
    return "listing_coverage"


def crawl_completeness(client, mail_date: str, expected_targets: int, days: int = 9) -> dict:
    """メールの日付までの直近days日間の、クロールの完走率＝毎日、処理できた対象数 / クロール対象数 の平均。
    記事が一覧に載っている間に、クロールがどれだけ回っていたか（運用起因の週かどうか）を見分けるための指標"""
    end = date.fromisoformat(mail_date) + timedelta(days=1)
    start = end - timedelta(days=days)
    jst = timezone(timedelta(hours=9))
    f = lambda d: datetime(d.year, d.month, d.day, tzinfo=jst).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        logs = client.select("crawl_logs", {"select": "crawl_target_id,started_at", "and": f"(started_at.gte.{f(start)},started_at.lt.{f(end)})"})
    except Exception:  # noqa: BLE001
        return {"avg": None, "per_day": {}}
    per = {}
    for l in logs:
        ts = _parse_ts(l["started_at"])
        if ts:
            per.setdefault(ts.astimezone(jst).strftime("%m-%d"), set()).add(l["crawl_target_id"])
    daily = {d: len(v) for d, v in sorted(per.items())}
    n_days = days
    avg = sum(min(v / max(expected_targets, 1), 1.0) for v in daily.values()) / n_days if daily else 0.0
    return {"avg": round(avg, 3), "per_day": daily}


def compute_kpis(rows: list, mail_date: str, crawl: dict = None) -> dict:
    """週のKPI。分母＝全記事−分母除外。カバレッジ(厳密)＝URL一致でC以上、カバレッジ(事象候補を含む)＝候補も捕捉とみなした場合"""
    excluded = [r for r in rows if r["judgement"] in (J_EXCL_PRE, J_EXCL_SCOPE)]
    den = [r for r in rows if r["judgement"] not in (J_EXCL_PRE, J_EXCL_SCOPE)]
    strict = [r for r in den if r["judgement"] == J_CAPTURED]
    event = [r for r in den if r["judgement"] == J_EVENT]
    own = [r for r in den if r["judgement"] == J_EVENT_OWN]
    ahead = [r["days_ahead"] for r in strict if r.get("days_ahead") is not None]
    d = len(den) or 1
    return {
        "mail_date": mail_date, "total": len(rows), "excluded_pre_crawl": sum(1 for r in excluded if r["judgement"] == J_EXCL_PRE),
        "excluded_out_of_scope": sum(1 for r in excluded if r["judgement"] == J_EXCL_SCOPE), "denominator": len(den),
        "captured_strict": len(strict), "captured_event_candidate": len(event), "captured_own_media": len(own),
        "coverage_strict": round(len(strict) / d, 4), "coverage_with_event": round((len(strict) + len(event)) / d, 4),
        "coverage_with_own_media": round((len(strict) + len(event) + len(own)) / d, 4),
        "collected_but_D": sum(1 for r in den if r["judgement"] == J_D),
        "collected_but_filtered": sum(1 for r in den if r["judgement"] in (J_FILTERED, J_UNANALYZED)),
        "not_collected": sum(1 for r in den if r["judgement"] == J_MISSED),
        "avg_days_ahead": round(sum(ahead) / len(ahead), 2) if ahead else None, "freshness_n": len(ahead),
        "reasons": dict(Counter(r["reason_code"] for r in den if r["judgement"] == J_MISSED)),
        "reasons_url_miss": dict(Counter(r["reason_code"] for r in den if r["judgement"] in (J_MISSED, J_EVENT_OWN) and r.get("reason_code"))),
        "crawl_completeness": (crawl or {}).get("avg"), "crawl_per_day": (crawl or {}).get("per_day", {}),
    }


HISTORY_COLUMNS = ["run_at", "mail_date", "total", "excluded_pre_crawl", "excluded_out_of_scope", "denominator", "captured_strict",
                   "captured_event_candidate", "captured_own_media", "coverage_strict", "coverage_with_event", "coverage_with_own_media", "collected_but_D", "collected_but_filtered",
                   "not_collected", "avg_days_ahead", "freshness_n", "crawl_completeness", "reasons"]


def append_history(path: Path, kpi: dict) -> None:
    """週ごとのKPIを、履歴CSVに追記する（同じメールの日付の行は、置き換える）"""
    rows = []
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("mail_date") != kpi["mail_date"]]
    rows.append({**{k: kpi.get(k, "") for k in HISTORY_COLUMNS}, "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "reasons": json.dumps(kpi["reasons"], ensure_ascii=False)})
    rows.sort(key=lambda r: r["mail_date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        w.writeheader()
        w.writerows(rows)


# ─── DB・取得 ───
def _quoted(values) -> str:
    return ",".join('"' + v.replace('"', "") + '"' for v in values)


def lookup_collected(client, urls: list) -> dict:
    """第1段: 出典URL(複数可)が収集済みか。収集済みなら、最も高い重要度ランクと、初回収集日時を返す"""
    rows = client.select("article_urls", {"select": "article_url_id,article_url,first_detected_at",
                                          "article_url": f"in.({_quoted(sorted({v for u in urls for v in url_variants(u)}))})"})
    if not rows:
        return {"found": False}
    ids = ",".join(r["article_url_id"] for r in rows)
    arts = client.select("articles", {"select": "article_id,crawl_target_id", "article_url_id": f"in.({ids})", "is_current": "eq.true"})
    analyses = []
    if arts:
        aids = ",".join(a["article_id"] for a in arts)
        analyses = client.select("article_analysis", {"select": "article_id,importance_level,analysis_status", "article_id": f"in.({aids})", "is_current": "eq.true"})
    best = best_analysis(analyses)
    first = min((_parse_ts(r["first_detected_at"]) for r in rows if r.get("first_detected_at")), default=None)
    return {"found": True, "has_article": bool(arts), "level": best["level"], "status": best["status"], "first_detected_at": first,
            "matched_url": rows[0]["article_url"]}


def best_analysis(analyses: list) -> dict:
    """複数の分析(複数のURL・版)がある場合、最も高い重要度ランクのものを採る。ランクが無ければ、最初のものの状態を返す"""
    if not analyses:
        return {"level": None, "status": None}
    top = max(analyses, key=lambda a: RANK_ORDER.get(a.get("importance_level"), 0))
    return {"level": top.get("importance_level"), "status": top.get("analysis_status")}


_STOP_FREE = set(_STOPWORDS) | {"the", "and", "for", "with", "from", "that", "this", "its", "are", "was", "has", "have", "new", "says", "https",
                                "www", "com", "html", "htm", "news", "press", "release", "releases", "en", "ja", "jp"}
_WINDOW_CACHE = {}


def lang_free_tokens(text: str) -> set:
    """言語に依存しない、照合用の特徴語。英字の語(固有名詞・略称。小文字)、数字(2桁以上)、日本語の2文字の並び。
    日本語⇔英語の記事は、英字の固有名詞・略称・数字で、日本語⇔日本語の記事は、文字の並びで、つながる"""
    text = text or ""
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]*", text):
        w = w.lower().strip("-")
        if len(w) >= 2 and w not in _STOP_FREE:
            out.add(w)
    for n in re.findall(r"\d[\d,\.]*\d|\d", text):
        n = n.replace(",", "")
        if len(n) >= 2:
            out.add("#" + n)
    for seq in re.findall(r"[ァ-ヶー]{2,}|[一-龥々]{2,}|[ぁ-ん]{3,}", text):
        out.update("~" + seq[i:i + 2] for i in range(len(seq) - 1))
    return out


def _fetch_all(client, table: str, params: dict) -> list:
    rows, off = [], 0
    while True:
        batch = client.select(table, {**params, "limit": "1000", "offset": str(off)})
        rows += batch
        if len(batch) < 1000:
            return rows
        off += 1000


def _window_docs(client, lo: str, hi: str) -> tuple:
    """収集日時が[lo, hi)の記事(タイトル・URL・要約・重要度)と、語の希少さ(IDF)。同じ窓は、キャッシュする"""
    key = (lo, hi)
    if key not in _WINDOW_CACHE:
        arts = _fetch_all(client, "articles", {"select": "article_id,title,fetched_url,fetched_at", "is_current": "eq.true",
                                               "and": f"(fetched_at.gte.{lo},fetched_at.lt.{hi})", "order": "fetched_at"})
        an_hi = (date.fromisoformat(hi) + timedelta(days=10)).isoformat()
        an = {a["article_id"]: a for a in _fetch_all(client, "article_analysis", {"select": "article_id,summary_short,importance_level", "is_current": "eq.true",
                                                                                   "and": f"(created_at.gte.{lo},created_at.lt.{an_hi})", "order": "created_at"})}
        docs = []
        for a in arts:
            x = an.get(a["article_id"], {})
            path = urlparse(a.get("fetched_url") or "").path.replace("-", " ").replace("/", " ")
            docs.append({"art": a, "level": x.get("importance_level"), "tokens": lang_free_tokens(f"{a.get('title') or ''} {x.get('summary_short') or ''} {path}")})
        df = Counter(t for d in docs for t in d["tokens"])
        n = len(docs)
        _WINDOW_CACHE[key] = (docs, {t: math.log(n / (1 + v)) for t, v in df.items()}, n)
    return _WINDOW_CACHE[key]


def find_event_candidates(client, article: dict, src_date) -> list:
    """第2段(事象一致・言語に依存しない): ニューラルの記事(日本語タイトル・要約・出典タイトル・出典URL)と、同じ時期に収集した記事を、
    語の希少さで重みづけした重なりで比べ、似ている順に返す(類似度EVENT_SHOW_SCORE以上、最大5件)。
    日本語⇔英語でも、英字の固有名詞・略称・数字でつながる。同名記事(ニューラル媒体)は、日本語タイトルの重なりで拾う。
    候補は、目視で確認する前提(類似度EVENT_MIN_SCORE以上で、重要度C以上のとき、捕捉に数える)"""
    base = src_date or date.fromisoformat(article["date"])
    lo_d = base - timedelta(days=3)
    lo_week = (lo_d - timedelta(days=lo_d.weekday())).isoformat()          # 同じ週の記事は、同じ窓を使い回す
    hi = (date.fromisoformat(article["mail"]) + timedelta(days=7)).isoformat()
    try:
        docs, idf, n = _window_docs(client, lo_week, hi)
    except Exception:  # noqa: BLE001
        return []
    if not docs:
        return []
    path = urlparse(article.get("url") or "").path.replace("-", " ").replace("/", " ")
    q = lang_free_tokens(f"{article.get('title') or ''} {(article.get('summary') or '')[:300]} {article.get('src_title') or ''} {path}")
    q_score = sum(idf.get(t, math.log(max(n, 2))) for t in q)
    if q_score <= 0:
        return []
    own_urls = {ac.normalize_url(u) for u in (article.get("urls") or [])}
    scored = []
    for d in docs:
        a = d["art"]
        if (a.get("fetched_at") or "")[:10] < lo_d.isoformat() or ac.normalize_url(a.get("fetched_url") or "") in own_urls:
            continue
        shared = q & d["tokens"]
        if not shared:
            continue
        d_score = sum(idf[t] for t in d["tokens"])
        score = sum(idf[t] for t in shared) / math.sqrt(q_score * d_score) if d_score > 0 else 0
        if score >= EVENT_SHOW_SCORE:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return [{"media": urlparse(d["art"].get("fetched_url") or "").netloc.lower().replace("www.", ""), "url": d["art"].get("fetched_url") or "",
             "title": (d["art"].get("title") or "")[:60], "level": d["level"], "score": round(score, 3), "first_detected_at": d["art"].get("fetched_at") or ""}
            for score, d in scored[:5]]


def _is_own_media(media: str, rules: dict) -> bool:
    return any(d and d in (media or "") for d in (rules.get("own_media_domains") or DEFAULT_OWN_MEDIA))


def live_fetch_ok(url: str, proxies, verify) -> tuple:
    """いまの取得手段(通常→TinyFish Fetch。Agent等の有料の手段は使わない)で、記事を取得できるか。(可否, 失敗理由)"""
    err = None
    for tier in (None, "tinyfish_fetch"):
        try:
            r = ac.extract_article(url, proxies, verify, tier=tier)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if r.get("ok") and len(r.get("text") or "") >= 200 and not r.get("encoding_suspect"):
            return True, None
        err = r.get("error") or "本文が短い"
    return False, str(err)[:120]


# ─── 実行 ───
def check_mail(client, articles: list, targets: list, rules: dict, proxies, verify, live: bool = True) -> list:
    """1通分の記事を突合して、行(dict)のリストを返す"""
    _WINDOW_CACHE.clear()
    by_host = {}
    for t in targets:
        d = (t.get("domain") or urlparse(t.get("target_url") or "").netloc).lower().replace("www.", "")
        if d:
            by_host.setdefault(d, []).append(t)
    rows = []
    for a in articles:
        src_date = source_date_from_url(a["url"]) if a["url"] else None
        src_date_basis = "URLの日付" if src_date else "メールの日付(目安)"
        src_date = src_date or (date.fromisoformat(a["date"]) if a["date"] else None)
        row = {**a, "src_date": str(src_date or ""), "src_date_basis": src_date_basis, "scope": "枠内", "stage1": "", "stage2": "",
               "reach_rank": "", "judgement": "", "reason_code": "", "reason": "", "fix_owner": "", "days_ahead": None,
               "first_detected_at": "", "live_ok": None, "live_error": "", "alt_indep": None, "alt_own": None}
        exc = exclusion_reason(a, src_date, rules)
        if exc:
            row.update(judgement=exc[0], reason=exc[1], scope="枠外" if exc[0] == J_EXCL_SCOPE else "枠内")
            rows.append(row)
            continue
        hit = lookup_collected(client, a["urls"]) if a["urls"] else {"found": False}
        if hit["found"]:
            row["stage1"] = "URL一致"
            row["reach_rank"], row["judgement"] = rank_judgement(hit["level"], hit["status"])
            if hit.get("first_detected_at"):
                row["first_detected_at"] = hit["first_detected_at"].astimezone().strftime("%Y-%m-%d %H:%M")
                row["days_ahead"] = (date.fromisoformat(a["mail"]) - hit["first_detected_at"].astimezone().date()).days
            if row["judgement"] != J_CAPTURED:
                row["reason"] = {J_D: "重要度判定でDになった(選定落ち)", J_FILTERED: "フィルタ除外になった", J_UNANALYZED: "未分析"}[row["judgement"]]
                row["fix_owner"] = "重要度判定・フィルタ"
        else:
            row["stage1"] = "URL不一致"
            cands = find_event_candidates(client, a, src_date)
            if cands:
                row["stage2"] = "; ".join(f"{c['media']}({c['level'] or '空'},{c.get('score', '')}): {c['title']}" for c in cands[:3])
            strong = [c for c in cands if c.get("score", 1.0) >= EVENT_MIN_SCORE and c["level"] in RANK_REACHED]
            best = lambda cs: max(cs, key=lambda c: (RANK_ORDER[c["level"]], c.get("score", 1.0))) if cs else None
            own = [c for c in strong if _is_own_media(c["media"], rules)]
            indep = [c for c in strong if not _is_own_media(c["media"], rules)]
            row["alt_indep"], row["alt_own"] = best(indep), best(own)
            if indep:                                           # 独立した媒体で、同じ事象を収集している
                row["judgement"], row["reach_rank"] = J_EVENT, row["alt_indep"]["level"]
            elif own:                                           # ニューラル自身の媒体(同名記事)だけで、収集している
                row["judgement"], row["reach_rank"] = J_EVENT_OWN, row["alt_own"]["level"]
            else:
                row["judgement"] = J_MISSED
        if row["judgement"] in (J_MISSED, J_EVENT, J_EVENT_OWN):
            host = urlparse(a["url"]).netloc.lower().replace("www.", "")
            tgt = [t for d, ts in by_host.items() if d in host for t in ts]       # クローラーと同じく、domainがホストに含まれるか
            live_ok, live_err = (None, "")
            if live and a["url"]:
                live_ok, live_err = live_fetch_ok(a["url"], proxies, verify)
            row["live_ok"], row["live_error"] = live_ok, live_err or ""
            runs = None
            if tgt and src_date and row["judgement"] in (J_MISSED, J_EVENT_OWN):
                lo, hi = (src_date + timedelta(days=CRAWL_WINDOW_DAYS[0])), (src_date + timedelta(days=CRAWL_WINDOW_DAYS[1]))
                try:
                    runs = len(client.select("crawl_logs", {"select": "crawl_log_id", "crawl_target_id": f"eq.{tgt[0]['crawl_target_id']}",
                                                            "and": f"(started_at.gte.{lo.isoformat()},started_at.lt.{hi.isoformat()})"}))
                except Exception:  # noqa: BLE001
                    runs = None
            if row["judgement"] in (J_MISSED, J_EVENT_OWN):         # URLでは未収集の記事。クロール改善の手がかりとして、原因を残す
                code = classify_miss(a, tgt, src_date, live_ok, runs)
                row["reason_code"] = code
                row["reason"], row["fix_owner"] = REASONS[code]
                row["target"] = ", ".join(f"{t['target_name']}[{t['crawl_method']}]" for t in tgt[:2])
        rows.append(row)
    return rows


def write_excel(rows: list, kpi: dict, path: Path) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    hdr, fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="305496")

    def style(ws, widths):
        for c in ws[1]:
            c.font, c.fill, c.alignment = hdr, fill, Alignment(wrap_text=True, vertical="center")
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "サマリー"
    ws.append(["項目", "値", "説明"])
    pct = lambda x: f"{x:.1%}"
    for k, v, e in [
        ("メールの日付", kpi["mail_date"], ""), ("掲載記事数", kpi["total"], ""),
        ("分母除外: クロール開始前", kpi["excluded_pre_crawl"], "出典の公表日がクロール開始前"), ("分母除外: 枠外", kpi["excluded_out_of_scope"], "neural_check_rules.json の機械的な基準"),
        ("分母", kpi["denominator"], "掲載記事数 − 分母除外"), ("捕捉(URL一致・C以上)", kpi["captured_strict"], "到達点＝重要度C以上"),
        ("別媒体で捕捉(候補・要目視)", kpi["captured_event_candidate"], "第2段(事象一致)の候補。目視で確認する"),
        ("ニューラル媒体で捕捉(Sustainable Japan等)", kpi["captured_own_media"], "ニューラルの掲載記事と同名の記事を、収集している(ニューラル自身の媒体。KPIに数えるかは、PMOと確認)"),
        ("カバレッジ(厳密)", pct(kpi["coverage_strict"]), "合格90%・目標95%（確定前の案）"), ("カバレッジ(事象候補を含む)", pct(kpi["coverage_with_event"]), "URL一致＋独立媒体の事象一致"),
        ("カバレッジ(ニューラル媒体を含む)", pct(kpi["coverage_with_own_media"]), "上に、ニューラル媒体の同名記事を加えた値"),
        ("収集したが選定落ち(D)", kpi["collected_but_D"], "重要度判定の見直し候補"), ("収集したがフィルタ除外・未分析", kpi["collected_but_filtered"], "フィルタの見直し候補"),
        ("未収集", kpi["not_collected"], "クロールの見直し候補（原因別は下表・原因別シート）"),
        ("鮮度: 平均の先行日数", kpi["avg_days_ahead"], f"目標＝平均3日以上先行。収集日時で代用(Notion掲載日時は未連携)。n={kpi['freshness_n']}"),
        ("クロールの完走率(直近9日の平均)", "" if kpi.get("crawl_completeness") is None else pct(kpi["crawl_completeness"]),
         "毎日、処理できた対象数 / クロール対象数の平均。低い週は、クロール運用が、未捕捉の原因になっている")]:
        ws.append([k, v, e])
    ws.append([])
    ws.append(["未収集の原因", "件数", "修正先"])
    for code, n in sorted(kpi["reasons"].items(), key=lambda kv: -kv[1]):
        ws.append([REASONS[code][0], n, REASONS[code][1]])
    style(ws, [40, 22, 70])

    ws2 = wb.create_sheet("突合シート")
    cols = ["メール日", "No", "テーマ", "ニューラルのタイトル", "出典URL(先頭)", "出典URL数", "出典公表日", "公表日の根拠", "枠内・枠外", "第1段(URL一致)",
            "第2段(事象一致・媒体名)", "到達ランク", "判定", "未捕捉の理由コード", "未捕捉の理由", "修正先", "初回収集日時", "先行日数", "いま取得できるか", "取得できない理由", "クロール先",
            "代替(独立媒体)", "代替の重要度", "代替の類似度", "代替(ニューラル媒体)の重要度", "代替(ニューラル媒体)のURL", "代替(ニューラル媒体)の収集日時"]
    ws2.append(cols)
    for r in rows:
        ws2.append([r["mail"], r["no"], r["theme"], r["title"], r["url"], len(r["urls"]), r["src_date"], r["src_date_basis"], r["scope"], r["stage1"],
                    r["stage2"], r["reach_rank"], r["judgement"], r["reason_code"], r["reason"], r["fix_owner"], r["first_detected_at"],
                    "" if r["days_ahead"] is None else r["days_ahead"], {True: "○", False: "×", None: ""}[r["live_ok"]], r["live_error"], r.get("target", ""),
                    (r["alt_indep"] or {}).get("url", ""), (r["alt_indep"] or {}).get("level", ""), (r["alt_indep"] or {}).get("score", ""),
                    (r["alt_own"] or {}).get("level", ""), (r["alt_own"] or {}).get("url", ""), (r["alt_own"] or {}).get("first_detected_at", "")])
    style(ws2, [11, 5, 16, 50, 50, 8, 12, 14, 9, 12, 50, 10, 24, 16, 40, 34, 16, 8, 10, 40, 36, 40, 9, 9, 12, 40, 18])
    ws2.auto_filter.ref = ws2.dimensions
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ニューラル週次メールとの突合(週次パフォーマンスチェック)")
    ap.add_argument("xlsx", nargs="+", help="ニューラルの週次レポート(xlsx)。ファイル名に YYYY-MM-DD を含む")
    ap.add_argument("--out", default="logs/neural_check", help="出力先(既定 logs/neural_check)")
    ap.add_argument("--rules", default="neural_check_rules.json", help="分母の除外ルール(JSON)")
    ap.add_argument("--no-live", action="store_true", help="未捕捉の記事の、実際の取得テストをしない")
    args = ap.parse_args(argv)

    cfg = ac.load_config()
    client = ac.SupabaseClient(cfg)
    proxies, verify = ac.make_proxies(cfg), cfg.get("ssl", {}).get("verify", True)
    rules = load_rules(args.rules)
    targets = client.select("crawl_targets", {"select": "crawl_target_id,target_name,target_url,domain,crawl_method,endpoint_type,include_paths,created_at"})
    out = Path(args.out)
    for path in args.xlsx:
        articles = read_neural_xlsx(path)
        mail_date = articles[0]["mail"]
        print(f"[{mail_date}] 掲載記事 {len(articles)}件を突合します…", flush=True)
        rows = check_mail(client, articles, targets, rules, proxies, verify, live=not args.no_live)
        active = sum(1 for t in targets if t.get("crawl_method") not in DISABLED_METHODS)
        kpi = compute_kpis(rows, mail_date, crawl_completeness(client, mail_date, active))
        write_excel(rows, kpi, out / f"突合_{mail_date}.xlsx")
        (out / f"check_{mail_date}.json").write_text(json.dumps({"run_at": datetime.now().isoformat(timespec="seconds"), "kpi": kpi, "rules": rules,
                                                                  "rows": [{**r, "urls": r["urls"]} for r in rows]}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        append_history(out / "history.csv", kpi)
        print(f"  分母{kpi['denominator']}件(除外: クロール開始前{kpi['excluded_pre_crawl']}・枠外{kpi['excluded_out_of_scope']}) / 捕捉{kpi['captured_strict']}件 "
              f"= カバレッジ{kpi['coverage_strict']:.1%}(事象候補を含む{kpi['coverage_with_event']:.1%}・ニューラル媒体も含む{kpi['coverage_with_own_media']:.1%}) / 先行日数の平均 {kpi['avg_days_ahead']} / クロール完走率 {kpi['crawl_completeness']}", flush=True)
        print(f"  収集したがD {kpi['collected_but_D']} / フィルタ除外・未分析 {kpi['collected_but_filtered']} / 未収集 {kpi['not_collected']}: {kpi['reasons']}", flush=True)
    print(f"出力: {out.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
