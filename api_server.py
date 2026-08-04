"""
サスティナビリティ記事ダッシュボード React版フロントエンド用バックエンドAPI。

eco-digest-spark (React/Vite) から呼び出される。既存のStreamlit版
(sustainability_dashboard_app.py) と同じデータ取得・翻訳・検索・チャットロジックを
sustainability_dashboard_core.py 経由で共有する。

起動: python api_server.py  (http://127.0.0.1:8000)
"""
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import load_config, SupabaseClient  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
from sustainability_knowledge_store import get_knowledge_store  # noqa: E402
import sustainability_dashboard_core as core  # noqa: E402

DEFAULT_LOOKBACK_DAYS = 30
ARTICLES_CACHE_TTL = 300

_config = load_config()
_azure_client, _model = make_openai_client(_config)
try:
    # knowledge/はgitignore対象（社外秘）のため、デプロイ環境に無い場合がある。
    # サスティナビリティ専門家機能はcommon.is_enabled()で別途フラグ制御されているため、
    # ここでは起動を止めずに空データにフォールバックする。
    _expert_base = common.load_expert_base()
except FileNotFoundError:
    _expert_base = {}
_knowledge_store = get_knowledge_store(_config)
_competitor_client = SupabaseClient(_config)

_articles_cache = {"data": None, "fetched_at": 0.0}
_translate_pool = ThreadPoolExecutor(max_workers=12)

# 9テーマの表示用メタ情報（英語ラベル・カテゴリ色）。
# 色はeco-digest-sparkの元デザイン(7カテゴリ)のoklchパレットを踏襲しつつ、
# 実際のテーマ数(9)に合わせて拡張したもの。
THEME_META = {
    "水": {"label": "Water", "hue": "oklch(0.62 0.09 220)"},
    "気候変動・GHG": {"label": "Climate & GHG", "hue": "oklch(0.62 0.13 55)"},
    "容器包装": {"label": "Packaging", "hue": "oklch(0.6 0.13 90)"},
    "原料調達": {"label": "Raw Materials", "hue": "oklch(0.55 0.09 60)"},
    "生物多様性": {"label": "Biodiversity", "hue": "oklch(0.62 0.15 145)"},
    "人権": {"label": "Human Rights", "hue": "oklch(0.62 0.12 20)"},
    "健康": {"label": "Health", "hue": "oklch(0.55 0.15 300)"},
    "人的資本": {"label": "Human Capital", "hue": "oklch(0.5 0.09 260)"},
    "責任あるマーケティング": {"label": "Responsible Marketing", "hue": "oklch(0.55 0.1 200)"},
}
DEFAULT_HUE = "oklch(0.5 0.02 150)"

# S/A/B/C/Dランクを、UIの円形スコア表示(0-100)向けに変換するための代表値
IMPORTANCE_SCORE = {"S": 96, "A": 84, "B": 66, "C": 46, "D": 26}

# 主体(11)・横断(7)タグの英語表示ラベル。件数が少ない固定語彙なので、
# LLM翻訳ではなく静的な対訳表で返す（英語UI表示用）。
SUBJECT_LABEL_EN = {
    "競合企業": "Competitors",
    "その他企業": "Other Companies",
    "バリューチェーン企業": "Value Chain Companies",
    "規制当局・政府": "Regulators / Government",
    "国際機関": "International Organizations",
    "基準設定機関": "Standard-Setting Bodies",
    "業界団体・イニシアチブ": "Industry Associations / Initiatives",
    "NGO・市民団体": "NGOs / Civil Society",
    "投資家・金融機関": "Investors / Financial Institutions",
    "研究機関・大学": "Research Institutions / Universities",
    "メディア・データ提供機関": "Media / Data Providers",
}
CROSS_LABEL_EN = {
    "情報開示": "Disclosure",
    "ESG評価・サステナブルファイナンス": "ESG Ratings / Sustainable Finance",
    "地政学・マクロ規制環境": "Geopolitics / Macro Regulation",
    "エンフォースメント・訴訟": "Enforcement / Litigation",
    "グリーンウォッシュ": "Greenwashing",
    "サプライチェーンDD": "Supply Chain Due Diligence",
    "物理的環境イベント": "Physical Environmental Events",
}


def _get_articles() -> list:
    now = time.time()
    if _articles_cache["data"] is None or now - _articles_cache["fetched_at"] > ARTICLES_CACHE_TTL:
        _articles_cache["data"] = core.fetch_dashboard_articles(_config, DEFAULT_LOOKBACK_DAYS)
        _articles_cache["fetched_at"] = now
    return _articles_cache["data"]


def _warm_translation_cache(articles: list, lang: str) -> None:
    """一覧表示で必要なタイトル・要約の翻訳を並列実行してキャッシュを温める。
    翻訳はI/O待ちが支配的なため、逐次実行だと数百件で数分かかってしまう
    （特にsummary_shortは常に日本語で生成されるため、英語UIでは実質全件が翻訳対象になる）。"""
    futures = []
    for a in articles:
        if ("title", lang, a["article_id"]) not in core._short_cache:
            futures.append(_translate_pool.submit(
                core.translate_title, _azure_client, _model, a["article_id"], a["title"], lang))
        if ("summary", lang, a["article_id"]) not in core._short_cache:
            summary_raw = a.get("summary_short") or a.get("importance_reason") or ""
            futures.append(_translate_pool.submit(
                core.translate_summary, _azure_client, _model, a["article_id"], summary_raw, lang))
    for f in futures:
        f.result()


def _to_ui_article(a: dict, engagement: dict, lang: str = "ja", with_body: bool = False) -> dict:
    theme = a["themes"][0] if a.get("themes") else "その他"
    level = a.get("importance_level")
    eng = engagement.get(a["article_id"], {})
    summary_raw = a.get("summary_short") or a.get("importance_reason") or ""
    sector_ja = (a.get("subject_tags") or [""])[0]
    tags_ja = a.get("cross_tags", [])
    if lang == "en":
        sector = SUBJECT_LABEL_EN.get(sector_ja, sector_ja) if sector_ja else ""
        tags = [CROSS_LABEL_EN.get(t, t) for t in tags_ja]
    else:
        sector = sector_ja
        tags = tags_ja
    out = {
        "id": a["article_id"],
        "title": core.translate_title(_azure_client, _model, a["article_id"], a["title"], lang),
        "summary": core.translate_summary(_azure_client, _model, a["article_id"], summary_raw, lang),
        "category": theme,
        "source": a.get("publisher") or "",
        "sector": sector,
        "publishedAt": a.get("published_at") or "",
        "importance": IMPORTANCE_SCORE.get(level, 50),
        "importanceLevel": level or "",
        "trending": level in ("S", "A"),
        "url": a.get("url") or "",
        "tags": tags,
        "materialityCodes": a.get("materiality_codes", []),
        "likesCount": eng.get("likes_count", 0),
        "readsCount": eng.get("reads_count", 0),
    }
    if with_body:
        body = a.get("extracted_text") or ""
        body_translated = core.translate_body(_azure_client, _model, a["article_id"], body, lang) if body else ""
        out["body"] = [p for p in body_translated.split("\n") if p.strip()]
    return out


app = FastAPI(title="Sustainability Dashboard API")

app.add_middleware(
    CORSMiddleware,
    # eco-digest-spark(vite-tanstack-config)はサンドボックス検出でポートを自動選択するため、
    # Viteの既定である5173だけでなく、実際に使われている8080/8081も許可する
    allow_origins=[
        "http://127.0.0.1:5173", "http://localhost:5173",
        "http://127.0.0.1:8080", "http://localhost:8080",
        "http://127.0.0.1:8081", "http://localhost:8081",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "chatEnabled": common.is_enabled(_config) and _azure_client is not None}


@app.get("/api/categories")
def get_categories():
    names = core.theme_options(_config)
    articles = _get_articles()
    counts: dict = {}
    for a in articles:
        for t in a.get("themes", []):
            counts[t] = counts.get(t, 0) + 1
    return [
        {
            "id": name,
            "label": THEME_META.get(name, {}).get("label", name),
            "labelJa": name,
            "hue": THEME_META.get(name, {}).get("hue", DEFAULT_HUE),
            "count": counts.get(name, 0),
        }
        for name in names
    ]


@app.get("/api/articles")
def list_articles(since_days: int = DEFAULT_LOOKBACK_DAYS, themes: str = "", q: str = "", lang: str = "ja"):
    articles = _get_articles()
    selected_themes = [t for t in themes.split(",") if t]
    filtered = core.apply_tag_filter(articles, selected_themes)

    keywords, nl_themes = [], []
    if q.strip():
        if _azure_client:
            intent = core.extract_search_intent(_azure_client, _model, q, core.theme_options(_config))
            keywords, nl_themes = intent.get("keywords", []), intent.get("themes", [])
        else:
            keywords = core.tokenize(q)
        filtered = core.apply_nl_search(filtered, keywords, nl_themes)

    _warm_translation_cache(filtered, lang)
    engagement = core.fetch_engagement_map(_config, [a["article_id"] for a in filtered])

    return {
        "total": len(articles),
        "filteredTotal": len(filtered),
        "keywords": keywords,
        "matchedThemes": nl_themes,
        "articles": [_to_ui_article(a, engagement, lang) for a in filtered],
    }


@app.get("/api/articles/{article_id}")
def get_article(article_id: str, lang: str = "ja"):
    articles = _get_articles()
    article = next((a for a in articles if a["article_id"] == article_id), None)
    if not article:
        raise HTTPException(status_code=404, detail="記事が見つかりません")
    engagement = core.fetch_engagement_map(_config, [article_id])
    return _to_ui_article(article, engagement, lang, with_body=True)


class EngagementRequest(BaseModel):
    liked: bool | None = None
    read: bool | None = None


@app.post("/api/articles/{article_id}/engagement")
def update_engagement(article_id: str, req: EngagementRequest):
    """いいね・読んだのトグルを行う。liked/read が True なら+1、Falseなら-1する
    （ブラウザのlocalStorageで管理しているON/OFF状態と対応させて呼び出す想定）。"""
    likes_delta = 1 if req.liked is True else (-1 if req.liked is False else 0)
    reads_delta = 1 if req.read is True else (-1 if req.read is False else 0)
    try:
        return core.increment_engagement(_config, article_id, likes_delta, reads_delta)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"いいね・読んだ機能にはDBマイグレーション(sql/2026-07-21_article_engagement_schema.sql)"
                   f"の適用が必要です: {type(e).__name__}: {e}",
        )


class TranslateRequest(BaseModel):
    texts: list[str]
    targetLang: str = "en"


@app.post("/api/translate")
def translate(req: TranslateRequest):
    """記事に紐づかない任意テキスト（サスティナAIのチャット履歴など）の翻訳。
    各テキストは既に対象言語であればそのまま返る（core.translate_plainの言語判定に依る）。"""
    if not _azure_client or not req.texts:
        return {"translations": req.texts}
    futures = [
        _translate_pool.submit(core.translate_plain, _azure_client, _model, t, req.targetLang)
        for t in req.texts
    ]
    return {"translations": [f.result() for f in futures]}


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    themes: list[str] = []
    lang: str = "ja"


@app.post("/api/chat")
def chat(req: ChatRequest):
    if not common.is_enabled(_config):
        raise HTTPException(status_code=400, detail="サスティナAIが無効化されています（config.jsonを確認してください）")
    if not _azure_client:
        raise HTTPException(status_code=400, detail="Azure OpenAI / OpenAI のAPIキーが設定されていません")

    articles = _get_articles()
    context_articles = core.apply_tag_filter(articles, req.themes) if req.themes else articles

    try:
        retrieved_docs = _knowledge_store.search(req.message, top_k=core.CHAT_CONTEXT_TOP_K)
    except Exception:
        retrieved_docs = []

    try:
        competitor_block = _build_competitor_chat_context(req.themes)
    except Exception:
        competitor_block = ""

    system_prompt = core.build_chat_system_prompt(context_articles, _expert_base, retrieved_docs, competitor_block)
    if req.lang == "en":
        system_prompt += (
            "\n\n# Response language\nRespond in English, regardless of the language of the "
            "source documents above (translate/synthesize as needed).\n"
        )
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": m.role, "content": m.content} for m in req.history]
    messages.append({"role": "user", "content": req.message})

    try:
        resp = _azure_client.chat.completions.create(model=_model, messages=messages, temperature=0.3)
        reply = resp.choices[0].message.content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    sources = [d["title"] for d in retrieved_docs[:3]]
    return {"reply": reply, "sources": sources}


# ─── 競合サステナビリティモニタリング（読み取り専用、eco-digest-sparkの「競合モニタリング」メニュー用） ───
# competitor_*テーブルへのクロール・分類・変更検知はcompetitor_crawler.py等の既存モジュールが担う。
# ここでは保存済みデータをUI向けに整形して返すだけで、判定・書き込みロジックは一切持たない。

def _competitor_company_map() -> dict:
    rows = _competitor_client.select("competitor_companies", {"select": "*"})
    return {c["company_id"]: c for c in rows}


def _change_to_ui(e: dict, companies: dict, target_records: dict = None) -> dict:
    company = companies.get(e["company_id"], {})
    after = (target_records or {}).get(e.get("after_record_id")) or {}
    return {
        "id": e["change_event_id"],
        "companyId": e["company_id"],
        "companyName": company.get("company_name", "?"),
        "companyNameEn": company.get("company_name_en"),
        "companyCategory": company.get("industry_category", ""),
        "recordType": e["record_type"],
        "changeType": e.get("change_type"),
        "direction": e.get("direction"),
        "summary": e.get("summary"),
        "reasoningSummary": e.get("reasoning_summary"),
        "changedFields": e.get("changed_fields") or [],
        "themes": after.get("themes") or [],
        "confidence": e.get("confidence"),
        "reviewRequired": e.get("review_required", False),
        "sourceUrl": after.get("source_url"),
        "createdAt": e.get("created_at"),
        "sourceUpdatedAt": after.get("source_updated_at"),
    }


def _initiative_to_ui(i: dict, companies: dict) -> dict:
    company = companies.get(i["company_id"], {})
    return {
        "id": i["initiative_id"],
        "companyId": i["company_id"],
        "companyName": company.get("company_name", "?"),
        "companyNameEn": company.get("company_name_en"),
        "companyCategory": company.get("industry_category", ""),
        "title": i.get("title"),
        "summary": i.get("summary"),
        "themes": i.get("themes") or [],
        "isNew": i.get("is_new", True),
        "detectedAt": i.get("detected_at"),
        "sourceUrl": i.get("source_url"),
    }


def _target_record_to_ui(r: dict, companies: dict) -> dict:
    company = companies.get(r["company_id"], {})
    fields = r.get("structured_fields") or {}
    return {
        "id": r["record_id"],
        "companyId": r["company_id"],
        "companyName": company.get("company_name", "?"),
        "companyNameEn": company.get("company_name_en"),
        "companyCategory": company.get("industry_category", ""),
        "recordType": r["record_type"],
        "title": r.get("title"),
        "themes": r.get("themes") or [],
        "targetValue": fields.get("target_value"),
        "baseYear": fields.get("base_year"),
        "targetYear": fields.get("target_year"),
        "scope": fields.get("scope"),
        "kpiDefinition": fields.get("kpi_definition"),
        "achievementStatus": fields.get("achievement_status"),
        "sourceUrl": r.get("source_url"),
        "sourceUpdatedAt": r.get("source_updated_at"),
    }


# チャット(/api/chat)に渡す競合コンテキストの上限件数。
# 記事側のCHAT_CONTEXT_LIMIT(30件)に準じて、プロンプトが肥大化しすぎないよう抑える。
CHAT_COMPETITOR_TARGETS_LIMIT = 60
CHAT_COMPETITOR_INITIATIVES_LIMIT = 25


def _build_competitor_chat_context(themes: list[str]) -> str:
    """専門家AIチャット用に、競合各社の現行目標・KPIと取組事例をテキストブロックにして返す。"""
    companies = _competitor_company_map()

    targets = _competitor_client.select("competitor_target_records", {
        "select": "*", "is_current": "eq.true",
        "record_type": "in.(TARGET,KPI)", "order": "extracted_at.desc",
    })
    if themes:
        targets = [r for r in targets if set(r.get("themes") or []) & set(themes)]
    targets = targets[:CHAT_COMPETITOR_TARGETS_LIMIT]

    initiatives = _competitor_client.select("competitor_initiatives", {
        "select": "*", "order": "detected_at.desc", "limit": str(CHAT_COMPETITOR_INITIATIVES_LIMIT * 3),
    })
    if themes:
        initiatives = [r for r in initiatives if set(r.get("themes") or []) & set(themes)]
    initiatives = initiatives[:CHAT_COMPETITOR_INITIATIVES_LIMIT]

    if not targets and not initiatives:
        return ""

    def company_label(company_id: str) -> str:
        c = companies.get(company_id, {})
        name = c.get("company_name", "?")
        category = c.get("industry_category", "")
        return f"{name}（{category}）" if category else name

    target_lines = []
    for r in targets:
        fields = r.get("structured_fields") or {}
        theme_str = "/".join(r.get("themes") or [])
        value = r.get("title") or fields.get("target_value") or "-"
        span = f"{fields.get('base_year', '-')}→{fields.get('target_year', '-')}"
        scope = fields.get("scope")
        scope_str = f", 対象: {scope}" if scope else ""
        target_lines.append(f"- [{company_label(r['company_id'])}] {theme_str}: {value} ({span}{scope_str})")

    initiative_lines = []
    for i in initiatives:
        theme_str = "/".join(i.get("themes") or [])
        tag = "新規" if i.get("is_new") else "更新"
        initiative_lines.append(
            f"- [{company_label(i['company_id'])}] {i.get('title', '-')}（{theme_str}、{tag}）: {i.get('summary', '')}"
        )

    blocks = []
    if target_lines:
        blocks.append("\n# 競合各社の目標・KPI（現行、データベースより）\n" + "\n".join(target_lines))
    if initiative_lines:
        blocks.append("\n# 競合各社の取組事例（データベースより）\n" + "\n".join(initiative_lines))
    return "\n".join(blocks)


@app.get("/api/competitors/overview")
def competitor_overview():
    reports = _competitor_client.select("monthly_reports", {
        "select": "*", "order": "report_month.desc", "limit": "1",
    })
    report = reports[0] if reports else None
    companies = _competitor_company_map()

    recent_events = _competitor_client.select("competitor_change_events", {
        "select": "*", "order": "created_at.desc", "limit": "5",
    })
    recent_initiatives = _competitor_client.select("competitor_initiatives", {
        "select": "*", "order": "detected_at.desc", "limit": "5",
    })

    summary_raw = (report or {}).get("summary_json") or {}
    summary = {
        "monitoredCompanies": summary_raw.get("monitored_companies", 0),
        "updatedCompanies": summary_raw.get("updated_companies", 0),
        "targetChangeCount": summary_raw.get("target_change_count", 0),
        "actualUpdateCount": summary_raw.get("actual_update_count", 0),
        "initiativeCount": summary_raw.get("initiative_count", 0),
        "reviewPendingCount": summary_raw.get("review_pending_count", 0),
    }
    trends = [{
        "title": tr.get("title"), "summary": tr.get("summary"),
        "relatedCompanyNames": tr.get("related_company_names", []),
        "relatedThemes": tr.get("related_themes", []),
        "confidence": tr.get("confidence"),
    } for tr in ((report or {}).get("cross_company_trends_json") or [])]

    return {
        "reportMonth": report["report_month"] if report else None,
        "summary": summary,
        "crossCompanyTrends": trends,
        "recentChanges": [_change_to_ui(e, companies) for e in recent_events],
        "topInitiatives": [_initiative_to_ui(i, companies) for i in recent_initiatives],
    }


@app.get("/api/competitors/changes")
def competitor_changes(company_id: str = "", theme: str = "", record_type: str = "", since_days: int = 90,
                        date_field: str = "createdAt", sort_dir: str = "desc",
                        date_from: str = "", date_to: str = ""):
    params = {"select": "*", "order": "created_at.desc"}
    if company_id:
        params["company_id"] = f"eq.{company_id}"
    if record_type:
        params["record_type"] = f"eq.{record_type}"
    if not date_from and not date_to:
        # 明示的な期間指定が無い場合のみ、従来通りsince_daysで当社取得日を絞り込む
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        params["created_at"] = f"gte.{cutoff}"
    events = _competitor_client.select("competitor_change_events", params)

    after_ids = list({e["after_record_id"] for e in events if e.get("after_record_id")})
    target_records = {}
    if after_ids:
        rows = _competitor_client.select("competitor_target_records", {
            "select": "record_id,source_url,title,themes,source_updated_at",
            "record_id": f"in.({','.join(after_ids)})",
        })
        target_records = {r["record_id"]: r for r in rows}

    if theme:
        events = [e for e in events
                  if theme in (target_records.get(e.get("after_record_id"), {}).get("themes") or [])]

    changes = [_change_to_ui(e, _competitor_company_map(), target_records) for e in events]

    # 掲載日(sourceUpdatedAt)は情報源にmetaタグが無ければNULLになりうるため、
    # 取得日(createdAt)と別軸として、DB側JOINではなくここでソート・期間フィルタする
    date_key = "sourceUpdatedAt" if date_field == "sourceUpdatedAt" else "createdAt"
    if date_from:
        changes = [c for c in changes if c.get(date_key) and c[date_key] >= date_from]
    if date_to:
        changes = [c for c in changes if c.get(date_key) and c[date_key] <= date_to]
    changes.sort(key=lambda c: c.get(date_key) or "", reverse=(sort_dir != "asc"))

    return {"changes": changes}


@app.get("/api/competitors/targets")
def competitor_targets(company_id: str = "", theme: str = ""):
    params = {
        "select": "*", "is_current": "eq.true",
        "record_type": "in.(TARGET,KPI)", "order": "extracted_at.desc",
    }
    if company_id:
        params["company_id"] = f"eq.{company_id}"
    records = _competitor_client.select("competitor_target_records", params)
    if theme:
        records = [r for r in records if theme in (r.get("themes") or [])]
    companies = _competitor_company_map()
    return {"targets": [_target_record_to_ui(r, companies) for r in records]}


@app.get("/api/competitors/initiatives")
def competitor_initiatives_list(company_id: str = "", theme: str = "", is_new: str = ""):
    params = {"select": "*", "order": "detected_at.desc"}
    if company_id:
        params["company_id"] = f"eq.{company_id}"
    rows = _competitor_client.select("competitor_initiatives", params)
    if theme:
        rows = [r for r in rows if theme in (r.get("themes") or [])]
    if is_new == "true":
        rows = [r for r in rows if r.get("is_new")]
    elif is_new == "false":
        rows = [r for r in rows if not r.get("is_new")]
    companies = _competitor_company_map()
    return {"initiatives": [_initiative_to_ui(r, companies) for r in rows]}


@app.get("/api/competitors/companies")
def competitor_companies_list():
    companies = _competitor_client.select("competitor_companies", {
        "select": "*", "is_own_company": "eq.false", "order": "display_order.asc",
    })
    targets = _competitor_client.select("competitor_target_records", {
        "select": "company_id", "is_current": "eq.true",
    })
    initiatives = _competitor_client.select("competitor_initiatives", {"select": "company_id"})
    target_counts = Counter(t["company_id"] for t in targets)
    initiative_counts = Counter(i["company_id"] for i in initiatives)

    return {
        "companies": [{
            "id": c["company_id"], "name": c["company_name"], "nameEn": c.get("company_name_en"),
            "category": c.get("industry_category", ""), "displayOrder": c.get("display_order") or 0,
            "targetCount": target_counts.get(c["company_id"], 0),
            "initiativeCount": initiative_counts.get(c["company_id"], 0),
        } for c in companies],
    }


@app.get("/api/competitors/companies/{company_id}")
def competitor_company_detail(company_id: str):
    companies = _competitor_client.select("competitor_companies", {
        "company_id": f"eq.{company_id}", "limit": "1",
    })
    if not companies:
        raise HTTPException(status_code=404, detail="企業が見つかりません")
    company = companies[0]
    company_map = {company_id: company}

    targets = _competitor_client.select("competitor_target_records", {
        "select": "*", "company_id": f"eq.{company_id}", "is_current": "eq.true",
        "order": "extracted_at.desc",
    })
    initiatives = _competitor_client.select("competitor_initiatives", {
        "select": "*", "company_id": f"eq.{company_id}", "order": "detected_at.desc",
    })
    events = _competitor_client.select("competitor_change_events", {
        "select": "*", "company_id": f"eq.{company_id}", "order": "created_at.desc", "limit": "20",
    })
    sources = _competitor_client.select("competitor_sources", {
        "select": "*", "company_id": f"eq.{company_id}",
    })

    return {
        "id": company["company_id"], "name": company["company_name"],
        "nameEn": company.get("company_name_en"), "category": company.get("industry_category", ""),
        "targets": [_target_record_to_ui(r, company_map) for r in targets],
        "initiatives": [_initiative_to_ui(i, company_map) for i in initiatives],
        "changeHistory": [_change_to_ui(e, company_map) for e in events],
        "sources": [{"id": s["source_id"], "url": s["source_url"], "type": s["source_type"]} for s in sources],
    }


@app.get("/api/competitors/notifications/unread-count")
def competitor_unread_count():
    digests = _competitor_client.select("competitor_daily_alert_digests", {
        "select": "digest_id", "review_status": "eq.review_required",
    })
    reports = _competitor_client.select("monthly_reports", {
        "select": "report_id", "status": "eq.GENERATED",
    })
    return {"count": len(digests) + len(reports)}


if __name__ == "__main__":
    import os
    import uvicorn
    # ローカル開発では127.0.0.1のみ、コンテナ内ではAPI_HOST=0.0.0.0を環境変数で指定する
    host = os.environ.get("API_HOST", "127.0.0.1")
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
