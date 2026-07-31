"""
サスティナビリティ記事ダッシュボード（Streamlitプロトタイプ）

直近記事のカルーセル表示・タグ絞り込み・自然言語検索・記事詳細表示に加え、
絞り込み後の記事だけを参照して会話できる「サスティナAI」チャットを提供する。
既存のサスティナビリティ専門家MVPのレビュー画面(sustainability_expert_dashboard.py)
とは別系統の、閲覧・検索向け画面。

データ取得・翻訳・検索意図抽出・チャット組み立てのロジックは sustainability_dashboard_core.py
に共通化されており、React版フロントエンド用の api_server.py とも共有している。

実行: streamlit run projects/world_monitor_dashboard/sustainability_dashboard_app.py
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import load_config  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
from sustainability_knowledge_store import get_knowledge_store  # noqa: E402
import sustainability_dashboard_core as core  # noqa: E402

DEFAULT_LOOKBACK_DAYS = 30
CAROUSEL_SIZE = 20
CAROUSEL_PAGE_SIZE = 4
IMPORTANCE_BADGE = {"S": "🔴 S", "A": "🟠 A", "B": "🟡 B", "C": "🟢 C", "D": "⚪ D"}


# ─── 設定・クライアント ─────────────────────────────────────────────
@st.cache_resource
def _load_config() -> dict:
    return load_config()


@st.cache_resource
def _load_expert_base() -> dict:
    return common.load_expert_base()


@st.cache_resource
def _get_knowledge_store():
    return get_knowledge_store(_load_config())


# ─── 記事取得 ───────────────────────────────────────────────────────
# Streamlitはウィジェット操作のたびにスクリプト全体を再実行するため、キャッシュ無しだと
# チェックボックス1つクリックするだけでもSupabaseへの一連の問い合わせが毎回走ってしまう。
# 記事データは数分単位でしか変わらないため、cache_dataで再取得を抑える。
@st.cache_data(ttl=300, show_spinner="記事を読み込み中...")
def fetch_dashboard_articles(since_days: int) -> list:
    return core.fetch_dashboard_articles(_load_config(), since_days)


@st.cache_data(ttl=3600)
def _theme_options() -> list:
    return core.theme_options(_load_config())


@st.cache_data(ttl=None, show_spinner=False)
def _translate_title(_azure_client, model: str, article_id: str, title: str) -> str:
    return core.translate_title(_azure_client, model, article_id, title)


@st.cache_data(ttl=None, show_spinner=False)
def _translate_body(_azure_client, model: str, article_id: str, text: str) -> str:
    return core.translate_body(_azure_client, model, article_id, text)


# ─── カルーセル ─────────────────────────────────────────────────────
def _render_carousel(top_articles: list, azure_client, model: str):
    if not top_articles:
        st.caption("該当する記事がありません")
        return

    st.session_state.setdefault("carousel_page", 0)
    max_page = max(0, (len(top_articles) - 1) // CAROUSEL_PAGE_SIZE)
    st.session_state.carousel_page = min(st.session_state.carousel_page, max_page)

    nav1, nav2, nav3 = st.columns([1, 8, 1])
    start = st.session_state.carousel_page * CAROUSEL_PAGE_SIZE
    page_items = top_articles[start:start + CAROUSEL_PAGE_SIZE]
    with nav2:
        st.caption(f"{start + 1}-{start + len(page_items)} / {len(top_articles)}件")
    with nav1:
        if st.button("◀", disabled=st.session_state.carousel_page == 0, key="carousel_prev"):
            st.session_state.carousel_page -= 1
            st.rerun()
    with nav3:
        if st.button("▶", disabled=st.session_state.carousel_page >= max_page, key="carousel_next"):
            st.session_state.carousel_page += 1
            st.rerun()

    cols = st.columns(len(page_items))
    for col, a in zip(cols, page_items):
        with col:
            with st.container(border=True):
                st.markdown(f"**{IMPORTANCE_BADGE.get(a.get('importance_level'), '')}**")
                title_ja = _translate_title(azure_client, model, a["article_id"], a["title"])
                st.markdown(f"**{title_ja}**")
                st.caption(f"{a.get('publisher', '')}｜{core.fmt_date(a.get('published_at'))}")
                st.caption(core.tag_caption(a))
                st.write((a.get("summary_short") or a.get("importance_reason") or "")[:60])
                if st.button("詳細を見る", key=f"carousel_detail_{a['article_id']}", use_container_width=True):
                    st.session_state.selected_article_id = a["article_id"]
                    st.session_state.view_mode = "list"
                    st.rerun()


# ─── 記事リスト・詳細 ───────────────────────────────────────────────
def _render_list(filtered: list, azure_client, model: str):
    if not filtered:
        st.info("該当する記事が見つかりませんでした。")
        return
    for a in filtered:
        with st.container(border=True):
            col1, col2 = st.columns([6, 1])
            with col1:
                title_ja = _translate_title(azure_client, model, a["article_id"], a["title"])
                st.markdown(f"**{title_ja}**")
                st.caption(f"{a.get('publisher', '')}｜{core.fmt_date(a.get('published_at'))}｜"
                           f"{IMPORTANCE_BADGE.get(a.get('importance_level'), '')}")
                st.write(a.get("summary_short") or a.get("importance_reason") or "")
                st.caption(core.tag_caption(a))
            with col2:
                if st.button("詳細", key=f"list_detail_{a['article_id']}", use_container_width=True):
                    st.session_state.selected_article_id = a["article_id"]
                    st.rerun()


def _render_detail(article: dict | None, azure_client, model: str):
    if st.button("← 一覧に戻る"):
        st.session_state.selected_article_id = None
        st.rerun()

    if not article:
        st.warning("記事が見つかりませんでした（絞り込み条件が変わった可能性があります）。")
        return

    title_ja = _translate_title(azure_client, model, article["article_id"], article["title"])
    st.subheader(title_ja)
    st.caption(f"{article.get('publisher', '')}｜{core.fmt_date(article.get('published_at'))}｜"
               f"重要度: {article.get('importance_level', '-')}")
    if article.get("url"):
        st.markdown(f"[元記事を開く]({article['url']})")
    st.caption(core.tag_caption(article))
    st.markdown("**要約**")
    st.write(article.get("summary_short") or article.get("importance_reason") or "（要約なし）")
    st.markdown("**本文（日本語訳）**")
    body = article.get("extracted_text") or ""
    if body:
        with st.spinner("本文を日本語に翻訳中..."):
            body_ja = _translate_body(azure_client, model, article["article_id"], body)
        st.write(body_ja)
    else:
        st.write("（本文取得なし）")


# ─── サスティナAIチャット ───────────────────────────────────────────
def _render_chat(azure_client, model: str, context_articles: list, config: dict):
    st.subheader("🌿 サスティナAI")
    if st.button("← 記事一覧に戻る"):
        st.session_state.view_mode = "list"
        st.rerun()

    if not common.is_enabled(config):
        st.warning("⚠️ SUSTAINABILITY_EXPERT_ENABLED が無効です。config.json の "
                   "sustainability_expert.enabled を true にするか、環境変数で有効化してください。")
        return

    if not azure_client:
        st.warning("Azure OpenAI / OpenAI のAPIキーが設定されていないため、チャットは利用できません。")
        return

    expert_base = _load_expert_base()
    knowledge_store = _get_knowledge_store()

    st.caption(f"現在の絞り込み結果 {len(context_articles)}件と、当社公式コンテキスト（質問に応じて検索）"
               f"を参照して回答します（左側のタグ・検索を変えると記事の参照範囲も変わります）。")
    if st.button("🔄 会話をリセット"):
        st.session_state.chat_messages = []
        st.rerun()

    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    user_input = st.chat_input("記事や当社サスティナビリティの取り組みについて質問する...")
    if user_input:
        st.session_state.chat_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.write(user_input)

        try:
            retrieved_docs = knowledge_store.search(user_input, top_k=core.CHAT_CONTEXT_TOP_K)
        except Exception:
            retrieved_docs = []

        system_prompt = core.build_chat_system_prompt(context_articles, expert_base, retrieved_docs)
        messages = [{"role": "system", "content": system_prompt}]
        messages += [{"role": m["role"], "content": m["content"]} for m in st.session_state.chat_messages]

        with st.chat_message("assistant"):
            try:
                resp = azure_client.chat.completions.create(model=model, messages=messages, temperature=0.3)
                reply = resp.choices[0].message.content
            except Exception as e:
                reply = f"エラーが発生しました: {type(e).__name__}: {e}"
            st.write(reply)
        st.session_state.chat_messages.append({"role": "assistant", "content": reply})


# ─── メイン ─────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="サスティナビリティ記事ダッシュボード", page_icon="🌏", layout="wide")
    config = _load_config()
    azure_client, model = make_openai_client(config)

    st.session_state.setdefault("view_mode", "list")
    st.session_state.setdefault("selected_article_id", None)
    st.session_state.setdefault("search_keywords", [])
    st.session_state.setdefault("search_themes", [])
    st.session_state.setdefault("search_query_text", "")
    st.session_state.setdefault("chat_messages", [])

    st.title("🌏 サスティナビリティ記事ダッシュボード")

    try:
        articles = fetch_dashboard_articles(DEFAULT_LOOKBACK_DAYS)
    except Exception as e:
        st.error(f"記事の取得に失敗しました: {e}")
        return

    if not articles:
        st.info(f"直近{DEFAULT_LOOKBACK_DAYS}日分の記事がありません。")
        return

    st.subheader("📌 今週のトピック")
    _render_carousel(core.top_articles_for_carousel(articles, CAROUSEL_SIZE), azure_client, model)
    st.divider()

    theme_options = _theme_options()

    with st.sidebar:
        st.subheader("🔍 検索・絞り込み")
        with st.form("search_form"):
            query_text = st.text_input("自然言語で検索", value=st.session_state.search_query_text)
            submitted = st.form_submit_button("検索")
        if submitted:
            st.session_state.search_query_text = query_text
            if query_text.strip():
                intent = core.extract_search_intent(azure_client, model, query_text, theme_options) \
                    if azure_client else {"keywords": core.tokenize(query_text), "themes": []}
                st.session_state.search_keywords = intent.get("keywords", [])
                st.session_state.search_themes = intent.get("themes", [])
            else:
                st.session_state.search_keywords = []
                st.session_state.search_themes = []

        st.markdown("**テーマで絞り込み**")
        selected_themes = [t for t in theme_options if st.checkbox(t, key=f"theme_{t}")]

        st.divider()
        if st.button("🌿 サスティナAI", use_container_width=True):
            st.session_state.view_mode = "chat" if st.session_state.view_mode != "chat" else "list"
            st.rerun()

    filtered = core.apply_tag_filter(articles, selected_themes)
    filtered = core.apply_nl_search(filtered, st.session_state.search_keywords, st.session_state.search_themes)

    st.caption(f"絞り込み結果: {len(filtered)}件 / 全{len(articles)}件（直近{DEFAULT_LOOKBACK_DAYS}日）")

    if st.session_state.view_mode == "chat":
        _render_chat(azure_client, model, filtered, config)
    elif st.session_state.selected_article_id:
        article = next((a for a in articles if a["article_id"] == st.session_state.selected_article_id), None)
        _render_detail(article, azure_client, model)
    else:
        _render_list(filtered, azure_client, model)


main()
