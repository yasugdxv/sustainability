"""記事一覧のページ分割（articles_paging.py）のテスト。
続きの取りこぼし・重複が無いこと、同じ値が並んでも順序が一意なこと、新着が増えても表示がずれないこと、
不正なカーソルで落ちないことを確認する。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import articles_paging as ap  # noqa: E402

SCORE = {"S": 96, "A": 84, "B": 66, "C": 46, "D": 26}
imp = lambda level: SCORE.get(level, 50)  # noqa: E731


def _art(i, level="C", pub=None, tags=None):
    return {"article_id": f"a{i:03d}", "importance_level": level,
            "published_at": pub or f"2026-11-{(i % 28) + 1:02d}T00:00:00+00:00",
            "cross_tags": tags or []}


def _collect_all(articles, sort, limit, engagement=None):
    """カーソルをたどって全ページ分のIDを集める"""
    engagement = engagement or {}
    ids, cursor = [], ""
    for _ in range(1000):
        page = ap.paginate(articles, sort, limit, cursor, imp, engagement)
        ids += [a["article_id"] for a in page["items"]]
        if not page["hasMore"]:
            assert page["nextCursor"] is None
            return ids
        cursor = page["nextCursor"]
    raise AssertionError("終わらない")


def test_all_pages_cover_every_article_exactly_once_for_every_sort():
    levels = ["S", "A", "B", "C", "D"]
    arts = [_art(i, levels[i % 5]) for i in range(57)]
    eng = {a["article_id"]: {"likes_count": i % 4, "reads_count": i % 3} for i, a in enumerate(arts)}
    for sort in ap.SORTS:
        ids = _collect_all(arts, sort, 10, eng)
        assert len(ids) == 57 and len(set(ids)) == 57, sort  # 取りこぼし・重複なし


def test_order_is_stable_and_descending_for_importance():
    arts = [_art(1, "C"), _art(2, "S"), _art(3, "B"), _art(4, "S", pub="2026-12-01T00:00:00+00:00")]
    page = ap.paginate(arts, "importance", 10, "", imp, {})
    # 重要度が高い順。同じSなら公開日が新しい方が先
    assert [a["article_id"] for a in page["items"]] == ["a004", "a002", "a003", "a001"]


def test_latest_is_newest_first_and_ties_are_unique():
    same = "2026-11-05T00:00:00+00:00"
    arts = [_art(i, pub=same) for i in range(5)]  # 公開日が全部同じでも順序が一意（ID降順）
    assert _collect_all(arts, "latest", 2) == ["a004", "a003", "a002", "a001", "a000"]


def test_new_article_added_between_pages_does_not_shift_or_duplicate():
    arts = [_art(i, pub=f"2026-11-{i + 1:02d}T00:00:00+00:00") for i in range(10)]
    first = ap.paginate(arts, "latest", 3, "", imp, {})
    # ページの合間に、いちばん新しい記事が増えた
    arts2 = arts + [_art(99, pub="2026-11-30T00:00:00+00:00")]
    second = ap.paginate(arts2, "latest", 3, first["nextCursor"], imp, {})
    seen = [a["article_id"] for a in first["items"]] + [a["article_id"] for a in second["items"]]
    assert len(seen) == len(set(seen))  # 重複しない（ページ番号方式だとずれて重複する）
    assert "a099" not in seen


def test_invalid_cursor_starts_from_top_without_error():
    arts = [_art(i) for i in range(3)]
    for bad in ("not-base64!!", "e30=", "", None):
        page = ap.paginate(arts, "latest", 10, bad or "", imp, {})
        assert len(page["items"]) == 3


def test_limit_is_clamped():
    arts = [_art(i) for i in range(150)]
    assert len(ap.paginate(arts, "latest", 100000, "", imp, {})["items"]) == ap.MAX_LIMIT
    assert len(ap.paginate(arts, "latest", 0, "", imp, {})["items"]) == 1


def test_unknown_sort_falls_back_to_latest():
    assert ap.normalize_sort("bogus") == "latest"


def test_likes_sort_uses_engagement():
    arts = [_art(1), _art(2), _art(3)]
    eng = {"a002": {"likes_count": 5}, "a003": {"likes_count": 1}}
    page = ap.paginate(arts, "likes", 10, "", imp, eng)
    assert [a["article_id"] for a in page["items"]] == ["a002", "a003", "a001"]


def test_stats_cover_all_articles_not_just_a_page():
    arts = [_art(1, "S", tags=["情報開示"]), _art(2, "A"), _art(3, "C", tags=["グリーンウォッシュ"]),
            _art(4, "D", tags=["地政学・マクロ規制環境", "情報開示"])]
    s = ap.build_stats(arts, imp)
    assert s["count"] == 4
    assert s["importantCount"] == 2 and s["trendingCount"] == 2  # S/A
    assert s["regulatoryCount"] == 2  # 1と4（複数該当でも1件と数える）
    assert s["avgImportance"] == round((96 + 84 + 46 + 26) / 4)


def test_stats_empty():
    assert ap.build_stats([], imp)["avgImportance"] == 0
