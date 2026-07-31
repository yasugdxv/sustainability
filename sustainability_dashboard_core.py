"""
サスティナビリティ記事ダッシュボードの共通ロジック（フレームワーク非依存）。

sustainability_dashboard_app.py（Streamlit版）と api_server.py（React版バックエンド）の
両方から利用する。Streamlit固有のキャッシュ・UI呼び出しはここに含めない。
"""
import json
import re

from article_crawler import SupabaseClient
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

CHAT_SYSTEM_PROMPT_HEADER = """あなたは当社サスティナビリティ専門家AIです。
以下の「当社サスティナビリティの重点領域・判断ルール」「当社公式コンテキスト」「参照記事一覧」
「競合各社の目標・KPI」「競合各社の取組事例」に書かれている情報をもとに、記事の内容だけでなく
当社の方針・目標との関係、競合他社との比較についても回答してください。
これらに無い情報を推測で補完しないでください。分からない場合は「手元の情報では分かりません」と
答えてください。会社の正式見解として断定はせず、事実と推測（サス推への示唆）を区別してください。
"""

CHAT_CONTEXT_LIMIT = 30
CHAT_CONTEXT_TOP_K = 5


# ─── クライアント ───────────────────────────────────────────────────
def get_client(config: dict) -> SupabaseClient:
    return SupabaseClient(config)


# 文字コードの誤りで文字化けし、repair_mojibake()でも復元できなかった記事向けの
# 表示用フォールバック。LLM翻訳に文字化けをそのまま推測復元させると、もっともらしいが
# 事実と異なる内容を生成してしまう恐れがあるため、正直に「表示できない」旨を示す。
GARBLED_TITLE_FALLBACK = "文字化けのため表示できません（元記事のリンクをご確認ください）"
GARBLED_BODY_FALLBACK = "この記事は文字コードの誤りにより本文を正しく表示できません。お手数ですが元記事のリンクからご確認ください。"


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
        if a.get("extracted_text") and _looks_garbled(a["extracted_text"]):
            a["extracted_text"] = GARBLED_BODY_FALLBACK
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


def fetch_engagement_map(config: dict, article_ids: list) -> dict:
    """記事ID一覧に対する いいね・読んだ 件数を取得する（React版ダッシュボード用）。
    article_engagementテーブル未適用の環境でも落ちないよう、取得失敗時は空map。"""
    if not article_ids:
        return {}
    client = get_client(config)
    try:
        rows = client.select("article_engagement", {
            "select": "article_id,likes_count,reads_count",
            "article_id": f"in.({','.join(article_ids)})",
        })
    except Exception:
        return {}
    return {r["article_id"]: r for r in rows}


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


# ─── 絞り込み ───────────────────────────────────────────────────────
def apply_tag_filter(articles: list, selected_themes: list) -> list:
    if not selected_themes:
        return articles
    selected = set(selected_themes)
    return [a for a in articles if selected & set(a["themes"])]


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


def tag_caption(a: dict) -> str:
    parts = a["themes"][:1] + a.get("cross_tags", [])[:2] + a.get("subject_tags", [])[:1]
    line = "｜".join(parts)
    if a.get("materiality_codes"):
        line += ("｜" if line else "") + "/".join(a["materiality_codes"])
    return line


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

# プロセス内メモリキャッシュ（記事ID・言語の組ごとに1回だけ翻訳すればよいため）
# _short_cache: (namespace, target_lang, article_id) -> text  （namespace: "title"/"summary"）
_short_cache: dict = {}
_body_cache: dict = {}


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
            temperature=0.2,
        )
        result = (resp.choices[0].message.content or "").strip() or text
        if _looks_garbled(result):
            result = text
    except Exception:
        result = text
    _short_cache[key] = result
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
            temperature=0.2,
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
    translated = []
    for chunk in chunks:
        try:
            resp = azure_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": f"あなたは翻訳者です。以下の記事本文を自然な{lang_name}に"
                                                   f"翻訳してください。原文の段落構成はできるだけ保ってください。"
                                                   f"前置きや説明を付けず、翻訳結果のみを出力してください。"},
                    {"role": "user", "content": chunk},
                ],
                temperature=0.2,
                max_completion_tokens=4000,
            )
            piece = (resp.choices[0].message.content or "").strip() or chunk
            translated.append(chunk if _looks_garbled(piece) else piece)
        except Exception:
            translated.append(chunk)
    result = "\n\n".join(translated)
    _body_cache[key] = result
    return result


# ─── サスティナAIチャット ───────────────────────────────────────────
def build_chat_system_prompt(
    context_articles: list, expert_base: dict, retrieved_docs: list, competitor_block: str = "",
) -> str:
    """絞り込み結果が多い場合、単純な先頭N件ではなく重要度優先で上位を渡す
    （新しい順のままだとS/Aランクの重要記事が新着の低重要度記事に押し出されてしまうため）"""
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
        + f"\n# 当社サスティナビリティの重点領域・判断ルール\n{company_context}\n"
        + f"\n# 当社公式コンテキスト（質問に関連して検索されたもの）\n{knowledge_block}\n"
        + note
        + f"\n# 参照記事一覧（画面左側の検索・タグ絞り込みの結果）\n{articles_block}\n"
        + (f"\n{competitor_block}\n" if competitor_block else "")
    )
