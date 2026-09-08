"""
Sustainability Department Intelligence AI Phase B: Cross-domain Intelligence Gateway。

A側（Sustainabilityアプリ。Chat/Search両方）から他部門Intelligenceへアクセスする
唯一の共通入口。中身は現状Geopolitics 1ドメインのみに委譲するが、関数シグネチャは
Domain非依存にしてある（将来SCM/PA/ERM等を追加する際、この1ファイルに新しいadapterを
足すだけで済むようにするため）。

このGatewayは推論・統合・十分性判定を一切持たない（それらは呼び出し元＝
sustainability_chat_geo_service.pyに置く）。役割は「Kill Switchを確認した上で
取得して正規化するだけ」。Kill Switchはこのファイルが唯一の強制ポイントであり、
呼び出し元（Chat/Search）が確認を怠っても、Gateway自身がOFFなら常に安全側
（None/空リスト）を返す。

現在の実装はweekly_geo_intelligence_items（週次バッチで取得済みのアイテム）を
Existing Intelligence Retrievalの暫定Sourceとして使う。これは実装詳細であり、
将来Geo側に専用のIntelligence Library/検索APIができた場合はretrieve_existing_intelligence()
の中身だけを差し替えれば呼び出し側（Chat/Search）は無改修で済む。
"""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from geo_intelligence_schema import parse_response
from geo_intelligence_service import query_geo_intelligence

SUPPORTED_DOMAINS = ("geopolitics",)


@dataclass
class CrossDomainIntelligenceSource:
    """Exact Cache／Retrieved／Freshの3経路すべてをこの形に正規化する。
    存在しない項目はNoneのまま持つ（補完しない）。"""
    source_department: str          # "geopolitics"（将来 "scm"/"pa"/"erm" 等）
    source_agent: object            # str | None。実在する識別子のみ
    source_type: str                # "department_intelligence"
    title: object                   # str | None。Retrieved由来のみ判明することが多い
    assessment: object              # str | None。geo_assessment相当
    why_relevant: object
    political_dynamics: object
    outlook: object
    confidence: object              # str | float | None
    as_of: object                   # str | None
    references: list
    origin: str                     # "exact_cache" | "retrieved" | "fresh"
    source_item_id: object          # str | None。weekly_geo_intelligence_items.id
    external_call_id: object        # str | None。external_intelligence_calls.id
    department_intelligence: object = None  # dict | None。Geo側Phase B-B応答（存在する場合のみ）


# ─── Kill Switch（Gatewayが唯一の強制ポイント） ──────────────────────────────
def _is_geo_domain_enabled(config: dict, capability: str) -> bool:
    """判定ルール（この順で確定、以降は評価しない。明示設定は常にenv変数より優先する）:
    1. geo_intelligence.enabled が明示的にfalse → capabilityに関わらず強制OFF
    2. geo_intelligence.<capability>.enabled が明示的にtrue  → ON
    3. geo_intelligence.<capability>.enabled が明示的にfalse → OFF（envは見ない）
    4. geo_intelligence.<capability>.enabled が未設定        → SUSTAINABILITY_<CAPABILITY>_GEO_ENABLED
       環境変数を確認（true相当ならON、それ以外はOFF）
    capability: "chat" | "search"。親がtrueでも子が明示的にfalseならその子はOFFになる
    （例: enabled=true, chat.enabled=true, search.enabled=false → Chat ON / Search OFF）。"""
    geo_cfg = (config or {}).get("geo_intelligence") or {}
    if geo_cfg.get("enabled") is False:
        return False
    cap_cfg = geo_cfg.get(capability) or {}
    if cap_cfg.get("enabled") is True:
        return True
    if cap_cfg.get("enabled") is False:
        return False
    env_name = f"SUSTAINABILITY_{capability.upper()}_GEO_ENABLED"
    return os.environ.get(env_name, "").strip().lower() in ("1", "true", "yes")


# ─── 正規化アダプタ ──────────────────────────────────────────────────────
def _source_from_cached_row(row: dict) -> CrossDomainIntelligenceSource | None:
    """external_intelligence_calls の1行（response_payloadに生JSONを保持）から
    CrossDomainIntelligenceSourceを復元する。パース失敗時はNone（呼び出し元は
    Exact Cacheミスとして扱いフォールバックする）。"""
    try:
        response = parse_response(row.get("response_payload"))
    except Exception:
        return None
    return CrossDomainIntelligenceSource(
        source_department="geopolitics", source_agent="geo_intelligence",
        source_type="department_intelligence", title=None,
        assessment=response.geo_assessment, why_relevant=None,
        political_dynamics=response.political_dynamics, outlook=response.outlook,
        confidence=response.confidence, as_of=response.completed_at,
        references=response.references or [], origin="exact_cache",
        source_item_id=None, external_call_id=row.get("id"),
        department_intelligence=getattr(response, "department_intelligence", None),
    )


def _source_from_geo_item(item: dict) -> CrossDomainIntelligenceSource:
    """weekly_geo_intelligence_items の1行からCrossDomainIntelligenceSourceへ正規化する。"""
    return CrossDomainIntelligenceSource(
        source_department="geopolitics", source_agent="geo_intelligence",
        source_type="department_intelligence", title=item.get("title"),
        assessment=item.get("geo_assessment"), why_relevant=item.get("why_relevant"),
        political_dynamics=item.get("political_dynamics"), outlook=item.get("outlook"),
        confidence=item.get("confidence"), as_of=item.get("as_of") or item.get("event_date"),
        references=item.get("references") or [], origin="retrieved",
        source_item_id=item.get("id"), external_call_id=None,
    )


def _source_from_geo_response(response, external_call_id) -> CrossDomainIntelligenceSource:
    """新規Geo問い合わせのGeoQueryResponseからCrossDomainIntelligenceSourceへ正規化する。"""
    return CrossDomainIntelligenceSource(
        source_department="geopolitics", source_agent="geo_intelligence",
        source_type="department_intelligence", title=None,
        assessment=response.geo_assessment, why_relevant=None,
        political_dynamics=response.political_dynamics, outlook=response.outlook,
        confidence=response.confidence, as_of=response.completed_at,
        references=response.references or [], origin="fresh",
        source_item_id=None, external_call_id=external_call_id,
        department_intelligence=getattr(response, "department_intelligence", None),
    )


# ─── ①Exact Response Cache ─────────────────────────────────────────────
def find_exact_cache(db_client, config, *, domain: str, request_type: str,
                      request_fingerprint_hash: str, freshness_requirement: str = "normal",
                      capability: str = "chat", now=None) -> CrossDomainIntelligenceSource | None:
    """_is_geo_domain_enabled(config, capability)がFalseなら即None。
    domain=="geopolitics"の場合のみ、呼び出し元が計算済みのrequest_fingerprint_hashで
    external_intelligence_callsを検索し、見つかればCrossDomainIntelligenceSource
    (origin="exact_cache")へ正規化して返す（Gateway自身はハッシュを再計算しない）。
    freshness_requirement="high"の場合は既定のreuse_max_age_hoursより短いしきい値
    （geo_intelligence.<capability>.reuse_max_age_hours_high_freshness、既定2時間）を適用する。
    未対応domain・Kill Switch OFF・ヒットなし・例外時はいずれもNone（Failure Isolation）。"""
    if not _is_geo_domain_enabled(config, capability):
        return None
    if domain not in SUPPORTED_DOMAINS:
        return None
    if not request_fingerprint_hash:
        return None

    cap_cfg = ((config or {}).get("geo_intelligence") or {}).get(capability) or {}
    if not cap_cfg.get("reuse_enabled", True):
        return None
    max_age_hours = cap_cfg.get("reuse_max_age_hours", 24)
    if freshness_requirement == "high":
        max_age_hours = cap_cfg.get("reuse_max_age_hours_high_freshness", 2)
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=max_age_hours)).isoformat()

    try:
        rows = db_client.select("external_intelligence_calls", {
            "select": "*",
            "target_service": "eq.geo_intelligence",
            "request_type": f"eq.{request_type}",
            "status": "eq.success",
            "request_fingerprint_hash": f"eq.{request_fingerprint_hash}",
            "requested_at": f"gte.{cutoff}",
            "order": "requested_at.desc",
            "limit": "1",
        })
    except Exception:
        return None
    if not rows:
        return None
    return _source_from_cached_row(rows[0])


# ─── ②Existing Department Intelligence Retrieval ───────────────────────
def _score_geo_item(row: dict, keywords: list, themes: list, country_region: list) -> int:
    """単純なキーワード・タグ一致スコア（意味検索ではない）。"""
    haystack = " ".join(filter(None, [
        row.get("title"), row.get("geo_assessment"), row.get("why_relevant"),
    ])).lower()
    score = sum(1 for k in keywords if k and k.lower() in haystack)
    score += len(set(row.get("sustainability_themes") or []) & set(themes))
    score += len(set(row.get("country_region") or []) & set(country_region))
    return score


def retrieve_existing_intelligence(db_client, config, *, domains: list, keywords: list = None,
                                    themes: list = None, country_region: list = None,
                                    limit: int = 5, capability: str = "chat") -> list:
    """_is_geo_domain_enabled(config, capability)がFalseなら即空リスト。
    domainsに含まれる対応済みドメイン（現状"geopolitics"のみ）それぞれについて既存
    Intelligence候補を取得し、CrossDomainIntelligenceSource(origin="retrieved")のリストへ
    正規化して返す。未対応domainは無視する（将来ドメイン追加時、呼び出し側は無改修）。
    候補のas_ofはそのままSource側に保持するのみで、ここでは「古さ」による除外は行わない
    （使えるかどうかの判断はSufficiency判定側で行う。Known Limitations参照）。
    例外時は空リスト（Failure Isolation）。"""
    if not _is_geo_domain_enabled(config, capability):
        return []
    if "geopolitics" not in (domains or []):
        return []
    try:
        rows = db_client.select("weekly_geo_intelligence_items", {
            "select": "*", "include_in_weekly": "eq.true",
        })
    except Exception:
        return []

    keywords = keywords or []
    themes = themes or []
    country_region = country_region or []
    scored = [(r, _score_geo_item(r, keywords, themes, country_region)) for r in rows]
    scored = [(r, s) for r, s in scored if s > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [_source_from_geo_item(r) for r, _ in scored[:limit]]


# ─── ③Fresh Query ────────────────────────────────────────────────────
def query_fresh(db_client, config, *, domain: str, request_type: str, question: str,
                 country_region: list = None, source_id: str = None,
                 request_fingerprint_hash: str = None,
                 capability: str = "chat", freshness_requirement: str = None) -> CrossDomainIntelligenceSource | None:
    """_is_geo_domain_enabled(config, capability)がFalseなら即None。
    domain=="geopolitics"の場合のみgeo_intelligence_service.query_geo_intelligence()を呼び、
    CrossDomainIntelligenceSource(origin="fresh")へ正規化して返す。失敗時はNone。
    Geo側 Phase B-B（Cross-Department Inquiry Receiver）向けに、呼び出し元capability
    （chat/search）をsource_workflowとしてそのまま渡し、source_department="sustainability"を
    Gateway自身が確定する（呼び出し元ごとに毎回指定させない）。"""
    if not _is_geo_domain_enabled(config, capability):
        return None
    if domain not in SUPPORTED_DOMAINS:
        return None
    try:
        result = query_geo_intelligence(
            request_type=request_type, question=question,
            country_region=country_region or [], source_type=capability, source_id=source_id,
            request_fingerprint_hash=request_fingerprint_hash,
            source_department="sustainability", source_workflow=capability,
            freshness_requirement=freshness_requirement,
            db_client=db_client, config=config,
        )
    except Exception:
        return None
    if not result or not result.get("success"):
        return None
    response = result.get("response")
    if response is None:
        return None
    return _source_from_geo_response(response, result.get("external_call_id"))
