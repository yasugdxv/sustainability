"""
軽量キーワードフィルタ（article_filter.py）の除外件数ログ（filter_exclusion_log）の
単体テスト。PMOレビュー依頼対応: 除外件数を出典区分別・件数のみで記録し、記事本体
（タイトル・本文等）は一切含めないことを確認する。

2026-08-27追記: フィルタ語彙(filter_keywords)がtag_referenceから機械的に導出されて
おらず独立管理であることが判明（過去に「ジェンダー」等の捕捉漏れが実際に発生）。
tag_reference由来の導出が完了するまでは、フィルタ除外記事を完全非保存（物理削除）に
する運用を本番化しない方針となり、delete_filtered_article()（物理削除）から
save_keyword_filtered_stub()（記事本体は残しスタブのみ保存）へ切り替えた。
その回帰確認テストを本ファイルへ追加する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import article_analyzer  # noqa: E402
import article_filter  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


class _BrokenInsertClient(FakeSupabaseClient):
    """filter_exclusion_logテーブル未作成（マイグレーション未適用）を模したフェイク。
    実際に2026-08-27の実行で本番DBに未適用のままarticle_analyzer.pyを実行し、
    save_filter_exclusion_log()の例外がLLM分析パイプライン全体を落とす事故が発生した
    （HTTPError 404）。その回帰防止用。"""

    def insert(self, table, rows, prefer="return=representation"):
        if table == "filter_exclusion_log":
            raise RuntimeError("404 Client Error: Not Found (filter_exclusion_logテーブル未作成を模擬)")
        return super().insert(table, rows, prefer)


def _client_with_keywords(keywords):
    """article_filter.load_keywords()相当のパターンをテスト用に直接セットする
    （FakeSupabaseClientはfilter_keywordsテーブルの中身を持たないため、
    load_keywords()経由ではなくarticle_filterのモジュール変数を直接操作する）。"""
    article_filter._SPECIFIC_PATTERNS = [(kw, article_filter._compile(kw)) for kw in keywords]
    article_filter._GENERIC_PATTERNS = []


def test_prefilter_saves_exclusion_log_by_publisher_tag_no_article_content():
    # pass_filter()は「関連キーワードに一致した記事を残す」方式（一致=関連ありとして通過、
    # 不一致=対象外として除外）。ノイズ語フィルタではないことに注意。
    _client_with_keywords(["サステナビリティ"])
    client = FakeSupabaseClient({
        "articles": [], "article_tags": [], "article_analysis": [], "article_files": [],
    })
    media_tag_ids = {"SJ-11-01", "SJ-11-02"}
    articles = [
        {"article_id": "a1", "title": "配当金のお知らせ", "extracted_text": "",
         "_target": {"publisher_tag_id": "SJ-11-01"}},  # 関連キーワード無し→除外
        {"article_id": "a2", "title": "サステナビリティ経営の取り組み", "extracted_text": "",
         "_target": {"publisher_tag_id": "SJ-11-01"}},  # 一致→通過
        {"article_id": "a3", "title": "M&Aに関するプレスリリース", "extracted_text": "",
         "_target": {"publisher_tag_id": "SJ-11-02"}},  # 関連キーワード無し→除外
        {"article_id": "a4", "title": "対象外ソースの記事", "extracted_text": "",
         "_target": {"publisher_tag_id": "OTHER-01"}},  # media_tag_idsに含まれないので判定対象外
    ]

    remaining = article_analyzer._prefilter_articles(client, media_tag_ids, articles)

    # a1, a3が除外され、a2, a4が残る
    assert [a["article_id"] for a in remaining] == ["a2", "a4"]

    log_rows = client.inserted.get("filter_exclusion_log", [])
    by_tag = {r["publisher_tag_id"]: r for r in log_rows}
    assert by_tag["SJ-11-01"]["excluded_count"] == 1
    assert by_tag["SJ-11-01"]["total_checked_count"] == 2
    assert by_tag["SJ-11-02"]["excluded_count"] == 1
    assert by_tag["SJ-11-02"]["total_checked_count"] == 1
    # 判定対象外(OTHER-01)はfilter_keywords照合すらしていないため、ログに現れない
    assert "OTHER-01" not in by_tag

    # 記事本体・タイトル・article_id等が一切含まれていないことを確認（件数集計のみ。
    # id/created_atはFakeSupabaseClient.insert()が自動付与する housekeeping フィールド）
    for row in log_rows:
        assert set(row.keys()) <= {"publisher_tag_id", "excluded_count", "total_checked_count",
                                    "id", "created_at"}
        assert "title" not in row and "article_id" not in row and "extracted_text" not in row


def test_prefilter_keeps_article_body_does_not_delete():
    """最重要の回帰テスト: フィルタ除外された記事の本体(articles/article_tags/
    article_files)が物理削除されず残ること、article_analysisへ
    analysis_status='フィルタ除外'のスタブのみが追加されること。
    delete_filtered_article()が呼ばれていないこと（物理削除への逆戻り防止）も確認する。"""
    _client_with_keywords(["サステナビリティ"])
    client = FakeSupabaseClient({
        "articles": [
            {"article_id": "a1", "title": "配当金のお知らせ"},
            {"article_id": "a2", "title": "サステナビリティ経営の取り組み"},
        ],
        "article_tags": [{"article_id": "a1", "tag_id": "TH-01"}],
        "article_analysis": [], "article_files": [],
    })
    articles = [
        {"article_id": "a1", "title": "配当金のお知らせ", "extracted_text": "",
         "_target": {"publisher_tag_id": "SJ-11-01"}},  # 関連キーワード無し→除外（スタブ保存）
        {"article_id": "a2", "title": "サステナビリティ経営の取り組み", "extracted_text": "",
         "_target": {"publisher_tag_id": "SJ-11-01"}},  # 一致→通過
    ]

    remaining = article_analyzer._prefilter_articles(client, {"SJ-11-01"}, articles)

    assert [a["article_id"] for a in remaining] == ["a2"]

    # 記事本体・タグが削除されずそのまま残っていること（物理削除されていない）
    assert [a["article_id"] for a in client.tables["articles"]] == ["a1", "a2"]
    assert len(client.tables["article_tags"]) == 1
    # deleteが一度も呼ばれていないこと
    assert client.deleted == []

    # article_analysisへフィルタ除外スタブが追加されていること
    stub_rows = [r for r in client.tables["article_analysis"] if r["article_id"] == "a1"]
    assert len(stub_rows) == 1
    assert stub_rows[0]["analysis_status"] == "フィルタ除外"


def test_prefilter_continues_when_exclusion_log_table_missing():
    """回帰テスト: filter_exclusion_logテーブルが無い（マイグレーション未適用）環境で
    実行しても、記事の除外処理自体（save_keyword_filtered_stub）は正常に完了すること。
    2026-08-27に実際にこの例外でarticle_analyzer.main()全体がクラッシュした事故の再発防止。"""
    _client_with_keywords(["サステナビリティ"])
    client = _BrokenInsertClient({
        "articles": [], "article_tags": [], "article_analysis": [], "article_files": [],
    })
    articles = [
        {"article_id": "a1", "title": "配当金のお知らせ", "extracted_text": "",
         "_target": {"publisher_tag_id": "SJ-11-01"}},
        {"article_id": "a2", "title": "サステナビリティ経営の取り組み", "extracted_text": "",
         "_target": {"publisher_tag_id": "SJ-11-01"}},
    ]

    # 例外を投げずに完走すること自体がアサーション
    remaining = article_analyzer._prefilter_articles(client, {"SJ-11-01"}, articles)

    assert [a["article_id"] for a in remaining] == ["a2"]


def test_prefilter_no_media_articles_writes_no_log():
    _client_with_keywords(["ノイズ"])
    client = FakeSupabaseClient({
        "articles": [], "article_tags": [], "article_analysis": [], "article_files": [],
    })
    articles = [{"article_id": "a1", "title": "記事", "extracted_text": "",
                 "_target": {"publisher_tag_id": "OTHER-01"}}]

    remaining = article_analyzer._prefilter_articles(client, {"SJ-11-01"}, articles)

    assert len(remaining) == 1
    assert client.inserted.get("filter_exclusion_log", []) == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
