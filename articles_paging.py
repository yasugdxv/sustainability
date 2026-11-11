"""記事一覧のページ分割（無限スクロール用）。

/api/articles が全件を一度に返すと、変換・翻訳・いいね取得が全件分走って重く、時間切れになりやすい。
そこで「並べ替え → 続きの位置(カーソル)より後ろだけ切り出す → その分だけ画面用に変換」にする。

  - カーソル方式（キーセット）: 「最後に返した記事の並べ替えキー」を覚えておき、それより後ろを返す。
    ページ番号方式と違い、新しい記事が増えても表示がずれず、深いページでも速度が変わらない。
  - 並べ替えキーは (主キー, 公開日時, 記事ID) の降順。同じ値が並んでも順序が必ず一意に決まる。
  - ここは純粋なロジックだけ（DB・LLMには触らない）。キャッシュ済みの記事一覧を渡して使う。
"""
from __future__ import annotations

import base64
import json

SORTS = ("importance", "latest", "likes", "reads", "trending")
DEFAULT_SORT = "latest"
MAX_LIMIT = 100
# 規制関連のタグ（サイドバーの「規制関連」件数に使う。日本語タグ名）
REGULATORY_TAGS = ("情報開示", "地政学・マクロ規制環境", "エンフォースメント・訴訟")


def normalize_sort(sort: str) -> str:
    return sort if sort in SORTS else DEFAULT_SORT


def clamp_limit(limit: int) -> int:
    """1回に返す件数を 1〜MAX_LIMIT に収める"""
    return max(1, min(int(limit), MAX_LIMIT))


def sort_key(article: dict, sort: str, importance_fn, engagement: dict) -> tuple:
    """並べ替えキー（大きいほど先に出る）。(主キー, 公開日時, 記事ID)"""
    pub = article.get("published_at") or ""
    aid = article["article_id"]
    eng = engagement.get(aid, {})
    if sort == "importance":
        primary = importance_fn(article.get("importance_level"))
    elif sort == "likes":
        primary = eng.get("likes_count", 0)
    elif sort == "reads":
        primary = eng.get("reads_count", 0)
    elif sort == "trending":
        primary = 1 if article.get("importance_level") in ("S", "A") else 0
    else:  # latest
        primary = 0
    return (primary, pub, aid)


def sort_articles(articles: list, sort: str, importance_fn, engagement: dict) -> list:
    keyed = [(sort_key(a, sort, importance_fn, engagement), a) for a in articles]
    keyed.sort(key=lambda x: x[0], reverse=True)
    return [a for _, a in keyed]


def encode_cursor(key: tuple) -> str:
    raw = json.dumps(list(key), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str):
    """不正なカーソルは None（先頭から返す）。外部入力なので例外にはしない"""
    if not cursor:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        if isinstance(data, list) and len(data) == 3:
            return (data[0], data[1], data[2])
    except Exception:
        pass
    return None


def paginate(articles: list, sort: str, limit: int, cursor: str, importance_fn, engagement: dict) -> dict:
    """並べ替えて、カーソルより後ろの先頭limit件を返す。
    戻り値: {"items": [...], "hasMore": bool, "nextCursor": str | None}"""
    sort = normalize_sort(sort)
    limit = clamp_limit(limit)
    ordered = sort_articles(articles, sort, importance_fn, engagement)
    after = decode_cursor(cursor)
    if after is not None:
        # ordered は降順なので、カーソルのキーより小さいものだけが「続き」
        ordered = [a for a in ordered if sort_key(a, sort, importance_fn, engagement) < after]
    items = ordered[:limit]
    has_more = len(ordered) > limit
    next_cursor = encode_cursor(sort_key(items[-1], sort, importance_fn, engagement)) if (items and has_more) else None
    return {"items": items, "hasMore": has_more, "nextCursor": next_cursor}


def build_stats(articles: list, importance_fn) -> dict:
    """絞り込み後の全件に対する集計（サイドバー・カテゴリ画面の件数表示用）。ページに依存しない"""
    n = len(articles)
    scores = [importance_fn(a.get("importance_level")) for a in articles]
    return {
        "count": n,
        "importantCount": sum(1 for a in articles if a.get("importance_level") in ("S", "A")),
        "trendingCount": sum(1 for a in articles if a.get("importance_level") in ("S", "A")),
        "regulatoryCount": sum(1 for a in articles if set(a.get("cross_tags", [])) & set(REGULATORY_TAGS)),
        "avgImportance": round(sum(scores) / n) if n else 0,
    }
