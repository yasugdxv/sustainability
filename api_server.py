"""
サステナビリティ記事ダッシュボード React版フロントエンド用バックエンドAPI。

eco-digest-spark (React/Vite) から呼び出される。データ取得・翻訳・検索・チャットロジックは
sustainability_dashboard_core.py 経由で共有する。旧Streamlitプロトタイプ
（sustainability_dashboard_app.py）は React 版へ機能移行済みのため _removed_20260827/ へ退避済み。

起動: python api_server.py  (http://127.0.0.1:8000)
"""
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import load_config, SupabaseClient  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
from sustainability_knowledge_store import get_knowledge_store  # noqa: E402
import sustainability_dashboard_core as core  # noqa: E402
import articles_paging  # noqa: E402
import sustainability_chat_geo_service  # noqa: E402
import cross_domain_intelligence_service  # noqa: E402
import strategic_question_responses as sqr  # noqa: E402
import decision_insight_service  # noqa: E402

DEFAULT_LOOKBACK_DAYS = 30
ARTICLES_CACHE_TTL = 300

_config = load_config()
_azure_client, _model = make_openai_client(_config)
_expert_base = common.load_expert_base()
_knowledge_store = get_knowledge_store(_config)
_competitor_client = SupabaseClient(_config)

try:
    _cached_translation_count = core.init_translation_cache(_config)
    print(f"[起動] 翻訳キャッシュを{_cached_translation_count}件読み込みました")
except Exception as e:
    # translation_cacheテーブル未適用の環境でも起動自体は継続する（従来通りメモリのみで動作）
    print(f"[起動] 翻訳キャッシュの読み込みをスキップしました: {type(e).__name__}: {e}")

# since_days値ごとにキャッシュする（検索画面の期間フィルターで異なる日数が
# 指定されるため、単一エントリのキャッシュだと常に同じ日数で上書きされてしまう）
_articles_cache: dict = {}

# 2026-09-29: 本番をGunicorn複数ワーカー化したところ、_articles_cache等の
# プロセス内(メモリ)キャッシュがワーカーごとに独立してしまい、リクエストがどの
# ワーカーに振り分けられるかによって「温まっている/いない」がバラつき、体感速度が
# 改善しない事象が発生した。同一コンテナ内であればワーカー間でファイルシステムを
# 共有しているため、ディスク上のファイルを介して補助的にキャッシュを共有する層
# （common.shared_cache_get/set、run_daily.pyのバッチ処理からも使う）を挟む。
#
# ファイル側のTTLは、メモリ側のARTICLES_CACHE_TTL(5分)より大幅に長い値にしている。
# 記事データの実体は日次バッチ(run_daily.py、warm_dashboard_cache.py)でしか
# 更新されないため、ファイルは「前回バッチ〜次回バッチまで」有効とみなしてよい
# （バッチ未実行環境向けの保険として、5分キャッシュのフォールバック経路自体は残す）。
SHARED_ARTICLES_CACHE_TTL = 25 * 3600

# competitor_change_events取得結果(target_records解決込み)のキャッシュ。
# after_record_id経由でcompetitor_target_recordsをchunk取得する処理が、
# 未キャッシュ・毎回同期実行だと数秒〜十数秒かかり、フロント側のfetchが
# タイムアウトして「変更なし」と誤表示される不具合があったため追加(2026-09-14)
_competitor_changes_cache: dict = {}
COMPETITOR_CHANGES_CACHE_TTL = 300
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


def _get_articles(since_days: int = DEFAULT_LOOKBACK_DAYS) -> list:
    now = time.time()
    entry = _articles_cache.get(since_days)
    if entry is not None and now - entry["fetched_at"] <= ARTICLES_CACHE_TTL:
        return entry["data"]

    shared_key = f"articles:{since_days}"
    data = common.shared_cache_get(shared_key, SHARED_ARTICLES_CACHE_TTL)
    if data is None:
        data = core.fetch_dashboard_articles(_config, since_days)
        common.shared_cache_set(shared_key, data)
    _articles_cache[since_days] = {"data": data, "fetched_at": now}
    return data


def _warm_translation_cache(articles: list, lang: str) -> None:
    """一覧表示で必要なタイトル・要約の翻訳をバックグラウンドで並列実行しキャッシュを温める。
    翻訳はI/O待ちが支配的なため、逐次実行だと数百件で数分かかってしまう
    （特にsummary_shortは常に日本語で生成されるため、英語UIでは実質全件が翻訳対象になる）。

    2026-08-27修正: 以前はここでfuture.result()を待っており、cold cache時に
    /api/articles全体が数分ブロックされていた（実測: 非日本語タイトル2165件で約250秒）。
    一覧表示（list_articles）は_to_ui_article(..., blocking=False)で「キャッシュに無ければ
    原文のまま返す」経路に変えたため、ここではfire-and-forgetで投げるだけでよい
    （このリクエストのレスポンスは原文のまま返り、次回以降のリクエストから翻訳済みで返る）。"""
    # 2026-10-26: 日次バッチが先回りで保存した翻訳を、メモリへ取り込んでから不足分だけを判定する
    # （バッチの結果に、起動中のサーバーが気づけず、保存済みの記事まで再翻訳・原文表示になるのを防ぐ）
    missing_ids = [a["article_id"] for a in articles
                   if ("title", lang, a["article_id"]) not in core._short_cache
                   or ("summary", lang, a["article_id"]) not in core._short_cache]
    if missing_ids:
        try:
            core.prefetch_translations_from_db(missing_ids, lang)
        except Exception:
            pass
    for a in articles:
        if ("title", lang, a["article_id"]) not in core._short_cache:
            _translate_pool.submit(
                core.translate_title, _azure_client, _model, a["article_id"], a["title"], lang)
        if ("summary", lang, a["article_id"]) not in core._short_cache:
            summary_raw = a.get("summary_short") or a.get("importance_reason") or ""
            _translate_pool.submit(
                core.translate_summary, _azure_client, _model, a["article_id"], summary_raw, lang)


def _to_ui_article(a: dict, engagement: dict, lang: str = "ja", with_body: bool = False,
                    blocking: bool = True) -> dict:
    """blocking=False（一覧表示専用）の場合、タイトル・要約はキャッシュ済みならそれを、
    未キャッシュなら原文をそのまま返す（LLM呼び出しをその場で待たない）。翻訳自体は
    _warm_translation_cache()がバックグラウンドで進める。単一記事の詳細表示
    （get_article、with_body=True）は従来通りblocking=True（既定）で確実に翻訳する。"""
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
    if blocking:
        title = core.translate_title(_azure_client, _model, a["article_id"], a["title"], lang)
        summary = core.translate_summary(_azure_client, _model, a["article_id"], summary_raw, lang)
    else:
        title = core.get_short_translation_nonblocking("title", a["article_id"], a["title"], lang)
        summary = core.get_short_translation_nonblocking("summary", a["article_id"], summary_raw, lang)
    scores = a.get("importance_scores") or {}
    out = {
        "id": a["article_id"],
        "title": title,
        "summary": summary,
        "category": theme,
        "categories": a.get("themes") or [theme],
        "source": a.get("publisher") or "",
        "sector": sector,
        "publishedAt": a.get("published_at") or "",
        "importance": IMPORTANCE_SCORE.get(level, 50),
        "importanceLevel": level or "",
        "importanceTotal": a.get("importance_total_score"),
        "importanceBreakdown": [{"id": k, "score": v} for k, v in scores.items()],
        "trending": level in ("S", "A"),
        "url": a.get("url") or "",
        "tags": tags,
        "subThemes": a.get("theme_subtags", []),
        "materialityCodes": a.get("materiality_codes", []),
        "likesCount": eng.get("likes_count", 0),
        "readsCount": eng.get("reads_count", 0),
        "provenance": {
            "sourceDepartment": "sustainability",
            "sourceAgent": None,
            "sourceType": "article_analysis",
            "assessmentTitle": None,
            "requestQuestion": None,
            "confidence": None,
            "reviewStatus": "needs_review" if a.get("needs_review") else "ai_only",
            "asOf": a.get("published_at") or "",
            "references": [],
            "externalCallId": None,
            "reuseType": "none",
        },
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
        "https://app-jp1-sjp-bn-susiq-frontend-001.azurewebsites.net",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    # backendはAzure Private Endpoint経由(プライベートIP)でのみ到達可能なため、
    # 公開アドレスのfrontendから呼ぶとChromeのPrivate Network Accessでブロックされる。
    # 明示的に許可する(社内ネットワーク/VPN経由の利用が前提)。
    allow_private_network=True,
)
# 記事一覧等が数千件規模になると無圧縮で約10MBのJSONになり、本番環境での転送時間が
# 体感速度を大きく悪化させていた（実測: 圧縮なしで9.7MB）ため、応答を圧縮する。
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/api/health")
def health():
    return {"status": "ok", "chatEnabled": common.is_enabled(_config) and _azure_client is not None}


@app.get("/api/categories")
def get_categories(since_days: int = DEFAULT_LOOKBACK_DAYS):
    names = core.theme_options(_config)
    articles = _get_articles(since_days)
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


@app.get("/api/subcategories")
def get_subcategories(since_days: int = DEFAULT_LOOKBACK_DAYS):
    """テーマの小分類タグ一覧（検索画面の詳細タグ絞り込み用）。
    英語対訳表は無い（121件と件数が多く、対訳表を作るとメンテコストが見合わないため）。
    英語UIでも日本語タグ名のまま表示する"""
    subthemes = core.subtheme_options(_config)
    articles = _get_articles(since_days)
    counts: dict = {}
    for a in articles:
        for st in a.get("theme_subtags", []):
            counts[st] = counts.get(st, 0) + 1
    return [
        {
            "id": s["id"], "label": s["label"], "labelJa": s["label"],
            "parent": s["parent"], "count": counts.get(s["id"], 0),
        }
        for s in subthemes
    ]


@app.get("/api/importance-rubric")
def get_importance_rubric():
    """重要度スコアの内訳（7項目・各0-5点）の名称・説明・スコア別基準。
    記事データではなく評価基準そのもの（更新頻度が低い静的参照データ）なので、
    articlesキャッシュとは別枠でフロントエンド側の長めのstaleTimeに任せる"""
    return core.importance_rubric(_config)


# 検索語(q)無しの一覧表示（トップページ等が使う既定の呼び出し）は、パラメータが
# 同じであれば5分間はレスポンス全体を使い回す。検索語ありはLLMによる検索意図抽出が
# 入力ごとに変わるためキャッシュ対象外とする。
# 追加理由(2026-09-28): CPU負荷の高いUI変換処理(_to_ui_article等)が毎回全件分
# 再実行されており、本番で同時リクエストが重なるとCPU(GIL)が詰まり、articles自体は
# もちろん/api/categories等の軽いエンドポイントまで数十秒待たされる事象を確認したため。
_articles_response_cache: dict = {}


# 検索語ごとの意図抽出(LLM)の結果。無限スクロールで2ページ目以降も同じ検索語で呼ばれるため、
# ページごとにLLMを呼び直さないよう使い回す（件数が増えすぎないよう上限つき）。
_search_intent_cache: dict = {}


def _search_intent(q: str) -> dict:
    cached = _search_intent_cache.get(q)
    if cached is not None:
        return cached
    intent = core.extract_search_intent(_azure_client, _model, q, core.theme_options(_config))
    if len(_search_intent_cache) >= 200:
        _search_intent_cache.clear()
    _search_intent_cache[q] = intent
    return intent


@app.get("/api/articles")
def list_articles(since_days: int = DEFAULT_LOOKBACK_DAYS, themes: str = "", q: str = "", lang: str = "ja",
                  limit: int = 0, cursor: str = "", sort: str = "latest", min_importance: int = 0):
    """記事一覧。limit を付けると無限スクロール用のページ分割（articles_paging.py）になり、
    sort(importance/latest/likes/reads/trending)・min_importance・cursor で絞り・並べ替え・続きを指定する。
    limit を付けない呼び出しは従来どおり全件を返す（フロントとバックを別々にデプロイしても壊れない）。"""
    paged = limit > 0
    cache_key = (since_days, themes, lang)
    shared_key = f"articles_response:{since_days}:{themes}:{lang}"
    if not q.strip() and not paged:
        now = time.time()
        cached = _articles_response_cache.get(cache_key)
        if cached is not None and now - cached["fetched_at"] <= ARTICLES_CACHE_TTL:
            return cached["data"]
        shared = common.shared_cache_get(shared_key, ARTICLES_CACHE_TTL)
        if shared is not None:
            _articles_response_cache[cache_key] = {"data": shared, "fetched_at": now}
            return shared

    articles = _get_articles(since_days)
    selected_themes = [t for t in themes.split(",") if t]
    filtered = core.apply_tag_filter(articles, selected_themes)

    keywords, nl_themes = [], []
    cross_domain_intelligence = []
    if q.strip():
        if _azure_client:
            intent = _search_intent(q)
            keywords, nl_themes = intent.get("keywords", []), intent.get("themes", [])
        else:
            keywords = core.tokenize(q)
        filtered = core.apply_nl_search(filtered, keywords, nl_themes)

        try:
            # 2ページ目以降(cursorあり)は他部門の既存知見を取り直さない（1ページ目だけに付ける）。
            # Phase B: Searchは人間が既存Cross-domain Intelligenceを直接見て判断するため、
            # 十分性判定LLMは呼ばずGatewayの候補をそのまま返す（capability="search"の
            # Kill SwitchはGateway自身が確認する。Failure Isolation: 失敗しても記事検索
            # 本体には一切影響しない）。
            if not cursor:
                sources = cross_domain_intelligence_service.retrieve_existing_intelligence(
                    _competitor_client, _config, domains=["geopolitics"], keywords=keywords,
                    themes=nl_themes, limit=10, capability="search")
                cross_domain_intelligence = [core._to_cross_domain_item(s) for s in sources]
        except Exception:
            cross_domain_intelligence = []

    if paged:
        # 重要度の絞り込み → 集計(絞り込み後の全件分) → 並べ替え+続きの切り出し → 返す分だけ変換・翻訳
        importance_fn = lambda level: IMPORTANCE_SCORE.get(level, 50)  # noqa: E731
        stats = articles_paging.build_stats(filtered, importance_fn)
        if min_importance > 0:
            filtered = [a for a in filtered if importance_fn(a.get("importance_level")) >= min_importance]
        # いいね・読んだ件数はサーバー内にキャッシュ済み（_all_engagement）。並べ替えキーに使うので全件分を引く
        engagement = core.fetch_engagement_map(_config, [a["article_id"] for a in filtered])
        page = articles_paging.paginate(filtered, sort, limit, cursor, importance_fn, engagement)
        _warm_translation_cache(page["items"], lang)  # 翻訳も返す分だけ
        return {
            "total": len(articles),
            "filteredTotal": len(filtered),
            "keywords": keywords,
            "matchedThemes": nl_themes,
            "articles": [_to_ui_article(a, engagement, lang, blocking=False) for a in page["items"]],
            "crossDomainIntelligence": cross_domain_intelligence,
            "hasMore": page["hasMore"],
            "nextCursor": page["nextCursor"],
            "stats": stats,
        }

    _warm_translation_cache(filtered, lang)
    engagement = core.fetch_engagement_map(_config, [a["article_id"] for a in filtered])

    result = {
        "total": len(articles),
        "filteredTotal": len(filtered),
        "keywords": keywords,
        "matchedThemes": nl_themes,
        "articles": [_to_ui_article(a, engagement, lang, blocking=False) for a in filtered],
        "crossDomainIntelligence": cross_domain_intelligence,
    }
    if not q.strip():
        _articles_response_cache[cache_key] = {"data": result, "fetched_at": time.time()}
        common.shared_cache_set(shared_key, result)
    return result


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
        result = core.increment_engagement(_config, article_id, likes_delta, reads_delta)
        # 共有ファイルキャッシュ全体(common.shared_cache_clear_all)はここでは呼ばない。
        # 呼ぶと、日次バッチが温めた重い記事一覧キャッシュ(articles:*)まで巻き込んで
        # 消えてしまい、次のリクエストが再び数十秒かかる高コストな再取得を踏んでしまう。
        # いいね/読んだ数の反映が他ワーカー・他プロセスに伝播するまで最大5分(このプロセス内
        # キャッシュのTTL)遅れる程度は許容する。
        _articles_response_cache.clear()
        return result
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
    """記事に紐づかない任意テキスト（サステナAIのチャット履歴など）の翻訳。
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
        raise HTTPException(status_code=400, detail="サステナAIが無効化されています（config.jsonを確認してください）")
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

    try:
        # Phase B: 他部門（現状Geopolitics）Intelligenceがmateriallyに必要な質問の場合のみ
        # 自動的に既存Intelligence優先で参照し、参考コンテキストとして追加する。
        # Kill Switch OFF・判定「不要」・他部門障害等、いかなる場合も空文字列/使用なしが
        # 返るだけでChat自体には影響しない（Failure Isolation）。
        recent_history_text = "\n".join(f"{m.role}: {m.content}" for m in req.history[-4:])
        geo_context_block, geo_meta = sustainability_chat_geo_service.get_geo_chat_context_with_meta(
            _config, _competitor_client, _azure_client, _model, req.message, recent_history_text,
            themes=req.themes)
    except Exception:
        geo_context_block, geo_meta = "", {"used": False}
    cross_domain_intelligence = geo_meta.get("crossDomainIntelligence") or []

    try:
        # Weekly Strategic Question由来の「組織内の判断傾向」。正式方針とは明確に分離し、
        # guardrail_label付きで参考情報としてのみ渡す（Failure Isolation）
        decision_signal_block = decision_insight_service.build_decision_insight_chat_block(
            _competitor_client, _config, themes=req.themes or None)
    except Exception:
        decision_signal_block = ""

    system_prompt = core.build_chat_system_prompt(
        context_articles, _expert_base, retrieved_docs, competitor_block, geo_context_block,
        decision_signal_block)
    if req.lang == "en":
        system_prompt += (
            "\n\n# Response language\nRespond in English, regardless of the language of the "
            "source documents above (translate/synthesize as needed).\n"
        )
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": m.role, "content": m.content} for m in req.history]
    messages.append({"role": "user", "content": req.message})

    try:
        resp = _azure_client.chat.completions.create(model=_model, messages=messages)
        reply = resp.choices[0].message.content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    sources = [d["title"] for d in retrieved_docs[:3]]
    return {"reply": reply, "sources": sources, "crossDomainIntelligence": cross_domain_intelligence}


# ─── Weekly Strategic Question: 回答収集（本ファイル唯一のブラウザ向けHTMLエンドポイント） ───
# トークン(t)は実質Bearer Tokenと同等に扱う。第三者JS・外部画像は置かず、Cache-Control/
# Referrer-Policyでトークン付きURLの漏洩経路を塞ぐ。GETは記録しない
# （メールセキュリティ製品によるリンク自動アクセスを回答として誤認しないための2段階方式:
#  GET=確認ページ表示のみ、POST=実際の記録）。

_NO_LEAK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

_ERROR_MESSAGES = {
    "not_found": ("質問が見つかりません", 404),
    "invalid_token": ("このリンクは無効です", 404),
    "invalid_option": ("選択肢が正しくありません", 400),
    "closed": ("この質問は回答受付を終了しました", 410),
}


def _render_sq_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title></head>
<body style="font-family:'Hiragino Sans','Meiryo',sans-serif;color:#0f172a;max-width:480px;
margin:60px auto;padding:0 20px;">{body}</body></html>"""


def _render_error_page(error: str) -> str:
    message, _ = _ERROR_MESSAGES.get(error, ("エラーが発生しました", 500))
    return _render_sq_page(message, f'<p style="font-size:15px;">{message}</p>')


def _render_confirm_page(question: dict, chosen_option: dict, q: str, o: str, t: str) -> str:
    return _render_sq_page("回答の確認", f"""
    <h1 style="font-size:16px;">回答の確認</h1>
    <p style="font-size:13px;color:#475569;">{question['question_text']}</p>
    <p style="font-size:15px;font-weight:700;margin:16px 0;">
      選択: {chosen_option['option_code']}. {chosen_option['label']}
    </p>
    <form method="post" action="/api/strategic-questions/respond">
      <input type="hidden" name="q" value="{q}">
      <input type="hidden" name="o" value="{o}">
      <input type="hidden" name="t" value="{t}">
      <label style="font-size:13px;color:#334155;">理由や条件があれば教えてください（任意）</label><br>
      <textarea name="comment" rows="3" style="width:100%;margin:8px 0;padding:8px;
        border:1px solid #cbd5e1;border-radius:6px;font-size:13px;"></textarea><br>
      <button type="submit" style="padding:10px 20px;background:#0f766e;color:#fff;border:none;
        border-radius:6px;font-size:14px;font-weight:700;cursor:pointer;">この内容で回答する</button>
    </form>""")


def _render_thanks_page() -> str:
    return _render_sq_page("回答ありがとうございました",
                            '<p style="font-size:15px;">回答を受け付けました。ご協力ありがとうございました。</p>')


@app.get("/api/strategic-questions/respond", response_class=HTMLResponse)
def strategic_question_respond_confirm(q: str, o: str, t: str):
    validation = sqr.validate_token(_competitor_client, q, t, is_get=True)
    if "error" in validation:
        _, status = _ERROR_MESSAGES.get(validation["error"], ("エラー", 500))
        return HTMLResponse(_render_error_page(validation["error"]), status_code=status, headers=_NO_LEAK_HEADERS)
    chosen = sqr.get_option_or_none(_competitor_client, q, o)
    if chosen is None:
        return HTMLResponse(_render_error_page("invalid_option"), status_code=400, headers=_NO_LEAK_HEADERS)
    return HTMLResponse(_render_confirm_page(validation["question"], chosen, q, o, t), headers=_NO_LEAK_HEADERS)


@app.post("/api/strategic-questions/respond", response_class=HTMLResponse)
def strategic_question_respond_submit(q: str = Form(...), o: str = Form(...),
                                       t: str = Form(...), comment: str = Form("")):
    validation = sqr.validate_token(_competitor_client, q, t)
    if "error" in validation:
        _, status = _ERROR_MESSAGES.get(validation["error"], ("エラー", 500))
        return HTMLResponse(_render_error_page(validation["error"]), status_code=status, headers=_NO_LEAK_HEADERS)
    chosen = sqr.get_option_or_none(_competitor_client, q, o)
    if chosen is None:
        return HTMLResponse(_render_error_page("invalid_option"), status_code=400, headers=_NO_LEAK_HEADERS)
    sqr.record_response(_competitor_client, validation, chosen["option_id"], comment.strip() or None)
    return HTMLResponse(_render_thanks_page(), headers=_NO_LEAK_HEADERS)


# ─── 競合サステナビリティモニタリング（読み取り専用、eco-digest-sparkの「競合モニタリング」メニュー用） ───
# competitor_*テーブルへのクロール・分類・変更検知はcompetitor_crawler.py等の既存モジュールが担う。
# ここでは保存済みデータをUI向けに整形して返すだけで、判定・書き込みロジックは一切持たない。

def _competitor_company_map() -> dict:
    rows = _competitor_client.select("competitor_companies", {"select": "*"})
    return {c["company_id"]: c for c in rows}


def _goal_category_map() -> dict:
    rows = _competitor_client.select("goal_categories", {"select": "goal_category_id,name,major_theme"})
    return {r["goal_category_id"]: r for r in rows}


def _change_to_ui(e: dict, companies: dict, target_records: dict = None, goal_categories: dict = None) -> dict:
    company = companies.get(e["company_id"], {})
    after = (target_records or {}).get(e.get("after_record_id")) or {}
    before = (target_records or {}).get(e.get("before_record_id")) or {}
    goal_category = (goal_categories or {}).get(after.get("goal_category_id"))
    before_source_url = before.get("source_url")
    after_source_url = after.get("source_url")
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
        "goalCategoryId": after.get("goal_category_id"),
        "goalCategoryName": goal_category.get("name") if goal_category else None,
        "goalCategoryTheme": goal_category.get("major_theme") if goal_category else None,
        "confidence": e.get("confidence"),
        "reviewRequired": e.get("review_required", False),
        "sourceUrl": after_source_url,
        "beforeSourceUrl": before_source_url,
        # before/afterで参照元文書自体が異なる場合、項目の出現・消失は会社側の開示内容の
        # 変更ではなく、比較対象の文書が違うことによる見かけ上の差の可能性があるため、
        # フロントエンド側で注記を出せるようフラグを渡す（両方URLが分かっている場合のみ判定）
        "crossDocumentComparison": bool(
            before_source_url and after_source_url and before_source_url != after_source_url
        ),
        "createdAt": e.get("created_at"),
        "sourceUpdatedAt": after.get("source_updated_at"),
    }


def _initiative_to_ui(i: dict, companies: dict, goal_categories: dict = None,
                       lang: str = "ja", with_body: bool = False) -> dict:
    company = companies.get(i["company_id"], {})
    goal_category = (goal_categories or {}).get(i.get("goal_category_id"))
    out = {
        "id": i["initiative_id"],
        "companyId": i["company_id"],
        "companyName": company.get("company_name", "?"),
        "companyNameEn": company.get("company_name_en"),
        "companyCategory": company.get("industry_category", ""),
        "title": i.get("title"),
        "summary": i.get("summary"),
        "themes": i.get("themes") or [],
        "goalCategoryId": i.get("goal_category_id"),
        "goalCategoryName": goal_category.get("name") if goal_category else None,
        "goalCategoryTheme": goal_category.get("major_theme") if goal_category else None,
        "isNew": i.get("is_new", True),
        "detectedAt": i.get("detected_at"),
        "sourceUrl": i.get("source_url"),
    }
    if with_body:
        # source_text(evidence_quote)が空の記事（過去にLLMが空文字を返しNULL化された
        # もの。2026-09-14にスキーマ側はminLength制約で今後の再発を防止済み）は、
        # 詳細ページの本文が完全に空になってしまうため、一覧と同じsummaryにフォールバックする
        body = (i.get("source_text") or "").strip() or (i.get("summary") or "")
        body_translated = core.translate_body(_azure_client, _model, i["initiative_id"], body, lang) if body else ""
        out["body"] = [p for p in body_translated.split("\n") if p.strip()]
    return out


def _target_record_to_ui(r: dict, companies: dict, goal_categories: dict = None) -> dict:
    company = companies.get(r["company_id"], {})
    fields = r.get("structured_fields") or {}
    goal_category = (goal_categories or {}).get(r.get("goal_category_id"))
    return {
        "id": r["record_id"],
        "companyId": r["company_id"],
        "companyName": company.get("company_name", "?"),
        "companyNameEn": company.get("company_name_en"),
        "companyCategory": company.get("industry_category", ""),
        "isOwnCompany": company.get("is_own_company", False),
        "recordType": r["record_type"],
        "title": r.get("title"),
        "themes": r.get("themes") or [],
        "goalCategoryId": r.get("goal_category_id"),
        "goalCategoryName": goal_category.get("name") if goal_category else None,
        "goalCategoryTheme": goal_category.get("major_theme") if goal_category else None,
        "targetValue": fields.get("target_value"),
        "numericValue": fields.get("numeric_value"),
        "unit": fields.get("unit"),
        "reductionRate": fields.get("reduction_rate"),
        "baseYear": fields.get("base_year"),
        "targetYear": fields.get("target_year"),
        "scope": fields.get("scope"),
        "boundary": fields.get("boundary"),
        "targetRegion": fields.get("target_region"),
        "targetMaterial": fields.get("target_material"),
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
    goal_categories = _goal_category_map()

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
        "recentChanges": [_change_to_ui(e, companies, goal_categories=goal_categories) for e in recent_events],
        "topInitiatives": [_initiative_to_ui(i, companies, goal_categories) for i in recent_initiatives],
    }


def _fetch_competitor_changes_base(company_id: str, record_type: str, since_days: int, unbounded: bool) -> tuple:
    """competitor_change_events + 紐づくcompetitor_target_records(after_record_id経由)を取得する。
    date_from/date_to指定時(unbounded=True)は元々DB側で期間を絞り込んでおらず、後段のPython側
    フィルタで対応しているため、company_id/record_typeが同じなら取得結果自体は
    since_daysに関わらず同一になる（キャッシュキーをsince_days=Noneとして共有する）"""
    cache_key = (company_id, record_type, None if unbounded else since_days)
    now = time.time()
    entry = _competitor_changes_cache.get(cache_key)
    if entry is not None and now - entry["fetched_at"] <= COMPETITOR_CHANGES_CACHE_TTL:
        return entry["events"], entry["target_records"]

    shared_key = f"competitor_changes:{cache_key}"
    shared = common.shared_cache_get(shared_key, COMPETITOR_CHANGES_CACHE_TTL)
    if shared is not None:
        _competitor_changes_cache[cache_key] = {**shared, "fetched_at": now}
        return shared["events"], shared["target_records"]

    params = {"select": "*", "order": "created_at.desc"}
    if company_id:
        params["company_id"] = f"eq.{company_id}"
    if record_type:
        params["record_type"] = f"eq.{record_type}"
    if not unbounded:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        params["created_at"] = f"gte.{cutoff}"
    events = _competitor_client.select("competitor_change_events", params)

    # before_record_id側も合わせて取得する。変更前後で参照元文書が異なるケース
    # （例: 詳細な目標一覧PDFと簡潔な保証報告書PDFを比較した結果、項目の有無だけが
    # 入れ替わって見える）があり、before側のsource_urlが無いと「本当に会社が
    # 内容を変えたのか、単に別文書と比較しただけなのか」がUI側で判別できないため。
    record_ids = {e["after_record_id"] for e in events if e.get("after_record_id")}
    record_ids |= {e["before_record_id"] for e in events if e.get("before_record_id")}
    record_ids = list(record_ids)
    target_records = {}
    if record_ids:
        # record_idsが多いと1つのin.(...)クエリのURLが長くなりすぎて414 URI Too Longに
        # なるため、chunk分割して取得する（article_analysisで実際に発生した問題と同種。
        # chunk自体は_select_in_chunks側で並列実行される）
        rows = common._select_in_chunks(_competitor_client, "competitor_target_records", {
            "select": "record_id,source_url,title,themes,source_updated_at,goal_category_id",
        }, "record_id", record_ids)
        target_records = {r["record_id"]: r for r in rows}

    _competitor_changes_cache[cache_key] = {"events": events, "target_records": target_records, "fetched_at": now}
    common.shared_cache_set(shared_key, {"events": events, "target_records": target_records})
    return events, target_records


@app.get("/api/competitors/changes")
def competitor_changes(company_id: str = "", theme: str = "", record_type: str = "", since_days: int = 90,
                        date_field: str = "createdAt", sort_dir: str = "desc",
                        date_from: str = "", date_to: str = ""):
    unbounded = bool(date_from or date_to)
    events, target_records = _fetch_competitor_changes_base(company_id, record_type, since_days, unbounded)

    if theme:
        events = [e for e in events
                  if theme in (target_records.get(e.get("after_record_id"), {}).get("themes") or [])]

    # companies/goal_categoriesはイベント件数分ループの中で毎回問い合わせるとDB呼び出しが
    # 件数倍に膨れ上がる（1710件で3420回の追加クエリになり応答が数十秒〜タイムアウトして
    # いた）ため、ループの外で1回だけ取得する
    companies = _competitor_company_map()
    goal_categories = _goal_category_map()
    changes = [_change_to_ui(e, companies, target_records, goal_categories)
               for e in events]

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
def competitor_targets(company_id: str = "", theme: str = "", goal_category: str = ""):
    params = {
        "select": "*", "is_current": "eq.true",
        "record_type": "in.(TARGET,KPI)", "order": "extracted_at.desc",
    }
    if company_id:
        params["company_id"] = f"eq.{company_id}"
    if goal_category:
        params["goal_category_id"] = f"eq.{goal_category}"
    records = _competitor_client.select("competitor_target_records", params)
    if theme:
        records = [r for r in records if theme in (r.get("themes") or [])]
    companies = _competitor_company_map()
    goal_categories = _goal_category_map()
    return {"targets": [_target_record_to_ui(r, companies, goal_categories) for r in records]}


@app.get("/api/competitors/initiatives")
def competitor_initiatives_list(company_id: str = "", theme: str = "", is_new: str = "", goal_category: str = ""):
    params = {"select": "*", "order": "detected_at.desc"}
    if company_id:
        params["company_id"] = f"eq.{company_id}"
    if goal_category:
        params["goal_category_id"] = f"eq.{goal_category}"
    rows = _competitor_client.select("competitor_initiatives", params)
    if theme:
        rows = [r for r in rows if theme in (r.get("themes") or [])]
    if is_new == "true":
        rows = [r for r in rows if r.get("is_new")]
    elif is_new == "false":
        rows = [r for r in rows if not r.get("is_new")]
    companies = _competitor_company_map()
    goal_categories = _goal_category_map()
    return {"initiatives": [_initiative_to_ui(r, companies, goal_categories) for r in rows]}


@app.get("/api/competitors/initiatives/{initiative_id}")
def competitor_initiative_detail(initiative_id: str, lang: str = "ja"):
    rows = _competitor_client.select("competitor_initiatives", {
        "select": "*", "initiative_id": f"eq.{initiative_id}",
    })
    if not rows:
        raise HTTPException(status_code=404, detail="取組事例が見つかりません")
    companies = _competitor_company_map()
    goal_categories = _goal_category_map()
    return _initiative_to_ui(rows[0], companies, goal_categories, lang=lang, with_body=True)


@app.get("/api/competitors/companies")
def competitor_companies_list():
    companies = _competitor_client.select("competitor_companies", {
        "select": "*", "order": "display_order.asc",
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
            "isOwnCompany": c.get("is_own_company", False),
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
    goal_categories = _goal_category_map()

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
        "targets": [_target_record_to_ui(r, company_map, goal_categories) for r in targets],
        "initiatives": [_initiative_to_ui(i, company_map, goal_categories) for i in initiatives],
        "changeHistory": [_change_to_ui(e, company_map, goal_categories=goal_categories) for e in events],
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


# ─── 週次メールレビュー（Streamlitのレビュー画面をReactへ移行） ─────────────────
# 認証はStreamlit版と同じDEV AUTH方式（identity.resolve_principal）のまま。
# 本番相当(DEV AUTH無効)では承認・却下・修正保存は常にfail-closedする。ロール管理は後続対応。
import identity  # noqa: E402
import sustainability_expert_api as expert_api  # noqa: E402
import weekly_email_report as wer  # noqa: E402


class WeeklyReviewBody(BaseModel):
    status: str  # approved | rejected | review_required
    reviewerFeedback: dict | None = None
    draftContent: dict | None = None
    testMode: bool = False  # True: 承認時、テスト受信者(is_test)だけへ送る。省略時は本番の受信者


@app.middleware("http")
async def _capture_bearer_token(request, call_next):
    """SSO: リクエストの Authorization: Bearer を、このリクエストの間だけ、identityへ渡す（2026-10-23追加）。
    トークンの検証は、承認系の_review_principal()(identity.resolve_principal)で行う"""
    reset = identity.set_request_bearer_token(identity.bearer_token_from_header(request.headers.get("authorization")))
    try:
        return await call_next(request)
    finally:
        identity.reset_request_bearer_token(reset)


def _review_principal():
    """(principal, error)を返す。解決できない場合principalはNone"""
    try:
        return identity.resolve_principal(dev_principal=identity.dev_principal_from_env(),
                                          bearer_token=identity.current_bearer_token()), None
    except identity.AuthenticationError as e:
        return None, str(e)


def _weekly_report_to_ui(r: dict, with_body: bool = False) -> dict:
    draft = r.get("draft_content") or {}
    out = {
        "id": r["report_id"],
        "subject": r.get("subject") or "",
        "periodStart": r.get("period_start"),
        "periodEnd": r.get("period_end"),
        "reviewStatus": r.get("review_status"),
        "status": r.get("status"),
        "errorMessage": r.get("error_message"),
        "articleCount": len(draft.get("articles", [])),
        "reviewerId": r.get("reviewer_id"),
        "sendStatus": r.get("send_status"),
    }
    if with_body:
        out["htmlBody"] = r.get("html_body") or ""
        out["draftContent"] = draft
    return out


@app.get("/api/review/principal")
def review_principal():
    principal, error = _review_principal()
    if principal is None:
        return {"ok": False, "canApprove": False, "error": error}
    return {"ok": True, "userId": principal.user_id, "displayName": principal.display_name,
            "authSource": principal.authentication_source, "canApprove": identity.can_approve(principal),
            "error": None}


@app.get("/api/weekly-reports")
def weekly_reports(status: str = ""):
    rows = wer.list_reports(_competitor_client, review_status=status or None)
    return {"reports": [_weekly_report_to_ui(r) for r in rows]}


@app.get("/api/weekly-reports/{report_id}")
def weekly_report_detail(report_id: str):
    r = wer.get_report(_competitor_client, report_id)
    if r is None:
        raise HTTPException(status_code=404, detail="指定されたreport_idが見つかりません")
    return _weekly_report_to_ui(r, with_body=True)


@app.post("/api/weekly-reports/{report_id}/review")
def weekly_report_review(report_id: str, body: WeeklyReviewBody):
    principal, error = _review_principal()
    if principal is None:
        raise HTTPException(status_code=401, detail=f"認証済みレビュー担当者を解決できません（{error}）")
    payload = {"status": body.status, "reviewer_feedback": body.reviewerFeedback, "test_mode": body.testMode}
    if body.draftContent is not None:
        payload["draft_content"] = body.draftContent
    try:
        result = expert_api.review_weekly_report(report_id, payload, _config, principal=principal)
    except Exception as e:  # 想定外の例外は、原因が画面とログに出るようにする（500の中身が分からないため）
        import logging
        logging.getLogger(__name__).exception("週次レビューの処理で例外が発生しました")
        raise HTTPException(status_code=500, detail=f"サーバー内エラー: {type(e).__name__}: {str(e)[:300]}")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "処理に失敗しました"))
    return result


# ─── 週次ドラフト生成（旧CLI `weekly_email_report.py build` のボタン化） ─────────
# 生成は記事ごとにLLMを呼ぶため数分かかる。リクエスト内で実行せず、別プロセスで起動し、
# 進捗は標準出力を書き込んだログファイルとジョブ状態JSONで共有する（Gunicornの複数
# プロセスでも状態が食い違わないよう、メモリではなくファイルで持つ）。送信は行わない。
import json as _json  # noqa: E402
import re as _re  # noqa: E402
import subprocess as _subprocess  # noqa: E402
import threading as _threading  # noqa: E402
import uuid as _uuid  # noqa: E402

_BUILD_DIR = Path(__file__).parent / "logs" / "weekly_build"
_BUILD_STALE_SEC = 60 * 60  # 1時間以上runningのままのジョブは、プロセス消失として扱う


class WeeklyBuildBody(BaseModel):
    sinceDays: int | None = None
    forceGeoRerun: bool = False


def _build_meta_path(job_id: str) -> Path:
    return _BUILD_DIR / f"{job_id}.json"


def _read_build_job(job_id: str) -> dict | None:
    try:
        meta = _json.loads(_build_meta_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    log_path = _BUILD_DIR / f"{job_id}.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    if meta.get("state") == "running" and time.time() - meta.get("startedAt", 0) > _BUILD_STALE_SEC:
        meta["state"] = "lost"
        meta["error"] = "ジョブの状態を確認できなくなりました（プロセスが終了した可能性があります）"
    m = _re.search(r"report_id=([0-9a-fA-F-]{36})", log)
    meta["reportId"] = m.group(1) if m else None
    meta["log"] = log[-4000:]
    return meta


def _running_build_job() -> str | None:
    for p in _BUILD_DIR.glob("*.json") if _BUILD_DIR.exists() else []:
        job = _read_build_job(p.stem)
        if job and job.get("state") == "running":
            return p.stem
    return None


def _run_build_job(job_id: str, cmd: list):
    meta_path = _build_meta_path(job_id)
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    try:
        with open(_BUILD_DIR / f"{job_id}.log", "w", encoding="utf-8") as log:
            proc = _subprocess.run(cmd, cwd=str(Path(__file__).parent), env=env,
                                   stdout=log, stderr=_subprocess.STDOUT)
        meta["state"] = "done" if proc.returncode == 0 else "error"
        if proc.returncode != 0:
            meta["error"] = f"生成プロセスが異常終了しました（終了コード {proc.returncode}）"
    except Exception as e:  # noqa: BLE001
        meta["state"], meta["error"] = "error", f"{type(e).__name__}: {e}"
    meta["finishedAt"] = time.time()
    meta_path.write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")


@app.post("/api/weekly-reports/build")
def weekly_report_build(body: WeeklyBuildBody):
    principal, error = _review_principal()
    if principal is None:
        raise HTTPException(status_code=401, detail=f"認証済みレビュー担当者を解決できません（{error}）")
    running = _running_build_job()
    if running:
        raise HTTPException(status_code=409, detail=f"ドラフト生成が実行中です（job_id={running}）")
    cmd = [sys.executable, "weekly_email_report.py", "build"]
    if body.sinceDays is not None:
        if not 1 <= body.sinceDays <= 90:
            raise HTTPException(status_code=400, detail="sinceDaysは1〜90で指定してください")
        cmd += ["--since-days", str(body.sinceDays)]
    if body.forceGeoRerun:
        cmd.append("--force-geo-rerun")
    _BUILD_DIR.mkdir(parents=True, exist_ok=True)
    job_id = _uuid.uuid4().hex[:12]
    _build_meta_path(job_id).write_text(_json.dumps({
        "jobId": job_id, "state": "running", "startedAt": time.time(),
        "startedBy": principal.user_id, "sinceDays": body.sinceDays, "error": None,
    }, ensure_ascii=False), encoding="utf-8")
    _threading.Thread(target=_run_build_job, args=(job_id, cmd), daemon=True).start()
    return {"jobId": job_id}


@app.get("/api/weekly-reports/build/latest")
def weekly_report_build_latest():
    """画面を開き直しても進行中/直近のジョブを追えるよう、最新のジョブを返す"""
    files = sorted(_BUILD_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if _BUILD_DIR.exists() else []
    return {"job": _read_build_job(files[0].stem) if files else None}


@app.get("/api/weekly-reports/build/{job_id}")
def weekly_report_build_status(job_id: str):
    if not _re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(status_code=400, detail="不正なjob_idです")
    job = _read_build_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="指定されたジョブが見つかりません")
    return job


# ─── 競合の日次ダイジェスト／月次レポートのレビュー（Streamlit画面のReact移行） ─────
# 2種類は「一覧→本文プレビュー→承認して送信／却下」で同じ形のため、共通のUI用形式に
# 正規化して1組のAPIで扱う。承認・却下は週次と同じくDEV AUTHのprincipalでのみ可能。
import competitor_daily_digest as daily_digest  # noqa: E402
import monthly_competitor_report as monthly_competitor  # noqa: E402

_REVIEW_FILTERS = ("pending", "approved", "rejected", "sent")
_MONTHLY_PENDING = ("GENERATED", "UNDER_REVIEW", "REVISION_REQUIRED")
_MONTHLY_STATUS_MAP = {"APPROVED": "approved", "SENT": "sent", "CANCELLED": "rejected"}


class ReviewActionBody(BaseModel):
    action: str  # approve | reject
    freeText: str | None = None


def _digest_to_ui(d: dict, with_body: bool = False) -> dict:
    status = d.get("review_status")
    auto = bool(d.get("auto_send_eligible")) and status == "sent"
    out = {
        "id": d["digest_id"], "subject": d.get("subject") or "", "dateLabel": str(d.get("digest_date") or ""),
        "status": "pending" if status == "review_required" else status,
        "rawStatus": status, "auto": auto, "reviewable": status == "review_required",
        "meta": f"変更イベント数: {len(d.get('change_event_ids') or [])}件　"
                f"自動配信対象日: {'はい' if d.get('auto_send_eligible') else 'いいえ'}",
        "reviewerId": d.get("reviewer_id"), "sendStatus": d.get("send_status"),
    }
    if with_body:
        out["htmlBody"] = d.get("html_body") or ""
    return out


def _monthly_to_ui(r: dict, with_body: bool = False) -> dict:
    status = r.get("status")
    s = r.get("summary_json") or {}
    meta = f"対象期間: {r.get('period_start', '')}〜{r.get('period_end', '')}"
    if s:
        meta += (f"　監視対象{s.get('monitored_companies', '—')}社　更新企業{s.get('updated_companies', '—')}社"
                 f"　目標変更{s.get('target_change_count', '—')}件　取組{s.get('initiative_count', '—')}件")
    out = {
        "id": r["report_id"], "subject": r.get("subject") or "", "dateLabel": str(r.get("report_month") or ""),
        "status": "pending" if status in _MONTHLY_PENDING else _MONTHLY_STATUS_MAP.get(status, status),
        "rawStatus": status, "auto": False, "reviewable": status in _MONTHLY_PENDING, "meta": meta,
        "reviewerId": r.get("reviewed_by"), "sendStatus": r.get("send_status"),
    }
    if with_body:
        out["htmlBody"] = r.get("html_body") or ""
    return out


def _review_kind(kind: str):
    """kind -> (一覧取得関数, 1件取得関数, 正規化関数, 承認関数, 却下関数)"""
    if kind == "daily":
        def get(rid):
            rows = _competitor_client.select("competitor_daily_alert_digests", {"digest_id": f"eq.{rid}", "limit": "1"})
            return rows[0] if rows else None
        return (lambda: daily_digest.list_digests(_competitor_client), get, _digest_to_ui,
                daily_digest.approve_and_send, daily_digest.reject_digest)
    if kind == "monthly":
        def get(rid):
            rows = _competitor_client.select("monthly_reports", {"report_id": f"eq.{rid}", "limit": "1"})
            return rows[0] if rows else None
        return (lambda: monthly_competitor.list_reports(_competitor_client), get, _monthly_to_ui,
                monthly_competitor.approve_and_send, monthly_competitor.reject_report)
    raise HTTPException(status_code=404, detail="kindはdailyまたはmonthlyです")


@app.get("/api/review/{kind}")
def review_list(kind: str, filter: str = ""):
    lister, _, to_ui, _, _ = _review_kind(kind)
    items = [to_ui(r) for r in lister()]
    if filter in _REVIEW_FILTERS:
        items = [i for i in items if i["status"] == filter]
    return {"items": items}


@app.get("/api/review/{kind}/{item_id}")
def review_detail(kind: str, item_id: str):
    _, getter, to_ui, _, _ = _review_kind(kind)
    row = getter(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="指定されたIDが見つかりません")
    return to_ui(row, with_body=True)


@app.post("/api/review/{kind}/{item_id}/action")
def review_action(kind: str, item_id: str, body: ReviewActionBody):
    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="actionはapproveまたはrejectです")
    _, getter, to_ui, approve_fn, reject_fn = _review_kind(kind)
    principal, error = _review_principal()
    if principal is None:
        raise HTTPException(status_code=401, detail=f"認証済みレビュー担当者を解決できません（{error}）")
    if not identity.can_approve(principal):
        raise HTTPException(status_code=403, detail="この操作を行う権限がありません（reviewerロールが必要です）")
    row = getter(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="指定されたIDが見つかりません")
    reviewer_id = identity.audit_reviewer_id(principal)
    feedback = {"free_text": body.freeText or None}
    if body.action == "approve":
        result = approve_fn(_competitor_client, _config, item_id, reviewer_id, reviewer_feedback=feedback)
    else:
        result = reject_fn(_competitor_client, item_id, reviewer_id, reviewer_feedback=feedback)
    if isinstance(result, dict) and result.get("ok") is False:
        raise HTTPException(status_code=400, detail=str(result.get("error") or "処理に失敗しました"))
    return {"ok": True}


# ─── 競合クロール候補管理 / 戦略質問・組織判断ナレッジ（Streamlit画面のReact移行） ─────
import competitor_source_discovery as source_discovery  # noqa: E402
import strategic_question_service as sq_service  # noqa: E402

_CANDIDATE_URL_TYPES = ["SUSTAINABILITY_HOME", "TARGETS", "PROGRESS", "REPORTS", "NEWS", "DISCLOSURE", "OTHER"]


class CandidateActivateBody(BaseModel):
    urlType: str


def _require_review_principal():
    """監視対象を書き換える操作は、承認操作と同じくDEV AUTHのprincipal必須にする"""
    principal, error = _review_principal()
    if principal is None:
        raise HTTPException(status_code=401, detail=f"認証済みレビュー担当者を解決できません（{error}）")
    if not identity.can_approve(principal):
        raise HTTPException(status_code=403, detail="この操作を行う権限がありません（reviewerロールが必要です）")
    return principal


@app.get("/api/competitor-candidates")
def competitor_candidates(status: str = "unconfirmed"):
    is_active = {"unconfirmed": False, "active": True}.get(status)
    rows = source_discovery.list_candidates(_competitor_client, is_active=is_active)
    return {"urlTypes": _CANDIDATE_URL_TYPES, "candidates": [{
        "id": c["candidate_id"], "url": c["url"], "pageTitle": c.get("page_title"),
        "urlType": c.get("url_type_candidate") or "OTHER", "isActive": bool(c.get("is_active")),
        "isCrawlable": bool(c.get("is_crawlable")), "discoveredFromSourceId": c.get("discovered_from_source_id"),
        "lastCheckedAt": c.get("last_checked_at"),
    } for c in rows]}


@app.post("/api/competitor-candidates/{candidate_id}/activate")
def competitor_candidate_activate(candidate_id: str, body: CandidateActivateBody):
    _require_review_principal()
    if body.urlType not in _CANDIDATE_URL_TYPES:
        raise HTTPException(status_code=400, detail="不正な種別です")
    result = source_discovery.activate_candidate(_competitor_client, candidate_id, body.urlType)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "処理に失敗しました"))
    return {"ok": True}


@app.post("/api/competitor-candidates/{candidate_id}/deactivate")
def competitor_candidate_deactivate(candidate_id: str):
    _require_review_principal()
    result = source_discovery.deactivate_candidate(_competitor_client, candidate_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "処理に失敗しました"))
    return {"ok": True}


@app.get("/api/strategic-questions")
def strategic_questions():
    out = []
    for q in sq_service.list_questions(_competitor_client):
        agg = None
        if q.get("question_status") in ("open", "closed"):
            try:
                a = sqr.aggregate_results(_competitor_client, q["question_id"])
                agg = {"totalResponses": a["total_responses"], "deliveryCount": a["delivery_count"],
                       "responseRate": a["response_rate"]}
            except Exception:
                agg = None
        out.append({
            "id": q["question_id"], "status": q.get("question_status"), "periodStart": q.get("period_start"),
            "theme": q.get("theme"), "title": q.get("title"), "questionText": q.get("question_text"),
            "dimensionLabel": q.get("decision_dimension_label"), "dimensionKey": q.get("decision_dimension_key"),
            "insightStatus": q.get("insight_status"), "insightError": q.get("insight_error_message"),
            "options": [{"code": o["option_code"], "label": o["label"], "description": o.get("description"),
                         "isStatusQuo": bool(o.get("is_status_quo"))} for o in q.get("_options", [])],
            "analysis": q.get("analysis_json") or {},
            "candidates": {"candidates": q.get("candidates_json") or [], "rankingScore": q.get("ranking_score"),
                           "rankingReasons": q.get("ranking_reasons") or []},
            "aggregate": agg,
        })
    return {"questions": out}


@app.get("/api/decision-insights")
def decision_insights():
    rows = _competitor_client.select("sustainability_decision_insights", {"select": "*", "order": "observed_at.desc"})
    out = []
    for ins in rows:
        try:
            publishable = bool(decision_insight_service.publishable(ins, _config))
        except Exception:
            publishable = False
        out.append({
            "id": ins.get("insight_id"), "theme": ins.get("theme"),
            "dimensionLabel": ins.get("decision_dimension_label"), "observedAt": ins.get("observed_at"),
            "responseCount": ins.get("response_count"), "deliveryCount": ins.get("delivery_count"),
            "confidence": ins.get("confidence"), "guardrailLabel": ins.get("guardrail_label"),
            "observedTendency": ins.get("observed_tendency"), "distribution": ins.get("distribution_json") or {},
            "reasoningSummary": ins.get("reasoning_summary"),
            "representativeReasoning": ins.get("representative_reasoning") or [], "publishable": publishable,
        })
    return {"insights": out}


# 画面（eco-digest-spark の静的ビルド）を同じサイトから配信する。必ず全ルート登録の後に呼ぶ。
# 配信元（_shell.html）が無ければ何もしない（API専用・ローカル開発は従来どおり）。
import frontend_static  # noqa: E402
frontend_static.mount_frontend(app)


if __name__ == "__main__":
    import os
    import uvicorn
    # ローカル開発・手動起動専用（1プロセス）。本番(Azure App Service)は
    # このブロックを経由せず、Gunicorn(Startup Command)がuvicorn.workers.UvicornWorker
    # を複数プロセス起動する構成にする（本番の複数プロセス化はGunicorn側の責務。
    # 理由はREADME/引き継ぎ参照: uvicorn自身の--workers/workers=はWindows(spawn)で
    # 不安定・かつ一般的にはGunicornに任せるのが標準的なため、あえてここでは持たない）。
    host = os.environ.get("API_HOST", "127.0.0.1")
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
