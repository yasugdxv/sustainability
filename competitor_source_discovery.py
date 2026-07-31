"""
競合サステナビリティモニタリング: クロール候補URL自動発見モジュール

competitor_sources のうち source_type='SUSTAINABILITY_HOME'（サステナビリティ起点ページ）を
クロールした際に、同ページ配下の内部リンクを解析し、目標(TARGETS)・実績(PROGRESS)・
レポート(REPORTS)・ニュース(NEWS)・開示(DISCLOSURE)に該当しそうなページを候補として抽出する。

自動抽出した候補は即座に本番監視対象(competitor_sources)へ昇格させず、
competitor_source_candidates に保存し、レビュー画面（sustainability_expert_dashboard.py）から
人が確認・種別修正のうえで有効化(activate_candidate)して初めてクロール対象になる。

種別推測(guess_url_type)はLLMを使わず、URL・タイトルのキーワードによる簡易判定とする
（人がレビュー画面で修正できるため、厳密な精度は求めない）。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from lxml import html as lxml_html

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, list_html_candidates  # noqa: E402

REQUEST_TIMEOUT = 15
MAX_TITLE_FETCH = 15  # 1情報源あたりタイトル取得を試みる候補の上限（相手サーバへの配慮）
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# 明らかに監視対象にならない定型ページは、種別判定を試みる前に除外する
EXCLUDE_KEYWORDS = (
    "cookie", "privacy", "age-gate", "agegate", "terms", "login", "signin",
    "career", "jobs", "contact", "search", "sitemap", "accessibility",
)

TYPE_KEYWORDS = {
    "REPORTS": ("report", "databook", "annual-report", "annualreport", ".pdf"),
    "TARGETS": ("target", "commitment", "goal", "net-zero", "netzero", "pledge"),
    "PROGRESS": ("progress", "performance", "results", "achievement"),
    "DISCLOSURE": ("esg-rating", "rating", "disclosure", "cdp", "djsi", "score"),
    "NEWS": ("news", "press", "media", "stories", "insights", "newsroom"),
}


def guess_url_type(url: str, title: str = None) -> str:
    """URL・タイトルのキーワードから種別を推測する（純粋関数）。該当なしはOTHER"""
    text = f"{url} {title or ''}".lower()
    for url_type, keywords in TYPE_KEYWORDS.items():
        if any(k in text for k in keywords):
            return url_type
    return "OTHER"


def _is_excluded(url: str) -> bool:
    lowered = url.lower()
    return any(k in lowered for k in EXCLUDE_KEYWORDS)


def fetch_title(url: str, proxies: dict, verify: bool) -> str:
    """<title>タグだけを軽量に取得する（本文全体は抽出しない）"""
    try:
        resp = requests.get(url, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT,
                             headers=_HEADERS)
        resp.raise_for_status()
    except Exception:
        return None
    try:
        tree = lxml_html.fromstring(resp.content)
        titles = tree.xpath("//title/text()")
        return titles[0].strip() if titles else None
    except Exception:
        return None


def upsert_candidate(client: SupabaseClient, *, company_id: str, discovered_from_source_id: str,
                      url: str, url_type_candidate: str, page_title: str, is_crawlable: bool) -> None:
    existing = client.select("competitor_source_candidates", {
        "select": "candidate_id,is_active", "company_id": f"eq.{company_id}", "url": f"eq.{url}",
    })
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        if existing[0]["is_active"]:
            return  # 既に有効化済みの候補は上書きしない
        client.update("competitor_source_candidates", {"candidate_id": f"eq.{existing[0]['candidate_id']}"}, {
            "page_title": page_title, "url_type_candidate": url_type_candidate,
            "is_crawlable": is_crawlable, "last_checked_at": now,
        })
    else:
        client.insert("competitor_source_candidates", [{
            "company_id": company_id, "discovered_from_source_id": discovered_from_source_id,
            "url": url, "url_type_candidate": url_type_candidate, "page_title": page_title,
            "is_crawlable": is_crawlable, "last_checked_at": now,
        }], prefer="return=minimal")


def discover_candidates(client: SupabaseClient, proxies: dict, verify: bool,
                         company: dict, source: dict) -> int:
    """sourceがSUSTAINABILITY_HOMEの場合のみ呼び出す想定。発見・保存した候補数を返す"""
    candidates, _status = list_html_candidates({"target_url": source["source_url"]}, proxies, verify)

    plausible = []
    for c in candidates:
        if _is_excluded(c["url"]):
            continue
        url_type = guess_url_type(c["url"])
        if url_type != "OTHER":
            plausible.append((c["url"], url_type))

    saved = 0
    for url, url_type in plausible[:MAX_TITLE_FETCH]:
        title = fetch_title(url, proxies, verify)
        upsert_candidate(
            client, company_id=company["company_id"], discovered_from_source_id=source["source_id"],
            url=url, url_type_candidate=url_type, page_title=title, is_crawlable=title is not None,
        )
        saved += 1
    return saved


def list_candidates(client: SupabaseClient, is_active: bool = None) -> list:
    params = {"select": "*", "order": "last_checked_at.desc"}
    if is_active is not None:
        params["is_active"] = f"eq.{str(is_active).lower()}"
    return client.select("competitor_source_candidates", params)


def activate_candidate(client: SupabaseClient, candidate_id: str, url_type: str,
                        crawl_method: str = "HTML", lookback_days: int = 90) -> dict:
    rows = client.select("competitor_source_candidates", {"candidate_id": f"eq.{candidate_id}", "limit": "1"})
    candidate = rows[0] if rows else None
    if candidate is None:
        return {"ok": False, "error": "candidate_idが見つかりません"}

    new_source = client.insert("competitor_sources", [{
        "company_id": candidate["company_id"], "source_url": candidate["url"],
        "source_type": url_type, "crawl_method": crawl_method, "lookback_days": lookback_days,
        "notes": f"競合クロール候補管理より有効化（発見元: {candidate.get('discovered_from_source_id')}）",
    }])[0]

    client.update("competitor_source_candidates", {"candidate_id": f"eq.{candidate_id}"}, {
        "is_active": True, "promoted_source_id": new_source["source_id"],
        "url_type_candidate": url_type,
    })
    return {"ok": True, "source": new_source}


def deactivate_candidate(client: SupabaseClient, candidate_id: str) -> dict:
    rows = client.select("competitor_source_candidates", {"candidate_id": f"eq.{candidate_id}", "limit": "1"})
    candidate = rows[0] if rows else None
    if candidate is None:
        return {"ok": False, "error": "candidate_idが見つかりません"}

    if candidate.get("promoted_source_id"):
        client.delete("competitor_sources", {"source_id": f"eq.{candidate['promoted_source_id']}"})

    client.update("competitor_source_candidates", {"candidate_id": f"eq.{candidate_id}"}, {
        "is_active": False, "promoted_source_id": None,
    })
    return {"ok": True}
