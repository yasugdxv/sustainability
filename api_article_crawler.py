"""
crawl_method='API'のうち、個別の記事・通知単位で内容を持つソース（②グループ）の取得モジュール。

①グループ（UN_SC/OFAC_SDN/ECFR_TITLES＝reference_list_monitor.py）は
「エンティティ一覧のスナップショット差分検知」だったのに対し、こちらは
1件1件が記事のように読める通知（規則の告示・食品リコール・食品アラート・
製品安全アラート）なので、articles/article_urlsに記事として保存し、
既存のarticle_analyzer.pyのタグ付け・重要度判定パイプラインにそのまま乗せる。

通常のRSS/HTML/ブラウザ操作とは異なりAPIが構造化データを返すため、
article_crawler.pyのextract_article()（HTML本文抽出）は使わず、
各APIのレスポンスから直接タイトル・本文・日付を組み立ててsave_article()に渡す。

対象（crawl_targets.target_name）:
    Federal Register API           米国EPA/FDA/FTC/TTB等の規則・告示・意見募集
    openFDA Food Enforcement API   米国FDA食品リコール
    UK FSA Food Alerts API         英国食品アラート・リコール
    EU Safety Gate（非食品製品安全） EU非食品製品の危険製品アラート
    Regulations.gov API            米国EPA/FDA/FTCの規則案・パブコメDocket（要APIキー）

使い方:
    python api_article_crawler.py                  # 全ソース
    python api_article_crawler.py federal_register  # 指定ソースのみ
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config, make_proxies, save_article, normalize_url  # noqa: E402

REQUEST_TIMEOUT = 60
DEFAULT_LOOKBACK_DAYS = 7


def _target_by_name(client: SupabaseClient, name: str) -> dict:
    rows = client.select("crawl_targets", {"select": "*", "target_name": f"eq.{name}"})
    if not rows:
        raise RuntimeError(f"crawl_targetsに「{name}」が見つかりません")
    return rows[0]


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Federal Register（EPA/FDA/FTC/TTBの規則・告示） ──────────────────
FEDERAL_REGISTER_AGENCIES = [
    "environmental-protection-agency", "food-and-drug-administration",
    "federal-trade-commission", "alcohol-and-tobacco-tax-and-trade-bureau",
]


def fetch_federal_register(proxies: dict, verify: bool, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    params = [
        ("conditions[type][]", "RULE"), ("conditions[type][]", "PRORULE"), ("conditions[type][]", "NOTICE"),
        ("conditions[publication_date][gte]", since),
        ("order", "newest"), ("per_page", 200),
    ]
    for agency in FEDERAL_REGISTER_AGENCIES:
        params.append(("conditions[agencies][]", agency))
    for field in ("title", "abstract", "html_url", "publication_date", "agencies", "type", "document_number"):
        params.append(("fields[]", field))

    resp = requests.get("https://www.federalregister.gov/api/v1/documents.json",
                         params=params, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    items = []
    for d in resp.json().get("results", []):
        agencies = "、".join(a["name"] for a in d.get("agencies") or [])
        items.append({
            "url": d["html_url"],
            "title": d.get("title"),
            "published_at": d.get("publication_date"),
            "text": f"[{d.get('type')}] {agencies}\n\n{d.get('abstract') or d.get('title') or ''}",
        })
    return items


# ─── openFDA Food Enforcement（米国FDA食品リコール） ──────────────────
def fetch_openfda_food(proxies: dict, verify: bool, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=lookback_days)).strftime("%Y%m%d")
    until = now.strftime("%Y%m%d")

    resp = requests.get("https://api.fda.gov/food/enforcement.json", params={
        "search": f"report_date:[{since} TO {until}]",
        "sort": "report_date:desc", "limit": 100,
    }, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return []  # openFDAは該当期間0件の場合404を返す
    resp.raise_for_status()

    items = []
    for r in resp.json().get("results", []):
        report_date = r.get("report_date") or ""
        pub_iso = f"{report_date[0:4]}-{report_date[4:6]}-{report_date[6:8]}" if len(report_date) == 8 else None
        # event_idは同一リコール事象内の複数レコード（対象地域・製品別等）で共有されることがあり、
        # 一意キーには使えない（実測: 33件中12件しか重複しなかった）。recall_numberは1レコード1件で一意。
        recall_number = r.get("recall_number") or r.get("event_id")
        items.append({
            "url": f"https://api.fda.gov/food/enforcement.json?recall_number={recall_number}",
            "title": f"{r.get('recalling_firm') or '不明企業'}のリコール: {(r.get('product_description') or '')[:80]}",
            "published_at": pub_iso,
            "text": (f"分類: {r.get('classification')}\n"
                     f"企業: {r.get('recalling_firm')}\n"
                     f"製品: {r.get('product_description')}\n"
                     f"リコール理由: {r.get('reason_for_recall')}\n"
                     f"対象地域: {r.get('distribution_pattern')}\n"
                     f"状態: {r.get('status')}"),
        })
    return items


# ─── UK FSA Food Alerts（英国食品アラート） ───────────────────────────
def fetch_uk_fsa_alerts(proxies: dict, verify: bool, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    resp = requests.get("https://data.food.gov.uk/food-alerts/id", params={
        "_view": "full", "_limit": 50, "_sort": "-modified",
    }, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
    resp.raise_for_status()

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    items = []
    for it in resp.json().get("items", []):
        ref_dt = _parse_iso(it.get("modified")) or _parse_iso(it.get("created"))
        if ref_dt and ref_dt < cutoff:
            break  # -modifiedで新しい順に並んでいるため、範囲外に出たら以降も範囲外
        text = "\n\n".join(p for p in (it.get("description"), it.get("actionTaken"), it.get("consumerAdvice")) if p)
        items.append({
            "url": it.get("alertURL") or it.get("@id"),
            "title": it.get("title") or it.get("shortTitle"),
            "published_at": it.get("created"),
            "text": text or it.get("title"),
        })
    return items


# ─── EU Safety Gate（非食品製品安全アラート） ─────────────────────────
def fetch_eu_safety_gate(proxies: dict, verify: bool, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    """公式APIは非公開だが、検索画面（screen/search）がPOST
    /public/api/notification/carousel/ を呼んでいるのを確認して使用。
    通知1件ごとの直接リンクは未確認のため、carousel APIへの参照URL
    （id付きアンカー）を仮のURLとして使う（記事重複判定キーとしては機能する）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    items = []
    page = 0
    while True:
        resp = requests.post(
            "https://ec.europa.eu/safety-gate-alerts/public/api/notification/carousel/",
            json={"language": "en", "page": str(page)},
            proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content") or []
        if not content:
            break

        stop = False
        for n in content:
            pub_dt = _parse_iso(n.get("publicationDate"))
            if pub_dt and pub_dt < cutoff:
                stop = True
                break
            product = n.get("product") or {}
            risk = n.get("risk") or {}
            risk_types = "、".join(rt.get("name", "") for rt in risk.get("riskType") or [])
            brands = "、".join(b.get("brand", "") for b in product.get("brands") or [])
            title = f"{product.get('name') or '製品'}（{brands or 'ブランド不明'}）－ {risk_types or 'リスクアラート'}"
            items.append({
                "url": f"https://ec.europa.eu/safety-gate-alerts/public/api/notification/carousel/?id={n['id']}",
                "title": title,
                "published_at": n.get("publicationDate"),
                "text": (f"参照番号: {n.get('reference')}\n"
                         f"リスク種別: {risk_types}\n"
                         f"製品: {product.get('name')}\n"
                         f"ブランド: {brands}"),
            })
        if stop or data.get("last", True):
            break
        page += 1
    return items


# ─── RASFF（EU食品・飼料早期警戒システム） ─────────────────────────────
_RASFF_CLASSIFICATION_ORDER = {"alert notification": 0, "information notification for attention": 1}


def fetch_rasff(proxies: dict, verify: bool, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    """公式APIは非公開だが、検索画面（screen/list）がPOST
    /backend/public/notification/search/consolidated/en/ を呼んでいるのを確認して使用。
    通知1件ごとの直接リンクは未確認のため、検索画面への参照URL（notifId付きクエリ）を
    仮のURLとして使う（記事重複判定キーとしては機能する）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    items = []
    page_number = 1
    while True:
        resp = requests.post(
            "https://webgate.ec.europa.eu/rasff-window/backend/public/notification/search/consolidated/en/",
            json={"parameters": {"pageNumber": page_number, "itemsPerPage": 50}},
            proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        notifications = data.get("notifications") or []
        if not notifications:
            break

        stop = False
        for n in notifications:
            pub_dt = None
            if n.get("ecValidationDate"):
                try:
                    pub_dt = datetime.strptime(n["ecValidationDate"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    pub_dt = None
            if pub_dt and pub_dt < cutoff:
                stop = True
                break
            origin = "、".join(c.get("organizationName", "") for c in n.get("originCountries") or [])
            items.append({
                "url": f"https://webgate.ec.europa.eu/rasff-window/screen/list?notifId={n['notifId']}",
                "title": n.get("subject") or n.get("reference"),
                "published_at": pub_dt.date().isoformat() if pub_dt else None,
                "text": (f"参照番号: {n.get('reference')}\n"
                         f"分類: {(n.get('notificationClassification') or {}).get('description')}\n"
                         f"リスク判定: {(n.get('riskDecision') or {}).get('description')}\n"
                         f"製品カテゴリ: {(n.get('productCategory') or {}).get('description')}\n"
                         f"通報国: {(n.get('notifyingCountry') or {}).get('organizationName')}\n"
                         f"原産国: {origin}"),
            })
        if stop or page_number >= (data.get("totalPages") or 1):
            break
        page_number += 1
    return items


# ─── Regulations.gov（米連邦官庁の規則案・パブコメDocket） ───────────────
# サステナ関連の官庁のみに絞る（全省庁対象だとデータ量・LLM分析コストが膨大になるため）
REGULATIONS_GOV_AGENCIES = ["EPA", "FDA", "FTC"]
# documentTypeを絞らないと過去の研究論文等"Supporting & Related Material"まで
# 大量に混入する（実測: EPA1エージェンシーだけで14日分の94%がこの種別）ため、
# Federal Register APIと同じ「規則・規則案・告知」のみに揃える
REGULATIONS_GOV_DOCUMENT_TYPES = "Rule,Proposed Rule,Notice"


def fetch_regulations_gov(proxies: dict, verify: bool, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    config = load_config()
    api_key = (config.get("api_keys") or {}).get("regulations_gov")
    if not api_key:
        raise RuntimeError("config.jsonのapi_keys.regulations_govが未設定です")

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    headers = {"X-Api-Key": api_key}

    items = []
    seen_ids = set()
    for agency in REGULATIONS_GOV_AGENCIES:
        page = 1
        while True:
            params = {
                "filter[postedDate][ge]": since,
                "filter[agencyId]": agency,
                "filter[documentType]": REGULATIONS_GOV_DOCUMENT_TYPES,
                "sort": "-postedDate",
                "page[size]": 250,
                "page[number]": page,
            }
            resp = requests.get("https://api.regulations.gov/v4/documents", params=params,
                                 headers=headers, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            body = resp.json()
            for d in body.get("data") or []:
                doc_id = d.get("id")
                if not doc_id or doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                attrs = d.get("attributes") or {}
                title = attrs.get("title") or doc_id
                items.append({
                    "url": f"https://www.regulations.gov/document/{doc_id}",
                    "title": title,
                    "published_at": attrs.get("postedDate"),
                    "text": (f"[{attrs.get('documentType')}] {attrs.get('agencyId')}\n\n{title}\n"
                             f"Docket: {attrs.get('docketId') or ''}"),
                })
            total_pages = (body.get("meta") or {}).get("totalPages", 1)
            if page >= total_pages:
                break
            page += 1
    return items


SOURCES = {
    "federal_register": ("Federal Register API", fetch_federal_register),
    "openfda_food": ("openFDA Food Enforcement API", fetch_openfda_food),
    "uk_fsa_alerts": ("UK FSA Food Alerts API", fetch_uk_fsa_alerts),
    "eu_safety_gate": ("EU Safety Gate（非食品製品安全）", fetch_eu_safety_gate),
    "rasff": ("RASFF Open Data API", fetch_rasff),
    "regulations_gov": ("Regulations.gov API", fetch_regulations_gov),
}


def save_items(client: SupabaseClient, target: dict, items: list) -> tuple:
    new_count = updated_count = unchanged_count = failed_count = 0
    for item in items:
        try:
            pub_dt = _parse_iso(item.get("published_at")) if item.get("published_at") else None
            if pub_dt is None and item.get("published_at"):
                try:
                    from dateutil import parser as dateutil_parser
                    pub_dt = dateutil_parser.isoparse(item["published_at"])
                except Exception:
                    pub_dt = None
            canonical_url = normalize_url(item["url"])
            is_new_url, is_new_version = save_article(
                client, target, canonical_url, item["url"], item["url"],
                item.get("title") or "", pub_dt, None, item.get("text") or "",
            )
            if is_new_url:
                new_count += 1
            elif is_new_version:
                updated_count += 1
            else:
                unchanged_count += 1
        except Exception as e:
            failed_count += 1
            print(f"    [失敗] {item.get('url')}: {type(e).__name__}: {e}")
    return new_count, updated_count, unchanged_count, failed_count


def main(sources: tuple = tuple(SOURCES.keys())):
    config = load_config()
    client = SupabaseClient(config)
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)

    for key in sources:
        target_name, fetcher = SOURCES[key]
        print(f"[{key}] 取得中...", flush=True)
        try:
            target = _target_by_name(client, target_name)
            lookback_days = target.get("lookback_days") or DEFAULT_LOOKBACK_DAYS
            items = fetcher(proxies, verify, lookback_days)
        except Exception as e:
            print(f"[{key}] 取得失敗: {type(e).__name__}: {e}")
            continue
        new_count, updated_count, unchanged_count, failed_count = save_items(client, target, items)
        print(f"[{key}] 総数{len(items)}件 "
              f"（新規{new_count} 更新{updated_count} 変更なし{unchanged_count} 失敗{failed_count}）")


if __name__ == "__main__":
    args = tuple(sys.argv[1:]) or tuple(SOURCES.keys())
    unknown = [a for a in args if a not in SOURCES]
    if unknown:
        print(f"未知のソース指定: {unknown}（対応: {tuple(SOURCES.keys())}）")
        sys.exit(1)
    main(args)
