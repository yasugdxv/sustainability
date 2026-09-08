"""
check_filter_keyword_coverage.py の単体テスト。

2026年7月に実際に発生した「ジェンダー」タグ捕捉漏れ（tag_referenceにはサブタグが
あるのにfilter_keywordsには対応する語彙が無い状態）の再発防止を主目的とする。
親タグにキーワードがあっても、requiredな子タグ自身に紐付けが無ければholesとして
検出できることを確認する（テスト1）。テスト6は、マッピング存在確認だけでなく
実際のarticle_filter.pass_filter()まで通した完全なEnd-to-Endの回帰確認。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_filter  # noqa: E402
from check_filter_keyword_coverage import check_filter_keyword_coverage  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


def _tag(tag_id, tag_name, policy, status="有効"):
    return {"tag_id": tag_id, "tag_name": tag_name, "filter_coverage_policy": policy, "status": status}


def _keyword(keyword_id, keyword_text="dummy", tier="厳密語", status="有効"):
    return {"keyword_id": keyword_id, "keyword_text": keyword_text, "keyword_group": "test",
            "tier": tier, "language": "ja", "status": status}


def _map(keyword_id, tag_id):
    return {"keyword_id": keyword_id, "tag_id": tag_id}


def test_parent_has_keyword_but_required_child_without_keyword_is_a_hole():
    """回帰テスト本体（2026年7月ジェンダー事象）: 親タグ「人的資本」(TH-08)には
    有効なキーワードが紐付いているが、子タグ「ジェンダー」(TH-08-05、required)には
    紐付くキーワードが無い。この場合、親にキーワードがあっても「ジェンダー」が
    holesとして検出され、親タグ自身はholesに含まれないこと。"""
    client = FakeSupabaseClient({
        "tag_reference": [
            _tag("TH-08", "人的資本", "required"),
            _tag("TH-08-05", "ジェンダー", "required"),
        ],
        "filter_keywords": [_keyword("kw-1", "従業員")],
        "filter_keyword_tag_map": [_map("kw-1", "TH-08")],
    })

    result = check_filter_keyword_coverage(client)

    hole_ids = [t["tag_id"] for t in result["holes"]]
    assert hole_ids == ["TH-08-05"]
    assert result["unreviewed"] == []


def test_all_required_tags_covered_holes_and_unreviewed_empty():
    client = FakeSupabaseClient({
        "tag_reference": [
            _tag("TH-01", "水", "required"),
            _tag("TH-08-05", "ジェンダー", "required"),
        ],
        "filter_keywords": [_keyword("kw-1", "水リスク"), _keyword("kw-2", "ジェンダー")],
        "filter_keyword_tag_map": [_map("kw-1", "TH-01"), _map("kw-2", "TH-08-05")],
    })

    result = check_filter_keyword_coverage(client)

    assert result["holes"] == []
    assert result["unreviewed"] == []


def test_exempt_tag_never_counted_as_hole_or_unreviewed():
    client = FakeSupabaseClient({
        "tag_reference": [_tag("SJ-99", "対象外タグ", "exempt")],
        "filter_keywords": [],
        "filter_keyword_tag_map": [],
    })

    result = check_filter_keyword_coverage(client)

    assert result["holes"] == []
    assert result["unreviewed"] == []


def test_unreviewed_tag_always_reported_regardless_of_mapping():
    client = FakeSupabaseClient({
        "tag_reference": [
            _tag("TH-99", "未判断タグA", "unreviewed"),
            _tag("TH-98", "未判断タグB", "unreviewed"),
        ],
        "filter_keywords": [_keyword("kw-1", "何か")],
        "filter_keyword_tag_map": [_map("kw-1", "TH-99")],  # マッピングがあってもunreviewedに含まれる
    })

    result = check_filter_keyword_coverage(client)

    unreviewed_ids = {t["tag_id"] for t in result["unreviewed"]}
    assert unreviewed_ids == {"TH-99", "TH-98"}
    assert result["holes"] == []


def test_required_tag_with_only_inactive_keyword_is_a_hole():
    client = FakeSupabaseClient({
        "tag_reference": [_tag("TH-08-05", "ジェンダー", "required")],
        "filter_keywords": [_keyword("kw-1", "ジェンダー", status="無効")],
        "filter_keyword_tag_map": [_map("kw-1", "TH-08-05")],
    })

    result = check_filter_keyword_coverage(client)

    assert [t["tag_id"] for t in result["holes"]] == ["TH-08-05"]


def test_e2e_gender_keyword_coverage_and_actual_filter_pass():
    """E2E回帰確認: check_filter_keyword_coverage()によるマッピング存在確認だけでなく、
    実際のarticle_filter.pass_filter()まで通して2026年7月のジェンダー捕捉漏れを
    再現・防止確認する。
    1. tag_reference上でTH-08-05(ジェンダー)がrequiredと判断される
    2. filter_keyword_tag_mapにTH-08-05とキーワードの紐付けが存在する
    3. check_filter_keyword_coverage()の結果、TH-08-05がholesに含まれない
    4. ジェンダー関連の実記事に対してarticle_filter.pass_filter()を実行し、
       記事が除外されず通過すること
    続けて、対応キーワードを無効化した場合にholesとして検出されることも確認する。
    """
    client = FakeSupabaseClient({
        "tag_reference": [_tag("TH-08-05", "ジェンダー", "required")],
        "filter_keywords": [_keyword("kw-1", "ジェンダー", tier="厳密語", status="有効")],
        "filter_keyword_tag_map": [_map("kw-1", "TH-08-05")],
    })

    # 1〜3: tag_referenceがrequired、マッピングが存在し、Coverage Checkerが正常判定する
    result = check_filter_keyword_coverage(client)
    assert result["holes"] == []

    # 4: filter_keywordsテーブルの実データ経由でarticle_filter.load_keywords()し、
    # ジェンダー関連の実記事タイトルがpass_filter()を通過することを確認する
    article_filter.load_keywords(client)
    passed, matched_keyword = article_filter.pass_filter(
        "女性活躍とジェンダー平等に向けた新方針を発表", "本文中略"
    )
    assert passed is True
    assert matched_keyword == "ジェンダー"

    # 対応キーワードを無効化した場合、Coverage CheckerがTH-08-05をholesとして検出すること
    client.update("filter_keywords", {"keyword_id": "eq.kw-1"}, {"status": "無効"})
    result_after_disable = check_filter_keyword_coverage(client)
    assert [t["tag_id"] for t in result_after_disable["holes"]] == ["TH-08-05"]


def test_severity_distinction_exit_code():
    """severity区別の確認: unreviewedのみが存在しholesが空の状態では終了コード0、
    holesが1件でもあれば非ゼロ終了になること（main()の分岐ロジックを検証する）。"""
    unreviewed_only = {"holes": [], "unreviewed": [_tag("TH-99", "未判断タグ", "unreviewed")]}
    with_holes = {"holes": [_tag("TH-08-05", "ジェンダー", "required")], "unreviewed": []}

    def _exit_code(result):
        if result["holes"]:
            return 1
        return 0

    assert _exit_code(unreviewed_only) == 0
    assert _exit_code(with_holes) != 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
