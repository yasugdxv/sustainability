"""
サステナビリティ記事ダッシュボードの共通ロジック（フレームワーク非依存）。

api_server.py（React版バックエンド）から利用する。旧Streamlitプロトタイプ
（sustainability_dashboard_app.py）は React 版へ機能移行済みのため
_removed_20260827/ へ退避済み。
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from article_crawler import SupabaseClient, _detect_nav_menu_only
import sustainability_chat_geo_service as chat_geo
import sustainability_expert_common as common

IMPORTANCE_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}

SEARCH_INTENT_SYSTEM_PROMPT_TEMPLATE = """あなたは記事検索アシスタントです。
ユーザーの自然言語の検索クエリから、記事タイトル・本文に対する検索キーワードと、
該当しそうなテーマを抽出してください。

テーマは、次の一覧の中に実在するものだけを選んでください（一致するものが無ければ空配列）:
{theme_options}

出力はJSON1個のみ:
{{"keywords": ["...", "..."], "themes": ["..."]}}
"""

SEARCH_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["keywords", "themes"],
    "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}},
        "themes": {"type": "array", "items": {"type": "string"}},
    },
}

CHAT_SYSTEM_PROMPT_HEADER = """あなたは当社サステナビリティ専門家AIです。
以下の「当社サステナビリティの重点領域・判断ルール」「当社公式コンテキスト」「参照記事一覧」
「競合各社の目標・KPI」「競合各社の取組事例」に書かれている情報をもとに、記事の内容だけでなく
当社の方針・目標との関係、競合他社との比較についても回答してください。
これらに無い情報を推測で補完しないでください。分からない場合は「手元の情報では分かりません」と
答えてください。会社の正式見解として断定はせず、事実と推測（サス推への示唆）を区別してください。
"""

CHAT_CONTEXT_LIMIT = 30
CHAT_CONTEXT_TOP_K = 5

# Phase B: Department Intelligence AI — 他部門Intelligence（Geo等）を使った回答の統合指示。
# geo_context_block（sustainability_chat_geo_service.get_geo_chat_context_with_meta()が
# 返すコンテキスト文字列）が非空の場合のみbuild_chat_system_prompt()から追記される。
# 4見出しを毎回強制せず、質問の複雑さに応じて統合してよいとする（簡潔な質問への冗長な
# 回答を避けるため）。また回答本文で出典元を毎回明示させない（出典はUI側のProvenance
# Badgeで示すため、本文が「複数AIの寄せ集め」に見えることを避ける）。
CHAT_GEO_SYNTHESIS_INSTRUCTION = """
# 他部門Intelligenceを使った回答の構成
他部門Intelligenceの参考情報を実際に回答へ反映させる場合、(1) Sustainability評価
(2) Cross-domainの参考情報 (3) Sustainability戦略・施策への示唆 (4) 未確認・不確実な点、
を質問の複雑さに応じて回答に統合してください。込み入った分析が必要な質問では見出しで
明確に分けてよく、単純な質問では見出しを省略し自然で簡潔な1つの回答にまとめてください。

回答本文で出典元（部門名・AI名）をわざわざ明示する必要はありません（出典はUI側で
別途表示されます）。ただし次の場合は本文でも明示してください: Sustainability側の
評価と他部門の見解が食い違う場合／他部門側の確信度が低い場合／結論が他部門の
判断に強く依存している場合。
"""


# ─── クライアント ───────────────────────────────────────────────────
def get_client(config: dict) -> SupabaseClient:
    return SupabaseClient(config)


# 文字コードの誤りで文字化けし、repair_mojibake()でも復元できなかった記事向けの
# 表示用フォールバック。LLM翻訳に文字化けをそのまま推測復元させると、もっともらしいが
# 事実と異なる内容を生成してしまう恐れがあるため、正直に「表示できない」旨を示す。
GARBLED_TITLE_FALLBACK = "文字化けのため表示できません（元記事のリンクをご確認ください）"
GARBLED_BODY_FALLBACK = "この記事は文字コードの誤りにより本文を正しく表示できません。お手数ですが元記事のリンクからご確認ください。"
# JS必須サイト等で本文が描画されず、ナビゲーションメニューの列挙が本文として
# 保存されてしまった過去記事向け（article_crawler側は今後の新規保存を防止済み、
# 2026-09-14）。表示時にのみ差し替え、DB上のextracted_textは変更しない。
NAV_MENU_BODY_FALLBACK = "この記事はサイト側の技術的な事情（JavaScript必須等）により本文を正しく取得できませんでした。お手数ですが元記事のリンクからご確認ください。"


# ─── 記事取得 ───────────────────────────────────────────────────────
def fetch_dashboard_articles(config: dict, since_days: int) -> list:
    """直近since_days日分の記事を、重要度ランクを問わず取得する
    （差し戻し(analysis_status='差戻し')は除く）。タグは軸別に分類して付与する
    （common.fetch_articles_with_tagsの薄いラッパー。weekly_email_report.fetch_ranked_articles
    と共通のロジックをcommon.pyに集約した）"""
    client = get_client(config)
    articles = common.fetch_articles_with_tags(client, since_days, ranks=None)
    for a in articles:
        for field in ("title", "extracted_text", "summary_short", "importance_reason"):
            if a.get(field):
                a[field] = repair_mojibake(a[field])
        if a.get("title") and _looks_garbled(a["title"]):
            a["title"] = GARBLED_TITLE_FALLBACK
        is_nav_menu_body = False
        if a.get("extracted_text") and _looks_garbled(a["extracted_text"]):
            a["extracted_text"] = GARBLED_BODY_FALLBACK
        elif a.get("extracted_text") and _detect_nav_menu_only(a["extracted_text"]):
            a["extracted_text"] = NAV_MENU_BODY_FALLBACK
            is_nav_menu_body = True
        if is_nav_menu_body:
            # 本文がナビメニューだった場合、summary_short/importance_reasonは
            # そのナビメニューを元にLLMが生成した誤った内容のため表示しない
            a["summary_short"] = ""
            a["importance_reason"] = ""
        if a.get("summary_short") and _looks_garbled(a["summary_short"]):
            a["summary_short"] = ""
        if a.get("importance_reason") and _looks_garbled(a["importance_reason"]):
            a["importance_reason"] = ""
    return articles


def top_articles_for_carousel(articles: list, limit: int) -> list:
    """重要度が高い順（同ランク内は新しい順）に上位limit件を返す"""
    ranked = sorted(articles, key=lambda a: a.get("published_at") or "", reverse=True)
    ranked.sort(key=lambda a: IMPORTANCE_ORDER.get(a.get("importance_level"), 9))
    return ranked[:limit]


_ENGAGEMENT_CACHE_TTL_SEC = 60
_engagement_cache: dict = {"data": None, "fetched_at": 0.0}


def _all_engagement(config: dict) -> dict:
    """article_engagementテーブル全体をTTLキャッシュする。
    以前はfetch_engagement_map呼び出しのたびに対象記事ID群を150件ずつin.(...)で
    チャンク問い合わせしており、検索等でフィルタ後の記事数が数千件規模になると
    リクエストごとに数十回の往復が発生し（実測: 検索1回で約20秒）検索の体感速度を
    大きく落としていた。article_engagementは「実際にいいね/読んだが発生した記事」
    のみを持つ小さいテーブルのため、全件を1回だけ取得してプロセス内でTTLキャッシュする
    （並び替え・絞り込みの正しさには影響しない）。"""
    now = time.time()
    if _engagement_cache["data"] is None or now - _engagement_cache["fetched_at"] > _ENGAGEMENT_CACHE_TTL_SEC:
        client = get_client(config)
        try:
            rows = client.select("article_engagement", {"select": "article_id,likes_count,reads_count"})
            _engagement_cache["data"] = {r["article_id"]: r for r in rows}
        except Exception:
            _engagement_cache["data"] = _engagement_cache["data"] or {}
        _engagement_cache["fetched_at"] = now
    return _engagement_cache["data"]


def fetch_engagement_map(config: dict, article_ids: list) -> dict:
    """記事ID一覧に対する いいね・読んだ 件数を取得する（React版ダッシュボード用）。
    article_engagementテーブル未適用の環境でも落ちないよう、取得失敗時は空map。"""
    if not article_ids:
        return {}
    all_engagement = _all_engagement(config)
    return {aid: all_engagement[aid] for aid in article_ids if aid in all_engagement}


def increment_engagement(config: dict, article_id: str, likes_delta: int = 0, reads_delta: int = 0) -> dict:
    """いいね・読んだ 件数を原子的に増減させる（Postgres関数 increment_article_engagement 経由）。
    sql/2026-07-21_article_engagement_schema.sql の適用が必要。"""
    client = get_client(config)
    result = client.rpc("increment_article_engagement", {
        "p_article_id": article_id,
        "p_likes_delta": likes_delta,
        "p_reads_delta": reads_delta,
    })
    row = result[0] if isinstance(result, list) and result else {}
    _engagement_cache["data"] = None  # 次回fetch_engagement_map呼び出し時に全件再取得させる
    return {
        "likesCount": row.get("likes_count", 0),
        "readsCount": row.get("reads_count", 0),
    }


def theme_options(config: dict) -> list:
    client = get_client(config)
    rows = client.select("tag_reference", {
        "select": "tag_name,tag_code",
        "tag_axis": "eq.テーマ", "tag_level": "eq.大分類", "order": "tag_code",
    })
    return [r["tag_name"] for r in rows]


def subtheme_options(config: dict) -> list:
    """テーマ軸の小分類タグ一覧を、属する大分類テーマ名付きで返す
    （検索画面でタグを大分類だけでなく小分類まで絞り込めるようにするため）。
    TH-04-12-01（サトウキビ）のように親が小分類自体（TH-04-12）である
    3階層構造のタグもあるため、tag_level='大分類'に達するまで親をたどる"""
    client = get_client(config)
    theme_tags = client.select("tag_reference", {
        "select": "tag_id,tag_code,tag_name,tag_level,parent_tag_id",
        "tag_axis": "eq.テーマ", "order": "tag_code",
    })
    by_id = {t["tag_id"]: t for t in theme_tags}

    def _top_major_name(tag: dict) -> str:
        seen = set()
        while tag["tag_level"] != "大分類" and tag.get("parent_tag_id") in by_id \
                and tag["tag_id"] not in seen:
            seen.add(tag["tag_id"])
            tag = by_id[tag["parent_tag_id"]]
        return tag["tag_name"]

    return [
        {"id": t["tag_name"], "label": t["tag_name"], "parent": _top_major_name(t)}
        for t in theme_tags if t["tag_level"] == "小分類"
    ]


def importance_rubric(config: dict) -> list:
    """重要度スコアの内訳表示用に、7評価項目の名称・説明・スコア別の判定基準を返す
    （article_analyzer.pyが記事ごとに0〜5点で採点する基準そのもの。
    importance_criteria/importance_score_bandsはどちらも小さく更新頻度も低い
    静的参照データのため、記事データのようなキャッシュは持たせず毎回取得する）"""
    client = get_client(config)
    criteria = client.select("importance_criteria", {"select": "*", "order": "display_order"})
    bands = client.select("importance_score_bands", {"select": "*"})
    bands_by_criterion: dict = {}
    for b in bands:
        bands_by_criterion.setdefault(b["criterion_id"], {})[b["score"]] = b["definition"]
    return [
        {
            "id": c["criterion_id"],
            "label": c["name_ja"],
            "description": c["description"],
            "maxScore": c["max_score"],
            "bands": bands_by_criterion.get(c["criterion_id"], {}),
        }
        for c in criteria
    ]


# ─── 絞り込み ───────────────────────────────────────────────────────
def apply_tag_filter(articles: list, selected_themes: list) -> list:
    """選択されたタグ名（大分類・小分類どちらでもよい）のいずれかを持つ記事に絞り込む"""
    if not selected_themes:
        return articles
    selected = set(selected_themes)
    return [
        a for a in articles
        if selected & set(a["themes"]) or selected & set(a.get("theme_subtags", []))
    ]


def tokenize(text: str) -> list:
    return common.tokenize(text)


def score_article(keywords: list, article: dict) -> float:
    title = (article.get("title") or "").lower()
    haystack = title + " " + (article.get("extracted_text") or "").lower()
    score = 0.0
    for kw in keywords:
        kw = (kw or "").lower().strip()
        if not kw:
            continue
        score += title.count(kw) * 3
        score += haystack.count(kw)
    return score


def apply_nl_search(articles: list, keywords: list, themes: list) -> list:
    if not keywords and not themes:
        return articles
    scored = []
    for a in articles:
        score = score_article(keywords, a)
        if themes and (set(themes) & set(a["themes"])):
            score += 5
        scored.append((score, a))
    scored = [(s, a) for s, a in scored if s > 0]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [a for _, a in scored]


def extract_search_intent(azure_client, model: str, query_text: str, theme_options_list: list) -> dict:
    system_prompt = SEARCH_INTENT_SYSTEM_PROMPT_TEMPLATE.format(theme_options="、".join(theme_options_list))
    try:
        result = common.call_llm_structured(
            azure_client, model, system_prompt, query_text,
            SEARCH_INTENT_SCHEMA, "DashboardSearchIntent")
        return result["data"]
    except common.ExpertLLMError:
        return {"keywords": tokenize(query_text), "themes": []}


# ─── 表示ヘルパー ───────────────────────────────────────────────────
def fmt_date(value: str) -> str:
    if not value:
        return "-"
    try:
        from dateutil import parser as dateutil_parser
        return dateutil_parser.parse(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)[:10]


# ─── 翻訳（タイトル・要約・本文） ────────────────────────────────────
# クロール元は海外メディアが多く原文が英語のことがあるため、日本語UIでは
# 日本語に、英語UI（React版のEN切り替え）では英語に翻訳して表示する。
# target_lang未指定時は従来通り日本語（Streamlit版はこの既定値のまま使用）。
_JP_CHAR_PAT = re.compile(r"[぀-ヿ一-鿿]")
TRANSLATE_CHUNK_SIZE = 2000

# プロキシ越しの通信が稀に破損し、UTF-8をLatin-1として誤デコードしたような
# 文字化け（例: "米"→"ç±³"）がLLM応答としてそのまま返ってくることがある。
# 通常の英語訳文にLatin-1補助文字(ã、ç、±等)が高頻度で出現することは無いため、
# その割合で簡易検知し、検知時は翻訳失敗として原文にフォールバックする。
_MOJIBAKE_PAT = re.compile("[\u00A0-\u00FF]")


def _looks_garbled(text: str, threshold: float = 0.15) -> bool:
    if not text:
        return False
    return len(_MOJIBAKE_PAT.findall(text)) / len(text) > threshold


def repair_mojibake(text: str) -> str:
    """クロール時に一部の記事で発生した、UTF-8バイト列をLatin-1として誤デコードした
    まま保存されてしまった二重エンコード文字化けを、読み込み時に検出・復元する。
    （例: "米に関する..." が "ç±³ã«é¢ãã..." のように壊れて格納されているケース。
    is_japanese()はこの壊れた文字列を「日本語ではない」と判定してしまうため、
    英語UIでは翻訳がスキップされそのまま表示され、日本語UIではLLM翻訳が偶然
    元の日本語を復元してしまい表面化しなかった）"""
    if not text or not _looks_garbled(text, threshold=0.3):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


_LANG_NAME = {"ja": "日本語", "en": "English"}

# プロセス内メモリキャッシュ（記事ID・言語の組ごとに1回だけ翻訳すればよいため）。
# 読み取りは常にこのメモリ上の辞書経由（レイテンシ最優先）。永続化はtranslation_cache
# テーブルに書き込み側だけ反映し、起動時にinit_translation_cache()で全件読み込んで
# このプロセスメモリキャッシュを温める（2026-09-02追加。以前はプロセスメモリのみで
# api_server.py再起動のたびに翻訳済みキャッシュが全消去され、再起動直後は数千件規模の
# 記事タイトル・要約が英語のまま表示される問題があった）
# _short_cache: (namespace, target_lang, article_id) -> text  （namespace: "title"/"summary"）
_short_cache: dict = {}
_body_cache: dict = {}
_translation_db_client = None


def init_translation_cache(config: dict) -> int:
    """translation_cacheテーブルの内容を全件読み込み、プロセスメモリキャッシュ
    （_short_cache/_body_cache）を温める。api_server.py起動時に1回呼ぶ想定。
    以後の書き込み（_translate_short/translate_body）もこのテーブルへ反映する。
    戻り値は読み込んだ件数（起動ログ確認用）"""
    global _translation_db_client
    client = get_client(config)
    _translation_db_client = client
    rows = client.select("translation_cache", {"select": "namespace,target_lang,article_id,text"})
    for r in rows:
        key = (r["target_lang"], r["article_id"]) if r["namespace"] == "body" else \
            (r["namespace"], r["target_lang"], r["article_id"])
        cache = _body_cache if r["namespace"] == "body" else _short_cache
        cache[key] = r["text"]
    return len(rows)


def _persist_translation(namespace: str, target_lang: str, article_id: str, text: str) -> None:
    """翻訳結果をtranslation_cacheへ書き込む（失敗してもプロセスメモリの
    キャッシュ自体には影響しないよう、呼び出し元で例外を吸収する）"""
    if _translation_db_client is None:
        return
    _translation_db_client.insert("translation_cache", [{
        "namespace": namespace, "target_lang": target_lang, "article_id": article_id, "text": text,
    }], prefer="resolution=merge-duplicates,return=minimal")


def is_japanese(text: str, threshold: float = 0.15) -> bool:
    """既に日本語であれば無駄な翻訳呼び出しをしないための簡易判定"""
    if not text or not text.strip():
        return True
    sample = text[:200]
    jp_count = len(_JP_CHAR_PAT.findall(sample))
    return jp_count / len(sample) >= threshold


def _needs_translation(text: str, target_lang: str) -> bool:
    if not text or not text.strip():
        return False
    jp = is_japanese(text)
    if target_lang == "ja":
        return not jp
    if target_lang == "en":
        return jp
    return False


def split_text(text: str, max_len: int) -> list:
    """改行（段落）単位でmax_len以内のチャンクに分割する。
    1段落がmax_len超の場合は強制的に文字数で分割する"""
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n"):
        candidate = (current + "\n" + para) if current else para
        if len(candidate) > max_len and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
        while len(current) > max_len:
            chunks.append(current[:max_len])
            current = current[max_len:]
    if current:
        chunks.append(current)
    return chunks


def get_short_translation_nonblocking(namespace: str, article_id: str, text: str, target_lang: str) -> str:
    """_short_cacheに既にあればそれを返し、無ければLLM呼び出しをせず原文をそのまま返す。
    記事一覧のように大量記事を一度に返す場面で、未キャッシュの翻訳待ちによって
    レスポンス全体がブロックされるのを避けるために使う（キャッシュ埋め自体は
    呼び出し側がThreadPoolExecutorへfire-and-forgetで依頼する想定、_warm_translation_cache参照）。
    単一記事の詳細表示等、確実に翻訳済みの文言を返したい場面ではtranslate_title/translate_summary
    （ブロッキング）をそのまま使うこと。"""
    key = (namespace, target_lang, article_id)
    return _short_cache.get(key, text)


def _translate_short(azure_client, model: str, namespace: str, article_id: str,
                      text: str, target_lang: str) -> str:
    key = (namespace, target_lang, article_id)
    if key in _short_cache:
        return _short_cache[key]
    if not azure_client or not _needs_translation(text, target_lang):
        _short_cache[key] = text
        return text
    lang_name = _LANG_NAME.get(target_lang, "日本語")
    try:
        resp = azure_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": f"あなたは翻訳者です。次のテキストを自然な{lang_name}に"
                                               f"翻訳してください。前置きや説明を付けず、翻訳結果のみを"
                                               f"1行で出力してください。"},
                {"role": "user", "content": text},
            ],
        )
        result = (resp.choices[0].message.content or "").strip() or text
        if _looks_garbled(result):
            result = text
    except Exception:
        result = text
    _short_cache[key] = result
    try:
        _persist_translation(namespace, target_lang, article_id, result)
    except Exception:
        pass  # DB書き込み失敗はプロセスメモリのキャッシュ自体には影響させない
    return result


_plain_cache: dict = {}  # (target_lang, hash(text)) -> text


def translate_plain(azure_client, model: str, text: str, target_lang: str = "ja") -> str:
    """記事に紐づかない任意テキスト（チャット履歴など）の翻訳用。
    キャッシュキーはテキスト内容のハッシュ（プロセス内のみ有効で十分なため）。"""
    if not text or not text.strip():
        return text
    key = (target_lang, hash(text))
    if key in _plain_cache:
        return _plain_cache[key]
    if not azure_client or not _needs_translation(text, target_lang):
        _plain_cache[key] = text
        return text
    lang_name = _LANG_NAME.get(target_lang, "日本語")
    try:
        resp = azure_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": f"あなたは翻訳者です。次のテキストを自然な{lang_name}に"
                                               f"翻訳してください。原文の改行構成はできるだけ保ってください。"
                                               f"前置きや説明を付けず、翻訳結果のみを出力してください。"},
                {"role": "user", "content": text},
            ],
            max_completion_tokens=2000,
        )
        result = (resp.choices[0].message.content or "").strip() or text
        if _looks_garbled(result):
            result = text
    except Exception:
        result = text
    _plain_cache[key] = result
    return result


def translate_title(azure_client, model: str, article_id: str, title: str, target_lang: str = "ja") -> str:
    return _translate_short(azure_client, model, "title", article_id, title, target_lang)


def translate_summary(azure_client, model: str, article_id: str, summary: str, target_lang: str = "ja") -> str:
    return _translate_short(azure_client, model, "summary", article_id, summary, target_lang)


def translate_body(azure_client, model: str, article_id: str, text: str, target_lang: str = "ja") -> str:
    key = (target_lang, article_id)
    if key in _body_cache:
        return _body_cache[key]
    if not azure_client or not _needs_translation(text, target_lang):
        _body_cache[key] = text
        return text
    lang_name = _LANG_NAME.get(target_lang, "日本語")
    chunks = split_text(text, TRANSLATE_CHUNK_SIZE)

    def _translate_chunk(chunk: str) -> str:
        try:
            resp = azure_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": f"あなたは翻訳者です。以下の記事本文を自然な{lang_name}に"
                                                   f"翻訳してください。原文の段落構成はできるだけ保ってください。"
                                                   f"前置きや説明を付けず、翻訳結果のみを出力してください。"},
                    {"role": "user", "content": chunk},
                ],
                max_completion_tokens=4000,
            )
            piece = (resp.choices[0].message.content or "").strip() or chunk
            return chunk if _looks_garbled(piece) else piece
        except Exception:
            return chunk

    # チャンクを逐次翻訳すると長い記事ほど所要時間が線形に増え、
    # 記事詳細表示が数十秒〜1分以上かかる原因になっていたため並列実行にする
    # （2026-09-09、川崎さんからの表示速度指摘対応）。
    with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as pool:
        translated = list(pool.map(_translate_chunk, chunks))
    result = "\n\n".join(translated)
    _body_cache[key] = result
    try:
        _persist_translation("body", target_lang, article_id, result)
    except Exception:
        pass  # DB書き込み失敗はプロセスメモリのキャッシュ自体には影響させない
    return result


# ─── サステナAIチャット ───────────────────────────────────────────
def build_chat_system_prompt(
    context_articles: list, expert_base: dict, retrieved_docs: list, competitor_block: str = "",
    geo_context_block: str = "", decision_signal_block: str = "",
) -> str:
    """絞り込み結果が多い場合、単純な先頭N件ではなく重要度優先で上位を渡す
    （新しい順のままだとS/Aランクの重要記事が新着の低重要度記事に押し出されてしまうため）。

    geo_context_block: Phase B、sustainability_chat_geo_service.get_geo_chat_context_with_meta()が
    返す他部門（Geopolitics）コンテキスト（不要な場合は空文字列）。既に「参考データとして扱う」旨を
    含めたテキストとして渡ってくるため、ここではそのまま末尾に追記するだけでよい。

    decision_signal_block: Weekly Strategic Question機能、
    decision_insight_service.build_decision_insight_chat_block()が返す「組織内の判断傾向」
    （不要な場合は空文字列）。公式方針・外部情報とは明確に区別されたセクションとして
    末尾に追記する。既にguardrail文言を含めたテキストとして渡ってくる。"""
    top = top_articles_for_carousel(context_articles, CHAT_CONTEXT_LIMIT)
    lines = []
    for a in top:
        lines.append(
            f"- [{a.get('importance_level', '-')}] {a['title']}"
            f"（{'/'.join(a['themes'])}、{fmt_date(a.get('published_at'))}、{a.get('publisher', '')}）\n"
            f"  要約: {a.get('summary_short') or a.get('importance_reason') or ''}\n"
            f"  URL: {a.get('url', '')}"
        )
    articles_block = "\n".join(lines) if lines else "（該当する記事はありません）"
    note = ""
    if len(context_articles) > CHAT_CONTEXT_LIMIT:
        note = (f"\n※ 絞り込み結果{len(context_articles)}件のうち、重要度の高い{CHAT_CONTEXT_LIMIT}件のみを"
                f"以下に示します。それ以外の記事について聞かれた場合は、範囲外である旨を伝えてください。\n")

    company_context = json.dumps(expert_base.get("company_context", {}), ensure_ascii=False)
    knowledge_block = common.format_retrieved_context(retrieved_docs)

    return (
        CHAT_SYSTEM_PROMPT_HEADER
        + f"\n# 当社サステナビリティの重点領域・判断ルール\n{company_context}\n"
        + f"\n# 当社公式コンテキスト（質問に関連して検索されたもの）\n{knowledge_block}\n"
        + note
        + f"\n# 参照記事一覧（画面左側の検索・タグ絞り込みの結果）\n{articles_block}\n"
        + (f"\n{competitor_block}\n" if competitor_block else "")
        + (geo_context_block if geo_context_block else "")
        + (f"\n{CHAT_GEO_SYNTHESIS_INSTRUCTION}\n" if geo_context_block else "")
        + (f"\n{decision_signal_block}\n" if decision_signal_block else "")
    )


# ─── Phase B: Search用Cross-domain Intelligence整形 ────────────────────────
def _to_cross_domain_item(source) -> dict:
    """cross_domain_intelligence_service.CrossDomainIntelligenceSource 1件を、Search専用の
    CrossDomainIntelligenceItem形（本文＋Provenance）へ変換する純粋関数。
    Chatと違いSearchは人間が直接Intelligence本体を読むため、Provenanceだけでなく
    title/assessment/whyRelevant/asOfも返す。"""
    return {
        "id": source.source_item_id or source.external_call_id,
        "title": source.title,
        "assessment": source.assessment,
        "whyRelevant": source.why_relevant,
        "asOf": source.as_of,
        "provenance": chat_geo._to_provenance(source),
    }
