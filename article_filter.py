"""
記事フィルタリング（軽量キーワード判定）

`フィルタールール_たたき台_20260720.md` に基づく、「メディア・データ提供機関」
カテゴリ限定のノイズ記事フィルタ。LLM分析(article_analyzer.py)の直前に適用し、
明らかにテーマ外の記事はLLM分析をスキップする（案A。記事本体は保存されたまま
残し、article_analysisにanalysis_status='フィルタ除外'のスタブだけ記録する）。

2026-07-30 キーワードのテーブル化（PMOフィードバック(6)対応）:
    以前はここにPython定数として直書きしていたが、PMO・DX本部が四半期レビュー
    （ソース棚卸しと同サイクル）で共同保守できるよう、Supabaseの
    `filter_keywords` テーブルへ移した（sql/2026-07-30_filter_keywords_table.sql、
    初期データはseed_filter_keywords.py）。このモジュールは判定ロジックのみを持ち、
    キーワード自体はDBから読み込む。tag_referenceを意図的に流用しない理由は
    たたき台3.1節を参照（日本語タグ名が英語記事にマッチしない・粒度が合わない）。

使い方: main()等の起動時に一度 load_keywords(client) を呼んでから pass_filter() を使う。
    load_keywords()を呼ばずにpass_filter()を呼ぶとRuntimeErrorになる
    （「キーワードが空＝何でも除外される」という気づきにくい事故を防ぐため、
    未初期化のまま素通りさせず明示的に落とす）。
"""
import re

MEDIA_CATEGORY_TAG_ROOT = "SJ-11"  # tag_reference: 主体大分類「メディア・データ提供機関」

_ASCII_WORD = re.compile(r"^[A-Za-z0-9 \-.'&]+$")

_SPECIFIC_PATTERNS = None  # load_keywords()で読み込むまではNone（未初期化を明示）
_GENERIC_PATTERNS = None


def _compile(kw: str) -> re.Pattern:
    """英数字キーワードは単語境界つき、日本語キーワードは単純部分一致にする。
    2〜4文字の全角大文字略語（ETS, WHO, GRI等）は誤爆しやすいため大小文字を区別する。"""
    if _ASCII_WORD.match(kw):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])"
        is_short_acronym = len(kw) <= 4 and kw.isupper()
        flags = 0 if is_short_acronym else re.IGNORECASE
        return re.compile(pattern, flags)
    return re.compile(re.escape(kw))


def load_keywords(client) -> None:
    """filter_keywords（status='有効'）を読み込み、モジュール内の照合用パターンを更新する。
    プロセス起動時に1回呼べばよい（記事1件ごとにDBへ問い合わせるのは避ける設計）。"""
    global _SPECIFIC_PATTERNS, _GENERIC_PATTERNS
    rows = client.select("filter_keywords", {
        "select": "keyword_text,tier", "status": "eq.有効", "order": "keyword_group,keyword_text",
    })
    specific = [r["keyword_text"] for r in rows if r["tier"] == "厳密語"]
    generic = [r["keyword_text"] for r in rows if r["tier"] == "一般語"]
    _SPECIFIC_PATTERNS = [(kw, _compile(kw)) for kw in specific]
    _GENERIC_PATTERNS = [(kw, _compile(kw)) for kw in generic]


def pass_filter(title: str, body: str) -> tuple:
    """通過判定。戻り値は (通過するか, マッチしたキーワード)。
    事前に load_keywords(client) を呼んでいない場合はRuntimeError。"""
    if _SPECIFIC_PATTERNS is None or _GENERIC_PATTERNS is None:
        raise RuntimeError("article_filter.load_keywords(client) を先に呼んでください")

    title = title or ""
    body_head = (body or "")[:1000]
    haystack_both = title + "\n" + body_head
    for kw, pat in _SPECIFIC_PATTERNS:
        if pat.search(haystack_both):
            return True, kw
    for kw, pat in _GENERIC_PATTERNS:
        if pat.search(title):
            return True, kw
    return False, ""


def fetch_media_tag_ids(client) -> set:
    """tag_reference から「メディア・データ提供機関」大分類配下のtag_idを取得する"""
    rows = client.select("tag_reference", {
        "select": "tag_id",
        "or": f"(tag_id.eq.{MEDIA_CATEGORY_TAG_ROOT},parent_tag_id.eq.{MEDIA_CATEGORY_TAG_ROOT})",
    })
    return {r["tag_id"] for r in rows}
