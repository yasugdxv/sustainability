"""
Weekly x Geo Intelligence Batch Inquiry（Phase S2）

週次レポート生成時、Geo Intelligence側（別アプリgeopolitical_monitor_dashboardの
weekly_monitoring request_type）へ「その週のSustainability監視対象テーマ・優先地域」を
まとめて問い合わせ、既存のSustainability記事候補とdedup分類（same_event/related_context/
independent）した上で、統合最終選定（置換・昇格）を経てWeekly Editor
（weekly_email_report.py）が使えるInput構造を組み立てて返す。

記事単位のGeo問い合わせは行わない。Sustainability記事本文はGeoへ送らない。

責務: Scope生成 → query_geo_intelligence(request_type="weekly_monitoring", ...)呼び出し
→ 結果保存（external_intelligence_callsへの参照のみ。response_payloadは二重保存しない）
→ Sustainability記事候補とのdedup分類 → 統合最終選定 → Weekly Editor向けInput構造を返す。

新しいHTTP Client・LLM Client・DBアクセスクラスは作らない。既存の
query_geo_intelligence()/sustainability_expert_common.call_llm_structured()/
article_crawler.SupabaseClientをそのまま再利用する。

「現在Weekly監視対象のテーマ」の正本はsustainability_article_selector.py側にあるため、
個別定数をfromでimportせずmodule経由（selector.xxx）で参照する
（weekly_email_report.pyが既にこの別名を使っている慣習と統一。二重管理・driftを防ぐ）。
"""
import hashlib
import json
import os
from datetime import date, datetime, timezone

from ai_client import make_openai_client
import sustainability_expert_common as common
import sustainability_article_selector as selector  # noqa: E402  正本はselector側。個別定数をfromでimportしない
from geo_intelligence_service import query_geo_intelligence

# ロジックversion定数。Prompt/Modelをチューニングした際はこれを上げるだけで
# 既存キャッシュ（dedup_input_hash/selection_input_hash）を安全に無効化できる
DEDUP_LOGIC_VERSION = "dedup-v1"
SELECTION_LOGIC_VERSION = "selection-v1"

# 日本語テーマ名→英語名の翻訳辞書のみ（正本ではない。未知のテーマ名はこの辞書に無ければ
# 日本語のまま送る＝取りこぼし防止のフォールバック）。9大分類＋2横断タグ。
THEME_LABEL_EN = {
    "水": "Water",
    "気候変動・GHG": "Climate",
    "容器包装": "Packaging",
    "原料調達": "Raw Materials",
    "生物多様性": "Biodiversity",
    "人権": "Human Rights",
    "健康": "Health",
    "人的資本": "Human Capital",
    "責任あるマーケティング": "Responsible Marketing",
    "情報開示": "Disclosure",
    "ESG評価・サステナブルファイナンス": "ESG Sustainable Finance",
}

# 優先地域（日本語の正本一覧）。当社（サントリーグループ）の主要事業地域・調達地域を
# 中心に選定した固定リスト（DB動的取得は行わない。地理タグは記事分類の主体ではないため）。
PRIORITY_GEOGRAPHIES_JA = [
    "日本", "米国", "中国", "欧州連合（EU）", "英国", "東南アジア（ASEAN）",
    "インド", "韓国", "メキシコ", "ブラジル", "中東", "アフリカ", "オセアニア",
]

PRIORITY_GEOGRAPHY_EN = {
    "日本": "Japan",
    "米国": "United States",
    "中国": "China",
    "欧州連合（EU）": "European Union",
    "英国": "United Kingdom",
    "東南アジア（ASEAN）": "Southeast Asia (ASEAN)",
    "インド": "India",
    "韓国": "South Korea",
    "メキシコ": "Mexico",
    "ブラジル": "Brazil",
    "中東": "Middle East",
    "アフリカ": "Africa",
    "オセアニア": "Oceania",
}

BUSINESS_CONTEXT_TEXT = (
    "当社（サントリーグループ）はアルコール飲料・清涼飲料・健康食品・スピリッツ等を"
    "グローバルに展開する消費財メーカーである。原材料調達（大麦・コーヒー豆・水資源等）・"
    "容器包装（プラスチック規制等）・気候変動・生物多様性・人権（サプライチェーン）・"
    "健康（アルコール規制等）・情報開示（ESG規制）等のサステナビリティ重点テーマに関連し、"
    "サステナビリティ担当者が把握すべき地政学的動向（規制動向・供給網リスク・地域紛争・"
    "貿易摩擦等）を抽出してほしい。"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value) -> str:
    """weekly_geo_intelligence_runs.period_start/period_end保存用。
    datetimeは.date()経由、dateはそのままisoformat()する（時刻成分を混入させない）。
    既に文字列の場合はそのまま通す。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


# ─── Kill Switch ────────────────────────────────────────────────────
def is_weekly_geo_enabled(config: dict) -> bool:
    """config.json の geo_intelligence.weekly_monitoring.enabled が優先。
    無ければ環境変数 SUSTAINABILITY_WEEKLY_GEO_ENABLED にフォールバックする
    （sustainability_expert_common.is_enabled()と同じ「config優先→envフォールバック」パターン）。

    geo_intelligence.enabled（Geo機能全体のKill Switch）が明示的にfalseの場合は、
    下位のweekly_monitoring設定に関わらず強制的に無効とする（設定ミスで全体スイッチを
    切ったつもりが子設定のtrueにより無駄なGeo呼び出しが発生し続けるのを防ぐため）。
    geo_intelligence.enabledが未設定の場合は、従来通り子設定にのみ従う。"""
    geo_cfg = (config or {}).get("geo_intelligence") or {}
    if geo_cfg.get("enabled") is False:
        return False
    cfg = geo_cfg.get("weekly_monitoring") or {}
    if "enabled" in cfg:
        return bool(cfg["enabled"])
    env = os.environ.get("SUSTAINABILITY_WEEKLY_GEO_ENABLED", "")
    return env.strip().lower() in ("1", "true", "yes")


# ─── 記事dictからのフィールド抽出（呼び出し元により、まだLLM書き換え前/後の両方があり得るため
# 複数の候補キー名を許容し、無い項目はNoneのまま渡す） ──────────────────────────
def _article_headline(article: dict):
    return article.get("headline") or article.get("title")


def _article_short_summary(article: dict):
    return article.get("short_summary") or article.get("summary_short") or article.get("importance_reason")


def _article_total_score(article: dict):
    v = article.get("total_score")
    if v is not None:
        return v
    return article.get("selector_total_score")


def _article_summary(article: dict) -> dict:
    return {
        "headline": _article_headline(article),
        "short_summary": _article_short_summary(article),
        "themes": article.get("themes") or [],
        "geography": article.get("geography"),
        "total_score": _article_total_score(article),
    }


# ─── テーマスコープ（DB動的取得。正本はsustainability_article_selector.py側） ─────────
def _fetch_weekly_theme_names(client) -> list:
    """現在Weekly監視対象のテーマ名（大分類9件＋横断2件、計11件）を返す。
    正本はsustainability_article_selector.pyのSUB_AXIS_PARENT_TAG_ID（下位種別の親、
    Weekly主要対象外）とTIER0_PROMOTED_CROSS_CUTTING_IDS（情報開示／ESG評価・
    サステナブルファイナンス）。個別定数はmodule経由(selector.xxx)で参照し、二重管理・
    driftを防ぐ。小分類は取得しない（大分類名のみで11領域を過不足なくカバーできるため）。"""
    tag_rows = client.select("tag_reference", {"select": "tag_id,tag_axis,tag_level,tag_name,status"})
    majors = [
        t for t in tag_rows
        if t.get("tag_axis") == "テーマ" and t.get("tag_level") == "大分類"
        and t.get("tag_id") != selector.SUB_AXIS_PARENT_TAG_ID
    ]
    cross = [t for t in tag_rows if t.get("tag_id") in selector.TIER0_PROMOTED_CROSS_CUTTING_IDS]
    return [t["tag_name"] for t in majors] + [t["tag_name"] for t in cross]


def build_weekly_scope(client, period_start, period_end, scope_version: str = "v1") -> dict:
    """Geoへ送信するweekly_monitoring Scope本体を組み立てる。
    themes/geographiesは英語変換（辞書に無ければ日本語のまま＝取りこぼし防止）。"""
    theme_names_ja = _fetch_weekly_theme_names(client)
    sustainability_themes = [THEME_LABEL_EN.get(t, t) for t in theme_names_ja]
    priority_geographies = [PRIORITY_GEOGRAPHY_EN.get(g, g) for g in PRIORITY_GEOGRAPHIES_JA]
    return {
        "sustainability_themes": sustainability_themes,
        "priority_geographies": priority_geographies,
        "business_context": BUSINESS_CONTEXT_TEXT,
        "period_start": _iso(period_start),
        "period_end": _iso(period_end),
        "scope_version": scope_version,
    }


def _compute_scope_hash(scope: dict) -> str:
    """sustainability_themes/priority_geographies/business_contextから安定したhashを
    生成する（ソート・正規化してから算出。標準ライブラリのみ）。実送信のbusiness_contextと
    必ず同じ値を使うこと（実装ガード1）。"""
    themes = sorted(scope.get("sustainability_themes") or [])
    geos = sorted(scope.get("priority_geographies") or [])
    business_context = scope.get("business_context") or ""
    text = "\x1f".join(themes) + "\x1e" + "\x1f".join(geos) + "\x1e" + business_context
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_dedup_input_hash(dedup_candidate_pool: list) -> str:
    """dedup_candidate_pool内の各記事のarticle_id（build_articles_from_picks()の戻り値に
    「更新時刻」相当の安定したフィールドが実際には無いため、article_idのみで安定ソートした
    リストから算出）に、DEDUP_LOGIC_VERSIONを加えてhash化する。"""
    parts = [f"A:{a.get('article_id')}" for a in (dedup_candidate_pool or [])]
    parts.append(f"version:{DEDUP_LOGIC_VERSION}")
    return hashlib.sha256("\x1f".join(sorted(parts)).encode("utf-8")).hexdigest()


def _compute_selection_input_hash(ranked_final_articles: list, independent_candidates: list,
                                   promotion_candidates: list, target_max: int,
                                   max_independent_topics: int) -> str:
    """final記事のarticle_id+total_score、independent/promotion candidates各itemのID・
    candidate種別、target_max、max_independent_topics、SELECTION_LOGIC_VERSIONをすべて
    ソート・結合してhash化する（実装ガード5・v7修正点3）。"""
    parts = [f"F:{a.get('article_id')}:{_article_total_score(a)}" for a in (ranked_final_articles or [])]
    parts += [f"I:{item.get('geo_item_id')}" for item in (independent_candidates or [])]
    parts += [f"P:{promo.get('article_id')}" for promo in (promotion_candidates or [])]
    parts.append(f"target_max:{target_max}")
    parts.append(f"max_independent_topics:{max_independent_topics}")
    parts.append(f"version:{SELECTION_LOGIC_VERSION}")
    return hashlib.sha256("\x1f".join(sorted(parts)).encode("utf-8")).hexdigest()


# ─── Geo Response item のview（DB行 → Weekly Editorが扱いやすい形） ────────────────
def _item_view(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "geo_item_id": row.get("geo_item_id"),
        "title": row.get("title"),
        "event_date": row.get("event_date"),
        "as_of": row.get("as_of"),
        "country_region": row.get("country_region") or [],
        "sustainability_themes": row.get("sustainability_themes") or [],
        "geo_assessment": row.get("geo_assessment"),
        "why_relevant": row.get("why_relevant"),
        "key_stakeholders": row.get("key_stakeholders") or [],
        "political_dynamics": row.get("political_dynamics"),
        "outlook": row.get("outlook"),
        "references": row.get("references") or [],
        "confidence": row.get("confidence"),
        "expert_response_id": row.get("expert_response_id"),
    }


def _resolve_promotion_representative(candidate_ids: list, pool_by_id: dict) -> str:
    """same_event Geo itemのmatched_article_idsが複数の未選定記事を指す場合、
    dedup_candidate_poolのtotal_score降順（同点はarticle_id昇順）で決定論的に
    代表article_idを1件選ぶ（実装ガード4）。"""
    def _key(aid):
        score = _article_total_score(pool_by_id[aid])
        return (-(score if score is not None else -1), aid)
    return sorted(set(candidate_ids), key=_key)[0]


def _structure_for_weekly_editor(items: list, final_articles: list, dedup_candidate_pool: list,
                                  run_id=None, run_status: str = "success") -> dict:
    """dedup_classification済みのitem群を、Weekly Editorが使えるInput構造へ組み立てる。
    dedup_classification IS NULL のitem（未分類・分類失敗分）はどのバケツにも入らない。"""
    final_ids = {a.get("article_id") for a in (final_articles or [])}
    pool_by_id = {a.get("article_id"): a for a in (dedup_candidate_pool or [])}

    independent_candidates = []
    article_context: dict = {}
    background_context: dict = {}
    theme_background_context: dict = {}
    promotion_by_article: dict = {}

    for row in items or []:
        classification = row.get("dedup_classification")
        if classification is None:
            continue
        item_view = _item_view(row)
        matched_ids = row.get("matched_article_ids") or []
        final_matches = [aid for aid in matched_ids if aid in final_ids]

        if classification == "same_event":
            if final_matches:
                for aid in final_matches:
                    article_context.setdefault(aid, []).append(item_view)
            else:
                candidate_ids = [aid for aid in matched_ids if aid in pool_by_id]
                if not candidate_ids:
                    continue  # マッチ先が母集団にも存在しない（解決不能。保存はされているが表示しない）
                rep_id = _resolve_promotion_representative(candidate_ids, pool_by_id)
                bucket = promotion_by_article.setdefault(rep_id, {
                    "article_id": rep_id,
                    "article_summary": _article_summary(pool_by_id[rep_id]),
                    "geo_items": [],
                })
                bucket["geo_items"].append(item_view)  # 実装ガード9: 同一代表記事への集約
        elif classification == "related_context":
            if final_matches:
                for aid in final_matches:
                    background_context.setdefault(aid, []).append(item_view)
            else:
                # v7修正点5: matched_article_idsが空、または全マッチ先が未選定記事のみ
                # → theme_background_contextへ回す（Theme Digestの背景情報として活用）
                for theme in (row.get("sustainability_themes") or []):
                    theme_background_context.setdefault(theme, []).append(item_view)
        elif classification == "independent":
            independent_candidates.append(item_view)

    return {
        "independent_candidates": independent_candidates,
        "promotion_candidates": list(promotion_by_article.values()),
        "article_context": article_context,
        "background_context": background_context,
        "theme_background_context": theme_background_context,
        "run_status": run_status,
        "run_id": run_id,
    }


def _empty_structured_result(run_status: str) -> dict:
    return {
        "independent_candidates": [], "promotion_candidates": [], "article_context": {},
        "background_context": {}, "theme_background_context": {},
        "run_status": run_status, "run_id": None,
    }


def _empty_selection_result() -> dict:
    return {"selected_independent_geo_item_ids": [], "promoted_article_ids": [], "displaced_article_ids": []}


# ─── Dedup分類（LLM、既存call_llm_structuredを再利用） ─────────────────────────────
DEDUP_SYSTEM_PROMPT = """あなたは当社サステナビリティ担当者向けに、地政学情報(Geo Intelligence)の
各アイテムと、既存のSustainability記事候補群との関係を分類する専門家です。

各geo_itemについて、以下の3分類のいずれかに判定してください:
- same_event: geo_itemが、いずれかのsustainability_articleと同一の事象を報じている
- related_context: 同一事象ではないが、いずれかのsustainability_articleの背景・関連文脈として有用
- independent: 既存のsustainability_articlesのいずれとも無関係な、新規の地政学トピック

same_event/related_contextの場合はmatched_article_idsに該当するarticle_idを1件以上含めること
（independentの場合は空配列）。テーマの一致だけで安易にsame_event/related_contextと判定せず、
具体的な事象・対象の一致を根拠にすること。入力に存在しないarticle_idを創作しないこと。

出力はJSON1個のみ。classifications配列の要素数・順序は入力のgeo_items配列と完全に一致させること
（1件目のgeo_item→classifications[0]、のように対応させる。記事間の内容を混同しないこと）。
"""

DEDUP_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classifications"],
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["classification", "matched_article_ids"],
                "properties": {
                    "classification": {"type": "string", "enum": ["same_event", "related_context", "independent"]},
                    "matched_article_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def _build_dedup_user_prompt(items: list, dedup_candidate_pool: list) -> str:
    geo_items_payload = [{
        "item_id": r.get("geo_item_id"), "title": r.get("title"), "event_date": r.get("event_date"),
        "country_region": r.get("country_region") or [],
        "sustainability_themes": r.get("sustainability_themes") or [],
        "geo_assessment": r.get("geo_assessment"), "why_relevant": r.get("why_relevant"),
    } for r in items]
    articles_payload = [{
        "article_id": a.get("article_id"), "headline": _article_headline(a),
        "short_summary": _article_short_summary(a),
        "publication_date": a.get("published_at") or a.get("publication_date"),
        "geography": a.get("geography"), "themes": a.get("themes") or [],
    } for a in dedup_candidate_pool]
    return (
        f"# geo_items（配列の順序を保ったまま、対応するclassificationsへ1件ずつ格納すること）\n"
        f"{json.dumps(geo_items_payload, ensure_ascii=False)}\n\n"
        f"# sustainability_articles（Weekly候補記事の広い母集団。本文全文は含まない）\n"
        f"{json.dumps(articles_payload, ensure_ascii=False)}\n"
    )


def classify_against_sustainability_articles(azure_client, model: str, items: list,
                                              dedup_candidate_pool: list) -> list:
    """items（Geo item群、本文全文は使わない）を、dedup_candidate_pool（Weekly候補の広い母集団）
    と比較しsame_event/related_context/independentへ分類する。既存のcall_llm_structured()を
    再利用（新しいLLM Clientは作らない）。戻り値はitemsと同じ順序・同じ長さのlist。"""
    user_prompt = _build_dedup_user_prompt(items, dedup_candidate_pool)
    result = common.call_llm_structured(
        azure_client, model, DEDUP_SYSTEM_PROMPT, user_prompt,
        DEDUP_CLASSIFICATION_SCHEMA, "GeoDedupClassificationBatch")
    classifications = result["data"]["classifications"]
    if len(classifications) != len(items):
        raise common.ExpertLLMError(
            f"分類件数({len(classifications)})が要求件数({len(items)})と一致しません")
    return classifications


def _run_dedup_classification(client, azure_client, model, run_id, dedup_candidate_pool: list,
                               dedup_input_hash: str) -> bool:
    """run_idに紐づくitem群をDedup分類する（初回・再試行共通）。
    実装ガード6: LLM呼び出し「前」に旧dedup_classification/matched_article_ids、
    item単位selection_status/include_in_weekly、Run単位selection_status/selection_result/
    selection_input_hashをすべて無効化（NULL/空）してからLLMを呼ぶ（初回実行時はitemが
    まだ未分類のため実質no-op）。再Dedup自体が失敗した場合もこのNULL状態のまま据え置き、
    古い分類・古いSelection結果へは絶対に巻き戻さない。
    戻り値: 分類が成功しdedup_status='completed'になった場合True、それ以外False。"""
    item_rows = client.select("weekly_geo_intelligence_items", {"select": "*", "run_id": f"eq.{run_id}"})
    if not item_rows:
        return False

    for row in item_rows:
        client.update("weekly_geo_intelligence_items", {"id": f"eq.{row['id']}"}, {
            "dedup_classification": None, "matched_article_ids": [],
            "selection_status": None, "include_in_weekly": False,
            "selection_reason": None, "selected_at": None,
        })
    client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {
        "selection_status": None, "selection_result": None, "selection_input_hash": None,
    })

    if azure_client is None:
        client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {"dedup_status": "failed"})
        return False

    try:
        classified = classify_against_sustainability_articles(azure_client, model, item_rows, dedup_candidate_pool)
    except Exception:
        client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {"dedup_status": "failed"})
        return False

    for row, cls in zip(item_rows, classified):
        client.update("weekly_geo_intelligence_items", {"id": f"eq.{row['id']}"}, {
            "dedup_classification": cls.get("classification"),
            "matched_article_ids": cls.get("matched_article_ids") or [],
        })
    client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {
        "dedup_status": "completed", "dedup_input_hash": dedup_input_hash,
    })
    return True


def _finalize_zero_candidate_selection(client, run_id, structured: dict) -> None:
    """Dedup（再）分類が成功した結果、独立候補・昇格候補が0件になった場合、Run単位の
    selection_statusを'not_needed'・selection_resultを空の3キー構造に明示的に確定する
    （実装ガード2。古いRunに残っていたdisplaced_article_ids/promoted_article_idsを
    絶対に残さないため）。呼び出し元はdedup分類が成功した場合のみこれを呼ぶこと
    （失敗時はNULLのまま据え置くのが正しい＝実装ガード6）。"""
    if not structured["independent_candidates"] and not structured["promotion_candidates"]:
        client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {
            "selection_status": "not_needed", "selection_result": _empty_selection_result(),
            "selection_input_hash": None,
        })


def _save_items(client, run_id, relevant_intelligence: list) -> list:
    rows = [{
        "run_id": run_id,
        "geo_item_id": it.get("item_id"),
        "title": it.get("title"),
        "event_date": it.get("event_date"),
        "as_of": it.get("as_of"),
        "key_stakeholders": it.get("key_stakeholders") or [],
        "expert_response_id": it.get("expert_response_id"),
        "country_region": it.get("country_region") or [],
        "sustainability_themes": it.get("sustainability_themes") or [],
        "geo_assessment": it.get("geo_assessment"),
        "why_relevant": it.get("why_relevant"),
        "political_dynamics": it.get("political_dynamics"),
        "outlook": it.get("outlook"),
        "references": it.get("references") or [],
        "confidence": it.get("confidence"),
        "dedup_classification": None,
        "matched_article_ids": [],
        "include_in_weekly": False,
    } for it in relevant_intelligence]
    return client.insert("weekly_geo_intelligence_items", rows)


# ─── run_weekly_geo_inquiry: Scope生成→Geo API→Dedup分類（3層再利用込み） ───────────
def run_weekly_geo_inquiry(config, db_client, period_start, period_end, final_articles, dedup_candidate_pool,
                            *, geo_client=None, azure_client=None, model=None, force_rerun=False) -> dict:
    """週次のGeo Intelligence問い合わせ本体。Kill Switch確認→Scope算出→3層再利用判定
    （Geo API／Dedup分類／[Selectionはselect_weekly_geo_topics()側]）→Weekly Editor向け
    Input構造を返す。例外は投げない（Failure Isolation。Weekly生成自体は継続する）。

    azure_client/modelは省略時にai_client.make_openai_client(config)から生成する
    （Dedup分類LLM呼び出し用。呼び出し元のweekly_email_report.pyは既にazure_client/modelを
    持っているが、このモジュール単体でも完結できるようにするため、テスト等でモック注入したい
    場合のみ明示的に渡せばよい）。"""
    if not is_weekly_geo_enabled(config):
        return _empty_structured_result(run_status="disabled")

    weekly_geo_cfg = ((config or {}).get("geo_intelligence") or {}).get("weekly_monitoring") or {}
    scope_version = weekly_geo_cfg.get("scope_version", "v1")

    scope = build_weekly_scope(db_client, period_start, period_end, scope_version)
    scope_hash = _compute_scope_hash(scope)
    dedup_input_hash = _compute_dedup_input_hash(dedup_candidate_pool)

    if azure_client is None and model is None:
        azure_client, model = make_openai_client(config)

    period_start_s = _iso(period_start)
    period_end_s = _iso(period_end)

    existing_rows = db_client.select("weekly_geo_intelligence_runs", {
        "select": "*",
        "period_start": f"eq.{period_start_s}",
        "period_end": f"eq.{period_end_s}",
        "scope_version": f"eq.{scope_version}",
        "status": "eq.success",
        "order": "created_at.desc",
        "limit": "1",
    })
    existing = existing_rows[0] if existing_rows else None

    reuse_full = (
        not force_rerun and existing is not None and existing.get("scope_hash") == scope_hash
        and existing.get("dedup_status") in ("completed", "not_needed")
        and existing.get("dedup_input_hash") == dedup_input_hash
    )
    dedup_retry_only = (
        not force_rerun and existing is not None and existing.get("scope_hash") == scope_hash
        and not reuse_full
    )

    if reuse_full:
        run_id = existing["id"]
        items = db_client.select("weekly_geo_intelligence_items", {"select": "*", "run_id": f"eq.{run_id}"})
        return _structure_for_weekly_editor(items, final_articles, dedup_candidate_pool,
                                             run_id=run_id, run_status=existing.get("status", "success"))

    if dedup_retry_only:
        run_id = existing["id"]
        dedup_succeeded = _run_dedup_classification(
            db_client, azure_client, model, run_id, dedup_candidate_pool, dedup_input_hash)
        items = db_client.select("weekly_geo_intelligence_items", {"select": "*", "run_id": f"eq.{run_id}"})
        structured = _structure_for_weekly_editor(items, final_articles, dedup_candidate_pool,
                                                   run_id=run_id, run_status=existing.get("status", "success"))
        if dedup_succeeded:
            _finalize_zero_candidate_selection(db_client, run_id, structured)
        return structured

    # ここから新規実行（scope不一致／既存run無し／force_rerun。履歴として新しいrunを作る）
    result = query_geo_intelligence(
        request_type="weekly_monitoring", period_start=period_start, period_end=period_end,
        sustainability_themes=scope["sustainability_themes"], priority_geographies=scope["priority_geographies"],
        business_context=scope.get("business_context"), source_type="weekly_report",
        source_id=f"{period_start_s}_{period_end_s}",
        geo_client=geo_client, db_client=db_client, config=config,
    )
    if not result.get("success"):
        return _empty_structured_result(run_status=result.get("status", "failed"))

    response = result.get("response")
    relevant_intelligence = (getattr(response, "relevant_intelligence", None) if response else None) or []
    run_row = {
        "period_start": period_start_s,
        "period_end": period_end_s,
        "scope_version": scope_version,
        "external_call_id": result.get("external_call_id"),
        "local_request_id": result.get("local_request_id"),
        "remote_request_id": getattr(response, "request_id", None) if response else None,
        "status": result["status"],
        "remote_status": result.get("remote_status"),
        "scope_hash": scope_hash,
        "dedup_input_hash": dedup_input_hash,
        "request_scope": scope,
        "knowledge_sufficiency": getattr(response, "knowledge_sufficiency", None) if response else None,
        "confidence": getattr(response, "confidence", None) if response else None,
        "item_count": len(relevant_intelligence),
        "truncated_count": (getattr(response, "truncated_count", None) if response else None) or 0,
        # created_atを明示的に付与する（本番Supabaseはdefault now()で自動採番されるが、
        # テスト用FakeSupabaseClientは行追加順ではなく作成時刻でdesc取得するため、
        # 同一ミリ秒での挿入順の曖昧さを避けるために明示する）
        "created_at": _now_iso(),
    }

    if not relevant_intelligence:
        # v4修正点2: 0件時はDedup不要として正常完了扱い（Knowledge Gap扱いしない）
        run_row["dedup_status"] = "not_needed"
        run_row["selection_status"] = "not_needed"
        run_row["selection_result"] = _empty_selection_result()
        inserted = db_client.insert("weekly_geo_intelligence_runs", [run_row])
        run_id = inserted[0]["id"]
        return _structure_for_weekly_editor([], final_articles, dedup_candidate_pool,
                                             run_id=run_id, run_status=result["status"])

    inserted = db_client.insert("weekly_geo_intelligence_runs", [run_row])
    run_id = inserted[0]["id"]
    _save_items(db_client, run_id, relevant_intelligence)

    dedup_succeeded = _run_dedup_classification(
        db_client, azure_client, model, run_id, dedup_candidate_pool, dedup_input_hash)
    items = db_client.select("weekly_geo_intelligence_items", {"select": "*", "run_id": f"eq.{run_id}"})
    structured = _structure_for_weekly_editor(items, final_articles, dedup_candidate_pool,
                                               run_id=run_id, run_status=result["status"])
    if dedup_succeeded:
        _finalize_zero_candidate_selection(db_client, run_id, structured)
    return structured


# ─── 統合最終選定（置換方式・select形式・LLM統一判定） ─────────────────────────────
SELECTION_SYSTEM_PROMPT = """あなたは当社サステナビリティ週次レポートの編集者です。
既存の週次掲載候補のうち重要度が低い記事群(existing_lowest_articles)と、地政学情報由来の
掲載候補(geo_candidates。独立トピックindependent、または既存記事への昇格候補promotion)を
横並びで比較し、どのgeo_candidateを実際にWeeklyへ掲載するか判定してください。

existing_lowest_articlesのtotal_scoreはSustainability記事側の参考情報に過ぎず、Geo候補には
同じ尺度の数値評価がありません。数値として単純比較せず、内容の重要度・当社事業への
関連性を踏まえて総合的に判定してください。

判定ルール:
- is_full（既存候補が既に定員一杯）がtrueの場合、select=trueにするgeo_candidateには、
  existing_lowest_articlesの中から実際に置き換える記事のarticle_idをdisplaced_article_idに
  必ず指定すること（置き換える記事が無いならselect=falseにすること）
- is_fullがfalseの場合、空き枠として追加採用するならdisplaced_article_idをnullにしてよい
- 枠に余裕があるからといって安易にselect=trueにせず、実際に掲載する価値があるものだけを
  選ぶこと（自動的に埋める必要はない）
- 同一のdisplaced_article_idを複数のgeo_candidateに割り当てないこと

出力はJSON1個のみ。decisions配列にgeo_candidates全件について1件ずつ判定を含めること
（candidate_idで対応関係を示す）。
"""

SELECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["candidate_id", "select", "displaced_article_id", "reason"],
                "properties": {
                    "candidate_id": {"type": "string"},
                    "select": {"type": "boolean"},
                    "displaced_article_id": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def _combine_geo_items_text(geo_items: list, field: str) -> str:
    """実装ガード9で1つの代表記事へ集約された複数geo_itemの同一フィールドを結合する
    （promotion candidateの統合評価用）"""
    values = [gi.get(field) for gi in (geo_items or []) if gi.get(field)]
    return "\n---\n".join(values)


def _apply_selection_guards(decisions_by_id: dict, order: list, existing_lowest_ids: set,
                             ranked_final_articles: list, target_max: int,
                             max_independent_topics: int, is_full: bool) -> dict:
    """LLM出力を適用する前のコード側最終防衛ライン（実装ガード3・7）。
    LLMの構造化出力を鵜呑みにせず、不変条件をコードで必ず保証する。"""
    decisions = {}
    for cid in order:
        raw = decisions_by_id.get(cid) or {}
        decisions[cid] = {
            "select": bool(raw.get("select")),
            "displaced_article_id": raw.get("displaced_article_id"),
            "reason": raw.get("reason") or "",
        }

    # 実装ガード7: 満枠なのにdisplaced_article_idが無い/無効なselect=trueは強制却下
    for cid in order:
        d = decisions[cid]
        if d["select"] and is_full:
            disp = d["displaced_article_id"]
            if not disp or disp not in existing_lowest_ids:
                d["select"] = False
                d["reason"] += "（満枠でdisplaced_article_id無効のため強制却下）"

    # 実装ガード3(a): 同一displaced_article_idの重複指定 → 最初の1件のみ採用
    used_displaced = set()
    for cid in order:
        d = decisions[cid]
        if d["select"] and d["displaced_article_id"]:
            if d["displaced_article_id"] in used_displaced:
                d["select"] = False
                d["reason"] += "（displaced_article_id重複のため強制却下）"
            else:
                used_displaced.add(d["displaced_article_id"])

    # max_independent_topics（independent/promotion合算）を超えるselect=trueは順序に従い切り詰める
    selected_order = [cid for cid in order if decisions[cid]["select"]]
    for cid in selected_order[max_independent_topics:]:
        decisions[cid]["select"] = False
        decisions[cid]["reason"] += "（max_independent_topics超過のため強制却下）"

    # 実装ガード3(c): 最終件数（既存Sustainability記事＋採用Geo/promotion候補）がtarget_maxを
    # 超えないことをコードで保証する
    selected_order = [cid for cid in order if decisions[cid]["select"]]
    added_without_displace = [cid for cid in selected_order if not decisions[cid]["displaced_article_id"]]
    displaced_count = len({decisions[cid]["displaced_article_id"] for cid in selected_order
                            if decisions[cid]["displaced_article_id"]})
    existing_kept = len(ranked_final_articles) - displaced_count
    final_total = existing_kept + len(selected_order)
    overflow = final_total - target_max
    if overflow > 0:
        for cid in reversed(added_without_displace):
            if overflow <= 0:
                break
            decisions[cid]["select"] = False
            decisions[cid]["reason"] += "（target_max超過のため強制却下）"
            overflow -= 1

    return decisions


def _mark_selection_failed(db_client, run_id, independent_candidates: list, promotion_candidates: list) -> None:
    patch = {"selection_status": "failed", "selection_reason": None, "selected_at": None,
              "include_in_weekly": False}
    for item in independent_candidates:
        gid = item.get("geo_item_id")
        if gid:
            db_client.update("weekly_geo_intelligence_items",
                              {"run_id": f"eq.{run_id}", "geo_item_id": f"eq.{gid}"}, patch)
    for promo in promotion_candidates:
        for geo_item in (promo.get("geo_items") or []):
            gid = geo_item.get("geo_item_id")
            if gid:
                db_client.update("weekly_geo_intelligence_items",
                                  {"run_id": f"eq.{run_id}", "geo_item_id": f"eq.{gid}"}, patch)
    db_client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {"selection_status": "failed"})


def _restore_selection_result(db_client, run_id, selection_result: dict) -> dict:
    """v7修正点2・実装ガード8: Run単位のselection_resultをそのまま読み出して再現する
    （item単位のselection_statusを再集計しない）。独立トピックセクションは
    selected_independent_geo_item_idsのみから復元し、promoted_article_ids由来のitemは
    重複して含めない。"""
    ids = selection_result.get("selected_independent_geo_item_ids") or []
    items = []
    if ids:
        items = db_client.select("weekly_geo_intelligence_items", {
            "select": "*", "run_id": f"eq.{run_id}", "geo_item_id": f"in.({','.join(ids)})",
        })
    return {
        "selected_geo_topics": [_item_view(row) for row in items],
        "promoted_article_ids": selection_result.get("promoted_article_ids") or [],
        "displaced_article_ids": selection_result.get("displaced_article_ids") or [],
    }


def select_weekly_geo_topics(azure_client, model, independent_candidates: list, promotion_candidates: list,
                              ranked_final_articles: list, config: dict, db_client, run_id,
                              *, force_rerun: bool = False) -> dict:
    """Geo独立候補・昇格候補を実際にWeeklyへ載せるかどうかの統合最終選定。
    run_idは結果をRun単位で保存・再現するために必須引数。例外は投げない
    （失敗時は対象itemをselection_status='failed'のまま据え置き、次回Selectionのみ再試行）。"""
    target_max = common.get_weekly_pick_range(config)[1]
    max_independent_topics = ((config or {}).get("geo_intelligence", {}).get("weekly_monitoring", {}) or {}
                               ).get("max_independent_topics", 3)
    selection_input_hash = _compute_selection_input_hash(
        ranked_final_articles, independent_candidates, promotion_candidates, target_max, max_independent_topics)

    run_rows = db_client.select("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}", "limit": "1"})
    run = run_rows[0] if run_rows else None

    if (not force_rerun and run and run.get("selection_status") == "completed"
            and run.get("selection_input_hash") == selection_input_hash):
        return _restore_selection_result(db_client, run_id, run.get("selection_result") or _empty_selection_result())

    candidate_type_by_id = {}
    geo_candidates = []
    for item in independent_candidates:
        cid = item.get("geo_item_id")
        candidate_type_by_id[cid] = "independent"
        geo_candidates.append({
            "candidate_id": cid, "candidate_type": "independent", "title": item.get("title"),
            "geo_assessment": item.get("geo_assessment"), "why_relevant": item.get("why_relevant"),
            "political_dynamics": item.get("political_dynamics"), "outlook": item.get("outlook"),
        })
    for promo in promotion_candidates:
        cid = promo.get("article_id")
        candidate_type_by_id[cid] = "promotion"
        geo_items = promo.get("geo_items") or []
        geo_candidates.append({
            "candidate_id": cid, "candidate_type": "promotion",
            "headline": (promo.get("article_summary") or {}).get("headline"),
            "article_summary": promo.get("article_summary"),
            "geo_assessment": _combine_geo_items_text(geo_items, "geo_assessment"),
            "why_relevant": _combine_geo_items_text(geo_items, "why_relevant"),
            "political_dynamics": _combine_geo_items_text(geo_items, "political_dynamics"),
            "outlook": _combine_geo_items_text(geo_items, "outlook"),
        })

    if not geo_candidates:
        db_client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {
            "selection_status": "not_needed", "selection_result": _empty_selection_result(),
            "selection_input_hash": selection_input_hash,
        })
        return {"selected_geo_topics": [], "promoted_article_ids": [], "displaced_article_ids": []}

    is_full = len(ranked_final_articles) >= target_max
    n = len(geo_candidates)
    sorted_existing = sorted(
        ranked_final_articles,
        key=lambda a: (-(_article_total_score(a) if _article_total_score(a) is not None else -1),
                       a.get("article_id") or ""))
    lowest_n = sorted_existing[-n:] if 0 < n <= len(sorted_existing) else sorted_existing
    existing_lowest_ids = {a.get("article_id") for a in lowest_n}

    payload = {
        "existing_lowest_articles": [{
            "article_id": a.get("article_id"), "headline": _article_headline(a),
            "total_score": _article_total_score(a), "short_summary": _article_short_summary(a),
            "themes": a.get("themes") or [], "geography": a.get("geography"),
        } for a in lowest_n],
        "geo_candidates": geo_candidates,
        "target_max": target_max, "max_independent_topics": max_independent_topics, "is_full": is_full,
    }

    try:
        if azure_client is None:
            raise common.ExpertLLMError("Azure OpenAIクライアントが未設定です")
        result = common.call_llm_structured(
            azure_client, model, SELECTION_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False),
            SELECTION_SCHEMA, "GeoWeeklySelection")
        decisions_raw = result["data"]["decisions"]
    except Exception:
        _mark_selection_failed(db_client, run_id, independent_candidates, promotion_candidates)
        return {"selected_geo_topics": [], "promoted_article_ids": [], "displaced_article_ids": []}

    decisions_by_id = {d.get("candidate_id"): d for d in decisions_raw}
    order = [c["candidate_id"] for c in geo_candidates]
    decisions = _apply_selection_guards(decisions_by_id, order, existing_lowest_ids,
                                         ranked_final_articles, target_max, max_independent_topics, is_full)

    selected_independent_geo_item_ids = [
        cid for cid in order if decisions[cid]["select"] and candidate_type_by_id[cid] == "independent"]
    promoted_article_ids = [
        cid for cid in order if decisions[cid]["select"] and candidate_type_by_id[cid] == "promotion"]
    displaced_article_ids = sorted({
        decisions[cid]["displaced_article_id"] for cid in order
        if decisions[cid]["select"] and decisions[cid].get("displaced_article_id")})

    promo_by_id = {p.get("article_id"): p for p in promotion_candidates}
    for cid in order:
        d = decisions[cid]
        patch = {
            "selection_status": "selected" if d["select"] else "rejected",
            "selection_reason": d.get("reason"),
            "selected_at": _now_iso() if d["select"] else None,
            "include_in_weekly": bool(d["select"]),
        }
        if candidate_type_by_id[cid] == "independent":
            db_client.update("weekly_geo_intelligence_items",
                              {"run_id": f"eq.{run_id}", "geo_item_id": f"eq.{cid}"}, patch)
        else:
            promo = promo_by_id.get(cid)
            for geo_item in ((promo or {}).get("geo_items") or []):
                gid = geo_item.get("geo_item_id")
                if gid:
                    db_client.update("weekly_geo_intelligence_items",
                                      {"run_id": f"eq.{run_id}", "geo_item_id": f"eq.{gid}"}, patch)

    selection_result = {
        "selected_independent_geo_item_ids": selected_independent_geo_item_ids,
        "promoted_article_ids": promoted_article_ids,
        "displaced_article_ids": displaced_article_ids,
    }
    db_client.update("weekly_geo_intelligence_runs", {"id": f"eq.{run_id}"}, {
        "selection_status": "completed", "selection_result": selection_result,
        "selection_input_hash": selection_input_hash,
    })

    selected_geo_topics = [item for item in independent_candidates
                            if item.get("geo_item_id") in selected_independent_geo_item_ids]
    return {"selected_geo_topics": selected_geo_topics, "promoted_article_ids": promoted_article_ids,
            "displaced_article_ids": displaced_article_ids}
