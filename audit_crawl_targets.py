"""クロール先(crawl_targets)の登録の不整合を検査する（読み取り専用。DBは変更しない）。

背景（2026-10-23）: 方式や種別を変えたときに、include_paths等の更新が漏れ、半年近く記事が0件のまま
気づかれない対象が、14対象あった。毎日のクロールでは「更新なし」と区別できないため、登録そのものを検査する。

使い方:  python audit_crawl_targets.py
検査項目:
  FEED_TYPE_NOT_RSS      種別がRSSでフィードのURLなのに、方式がRSSではない（コードは、フィードとして読むようになっているが、登録も直す）
  RSS_TYPE_BUT_HTML_URL  種別がRSSなのに、URLがフィードの形ではない（HTMLの一覧なら、種別を「ニュース・一覧ページ」に）
  FEED_INCLUDE_ON_HTML   HTML一覧なのに、include_pathsがフィードのパス（/feed/、/rss/…）で、記事のリンクに一致しない
  NO_ARTICLES_30D        直近30日の記事が0件（有効な方式の対象。サイト側が更新されていないだけの場合もある）
"""
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from article_crawler import LISTING_ENDPOINT_TYPES, SupabaseClient, _is_feed_target, load_config

ACTIVE_METHODS = ("RSS", "HTML", "ブラウザ操作", "Zyte", "JSON API", "API")
LISTING_METHODS = ("HTML", "ブラウザ操作", "Zyte")
_FEED_LIKE = re.compile(r"(rss|feed|atom|\.xml)", re.IGNORECASE)


def find_issues(targets: list, articles_30d: dict = None) -> list:
    """登録の不整合を、[{code, severity, target, method, message, fix}]で返す（純粋関数）。
    articles_30dは {crawl_target_id: 直近30日の記事数}。Noneなら、NO_ARTICLES_30Dは検査しない"""
    out = []
    for t in targets:
        method, etype = t.get("crawl_method"), t.get("endpoint_type") or ""
        name = t.get("target_name") or ""

        def add(code, severity, message, fix):
            out.append({"code": code, "severity": severity, "target": name, "method": method,
                        "message": message, "fix": fix, "id": t.get("crawl_target_id")})

        if method in ACTIVE_METHODS and method != "RSS" and etype == "RSS":
            if _is_feed_target(t):
                add("FEED_TYPE_NOT_RSS", "info", f"種別RSS・フィードURLなのに、方式が{method}", "方式をRSSに戻す")
            else:
                add("RSS_TYPE_BUT_HTML_URL", "warn", f"種別RSSなのに、URLがフィードの形ではない({t.get('target_url')})",
                    "種別を「ニュース・一覧ページ」に直す")
        if method in LISTING_METHODS and etype in LISTING_ENDPOINT_TYPES and _FEED_LIKE.search(t.get("include_paths") or ""):
            add("FEED_INCLUDE_ON_HTML", "error",
                f"HTML一覧なのに、include_pathsがフィード形式({t.get('include_paths')})。一覧のリンクに一致せず、記事が0件になる",
                "include_pathsを、記事のURLの規則に合わせて直す")
        if articles_30d is not None and method in ACTIVE_METHODS and articles_30d.get(t.get("crawl_target_id"), 0) == 0:
            add("NO_ARTICLES_30D", "warn", "直近30日の記事が0件", "一覧・条件・方式を確認する（サイト側の更新が無いだけの場合もある）")
    return out


def main():
    client = SupabaseClient(load_config())
    targets = client.select("crawl_targets", {"select": "crawl_target_id,target_name,target_url,crawl_method,endpoint_type,include_paths"})
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    counts = Counter(r["crawl_target_id"] for r in client.select("articles", {"select": "crawl_target_id", "fetched_at": f"gte.{since}"}))
    issues = find_issues(targets, counts)
    print(f"検査した対象 {len(targets)}件 / 指摘 {len(issues)}件: " + str(dict(Counter(i['code'] for i in issues))))
    for code in ("FEED_INCLUDE_ON_HTML", "RSS_TYPE_BUT_HTML_URL", "FEED_TYPE_NOT_RSS", "NO_ARTICLES_30D"):
        rows = [i for i in issues if i["code"] == code]
        if not rows:
            continue
        print(f"\n■ {code}（{len(rows)}件）")
        for i in rows:
            print(f"  [{i['method']}] {i['target'][:34]:34} {i['message'][:80]}  → {i['fix']}")


if __name__ == "__main__":
    main()
