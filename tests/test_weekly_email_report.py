import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import weekly_email_report as wer  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402

TAG_REF = {
    "TH-01": {"tag_id": "TH-01", "tag_axis": "テーマ", "tag_level": "大分類", "tag_code": "01",
              "tag_name": "水", "parent_tag_id": None},
    "CR-01": {"tag_id": "CR-01", "tag_axis": "横断", "tag_level": "大分類", "tag_code": "01",
              "tag_name": "情報開示", "parent_tag_id": None},
    "SJ-05": {"tag_id": "SJ-05", "tag_axis": "主体", "tag_level": "大分類", "tag_code": "05",
              "tag_name": "国際機関", "parent_tag_id": None},
    "SJ-05-01": {"tag_id": "SJ-05-01", "tag_axis": "主体", "tag_level": "小分類", "tag_code": "05-01",
                 "tag_name": "国連", "parent_tag_id": "SJ-05"},
    "MT-E": {"tag_id": "MT-E", "tag_axis": "マテリアリティ接続", "tag_level": "大分類", "tag_code": "E",
             "tag_name": "E：水・自然関連", "parent_tag_id": None},
    "MT-I": {"tag_id": "MT-I", "tag_axis": "マテリアリティ接続", "tag_level": "大分類", "tag_code": "I",
             "tag_name": "I：物理的環境", "parent_tag_id": None},
}


def _mock_llm_response(data: dict):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=json.dumps(data, ensure_ascii=False)))]
    resp.usage = MagicMock(prompt_tokens=50, completion_tokens=30, total_tokens=80,
                            completion_tokens_details=MagicMock(reasoning_tokens=0))
    return resp


def _report_row(report_id="r-1", review_status="review_required"):
    return {
        "report_id": report_id, "period_start": "2026-07-12", "period_end": "2026-07-19",
        "article_ids": ["a-1"], "subject": "件名", "html_body": "<p>本文</p>",
        "draft_content": {"articles": [], "synthesis": {}, "since_days": 7},
        "status": "success", "review_status": review_status,
        "send_mode": None, "recipients": [], "send_status": None,
    }


def test_categorize_tags_resolves_subcategory_to_parent():
    result = wer._categorize_tags(["SJ-05-01"], TAG_REF)
    assert result["subject_tags"] == ["国際機関"]


def test_categorize_tags_defaults_to_other_when_no_theme():
    result = wer._categorize_tags(["CR-01"], TAG_REF)
    assert result["themes"] == ["その他"]


def test_build_articles_from_picks_builds_dicts_with_selector_enrichment():
    tables = {
        "article_urls": [
            {"article_url_id": "u-1", "article_url": "https://example.com/a1",
             "duplicate_of_article_url_id": None},
            {"article_url_id": "u-2", "article_url": "https://example.com/a2",
             "duplicate_of_article_url_id": None},
        ],
        "articles": [
            {"article_id": "a-1", "article_url_id": "u-1", "title": "国際機関が水リスク評価を更新",
             "extracted_text": "本文1", "published_at": "2026-07-17T00:00:00+00:00",
             "final_url": "https://example.com/a1", "fetched_url": "https://example.com/a1",
             "crawl_target_id": "t-1", "is_current": True},
            {"article_id": "a-2", "article_url_id": "u-2", "title": "気候変動対応が進む",
             "extracted_text": "本文2", "published_at": "2026-07-16T00:00:00+00:00",
             "final_url": "https://example.com/a2", "fetched_url": "https://example.com/a2",
             "crawl_target_id": "t-1", "is_current": True},
        ],
        "crawl_targets": [
            {"crawl_target_id": "t-1", "publisher_name": "Example News", "domain": "example.com"},
        ],
        "article_analysis": [
            {"article_id": "a-1", "importance_level": "S", "importance_reason": "最重要事象",
             "summary_short": "水リスク評価が更新", "primary_source_status": "一次情報", "is_current": True},
            {"article_id": "a-2", "importance_level": "B", "importance_reason": "通常",
             "summary_short": "気候変動対応", "primary_source_status": "一次情報", "is_current": True},
        ],
        "article_tags": [
            {"article_id": "a-1", "tag_id": "TH-01"},
        ],
        "tag_reference": list(TAG_REF.values()),
    }
    client = FakeSupabaseClient(tables)
    picks = [
        {"article_cluster_id": "u-2", "select_run_id": "run-2", "decision": "watch_or_archive",
         "total_score": 55, "assessment": {"selection_reasons": [], "evidence": []}},
        {"article_cluster_id": "u-1", "select_run_id": "run-1", "decision": "publish_candidate",
         "total_score": 82, "assessment": {
             "selection_reasons": ["水リスクに関する重要な動き"],
             "evidence": [{"claim": "水リスク評価手法が更新された", "source_url": "https://example.com/a1",
                           "source_type": "primary"}]}},
    ]

    result = wer.build_articles_from_picks(client, picks)

    assert [a["article_id"] for a in result] == ["a-1", "a-2"]  # total_score降順
    top = result[0]
    assert top["title"] == "国際機関が水リスク評価を更新"
    assert top["themes"] == ["水"]
    assert top["importance_level"] == "S"
    assert top["publisher"] == "Example News"
    assert top["selector_total_score"] == 82
    assert top["selector_decision"] == "publish_candidate"
    assert top["selector_selection_reasons"] == ["水リスクに関する重要な動き"]


def test_rewrite_article_for_digest_calls_llm_and_returns_headline_summary():
    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response(
        {"headline": "水リスク評価手法が更新", "summary": "国際機関が水リスク評価手法を更新した。サス推では主要拠点の該当有無を確認。"})
    expert_base = {"company_context": {"focus_areas": []}}
    article = {"title": "国際機関が水リスク評価を更新", "themes": ["水"],
               "summary_short": "水リスク評価が更新", "extracted_text": "本文"}

    result = wer.rewrite_article_for_digest(azure_client, "gpt-4o", expert_base, article)

    assert result["headline"] == "水リスク評価手法が更新"
    assert "サス推" in result["summary"]


def test_rewrite_article_for_digest_includes_selector_reasons_when_present():
    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response(
        {"headline": "見出し", "summary": "要点"})
    article = {"title": "t", "themes": ["水"], "extracted_text": "本文",
               "selector_selection_reasons": ["水リスクに関する重要な動き"]}

    wer.rewrite_article_for_digest(azure_client, "gpt-4o", {"company_context": {}}, article)

    sent_user_prompt = azure_client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "水リスクに関する重要な動き" in sent_user_prompt


def test_build_weekly_synthesis_returns_empty_when_no_articles():
    result = wer.build_weekly_synthesis(MagicMock(), "gpt-4o", {"company_context": {}}, [])
    assert result == {"overview": "", "highlights": [], "theme_digests": []}


def test_build_weekly_synthesis_calls_llm():
    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response({
        "overview": "今週は水関連の動きが目立った週でした。",
        "highlights": ["水リスク評価手法の更新が複数確認された"],
        "theme_digests": [{"theme": "水", "digest": "水リスク評価の更新が続く。"}],
    })
    rewritten = [{"themes": ["水"], "headline": "水リスク評価手法が更新", "summary": "要点"}]

    result = wer.build_weekly_synthesis(azure_client, "gpt-4o", {"company_context": {}}, rewritten)

    assert result["overview"]
    assert result["theme_digests"][0]["theme"] == "水"


def test_build_subject_counts_themes_and_articles():
    rewritten = [
        {"themes": ["水"]}, {"themes": ["水"]}, {"themes": ["気候変動・GHG"]},
    ]
    subject = wer.build_subject(rewritten)
    assert "水" in subject
    assert "3件" in subject
    assert subject.startswith("【サステナ週次】")


def test_build_email_renders_sections_and_table():
    rewritten = [
        {"article_id": "a-1", "themes": ["水"], "cross_tags": ["情報開示"], "subject_tags": ["国際機関"],
         "materiality_codes": ["E", "I"], "headline": "水リスク評価手法が更新",
         "summary": "国際機関が水リスク評価手法を更新した。サス推では確認が必要。",
         "url": "https://example.com/a1", "importance_level": "S"},
    ]
    synthesis = {"overview": "今週は水関連の動きが目立ちました。",
                 "highlights": ["水リスク評価の更新が複数確認された"],
                 "theme_digests": [{"theme": "水", "digest": "水リスク評価の更新が続く。"}]}

    subject, html = wer.build_email(rewritten, synthesis, since_days=7)

    assert "水リスク評価手法が更新" in html
    assert "E/I" in html
    assert "今週の注目ポイント" in html
    assert "テーマ別ダイジェスト" in html
    assert "水リスク評価の更新が続く" in html


def test_build_email_handles_empty_results():
    subject, html = wer.build_email([], {"overview": "", "highlights": [], "theme_digests": []}, since_days=7)
    assert "該当する記事はありませんでした" in html


def test_send_email_falls_back_to_preview_when_not_configured(tmp_path, monkeypatch):
    """email.enabledがfalse、またはsmtp_host未設定なら、SMTP送信せずプレビュー保存すること"""
    monkeypatch.setattr(wer, "CACHE_DIR", tmp_path)
    result = wer.send_email("件名", "<p>本文</p>", {"email": {"enabled": False}})

    assert result["ok"] is True
    assert result["mode"] == "preview"
    assert Path(result["path"]).exists()
    assert "本文" in Path(result["path"]).read_text(encoding="utf-8")


def test_rewrite_article_for_digest_records_usage_when_log_provided():
    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response(
        {"headline": "見出し", "summary": "要点"})
    usage_log = []

    wer.rewrite_article_for_digest(
        azure_client, "gpt-4o", {"company_context": {}},
        {"title": "t", "themes": ["水"], "extracted_text": ""}, usage_log=usage_log)

    assert len(usage_log) == 1
    assert usage_log[0]["token_usage"]["total_tokens"] == 80


def test_aggregate_usage_sums_across_calls():
    usage_log = [
        {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "reasoning_tokens": 0},
         "latency_ms": 500},
        {"token_usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50, "reasoning_tokens": 5},
         "latency_ms": 300},
    ]
    totals, latency = wer._aggregate_usage(usage_log)
    assert totals == {"prompt_tokens": 130, "completion_tokens": 70, "total_tokens": 200, "reasoning_tokens": 5}
    assert latency == 800


def test_save_draft_report_saves_review_required_row():
    client = FakeSupabaseClient({"weekly_email_reports": []})

    report_id = wer.save_draft_report(
        client, period_start=dt.date(2026, 7, 12), period_end=dt.date(2026, 7, 19),
        article_ids=["a-1"], model_deployment="gpt-4o", token_usage={"total_tokens": 100},
        latency_ms=1000, status="success", error_message=None, subject="件名",
        draft_content={"articles": [], "synthesis": {}, "since_days": 7}, html_body="<p>本文</p>",
    )

    assert report_id is not None
    saved = client.tables["weekly_email_reports"][0]
    assert saved["review_status"] == "review_required"
    assert saved["article_ids"] == ["a-1"]


def test_save_draft_report_does_not_raise_when_table_missing():
    """weekly_email_reportsテーブルが未作成(マイグレーション未実施)でも例外を送出しないこと"""

    class BrokenClient:
        def insert(self, *args, **kwargs):
            raise RuntimeError("relation \"weekly_email_reports\" does not exist")

    report_id = wer.save_draft_report(
        BrokenClient(), period_start=None, period_end=None, article_ids=[],
        model_deployment="gpt-4o", token_usage={}, latency_ms=0,
        status="success", error_message=None, subject="件名",
        draft_content={}, html_body="",
    )
    assert report_id is None


def test_reject_report_sets_rejected_status():
    client = FakeSupabaseClient({"weekly_email_reports": [_report_row()]})
    wer.reject_report(client, "r-1", reviewer_id="reviewer-a", reviewer_feedback={"free_text": "対象外"})
    row = client.tables["weekly_email_reports"][0]
    assert row["review_status"] == "rejected"
    assert row["reviewer_id"] == "reviewer-a"


def test_save_draft_edits_regenerates_html_and_subject():
    client = FakeSupabaseClient({"weekly_email_reports": [_report_row()]})
    draft_content = {
        "articles": [{"article_id": "a-1", "themes": ["水"], "cross_tags": [], "subject_tags": [],
                       "materiality_codes": [], "headline": "見出し", "summary": "要点",
                       "url": "https://example.com/a1", "importance_level": "S"}],
        "synthesis": {"overview": "概況", "highlights": [], "theme_digests": []},
        "since_days": 7,
    }
    wer.save_draft_edits(client, "r-1", draft_content, since_days=7)
    updated = client.tables["weekly_email_reports"][0]
    assert "見出し" in updated["html_body"]
    assert updated["draft_content"] == draft_content


def test_approve_and_send_success_marks_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(wer, "CACHE_DIR", tmp_path)
    client = FakeSupabaseClient({"weekly_email_reports": [_report_row()]})
    config = {"email": {"enabled": False}}

    result = wer.approve_and_send(client, config, "r-1", reviewer_id="reviewer-a")

    assert result["ok"] is True
    assert result["review_status"] == "sent"
    row = client.tables["weekly_email_reports"][0]
    assert row["review_status"] == "sent"
    assert row["send_status"] == "success"
    assert row["reviewer_id"] == "reviewer-a"


def test_approve_and_send_failure_keeps_approved_for_retry(monkeypatch):
    client = FakeSupabaseClient({"weekly_email_reports": [_report_row()]})
    monkeypatch.setattr(wer, "send_email", lambda *a, **k: {"ok": False, "mode": "smtp", "error": "boom"})
    config = {"email": {"enabled": True, "smtp_host": "smtp.example.com"}}

    result = wer.approve_and_send(client, config, "r-1", reviewer_id="reviewer-a")

    assert result["ok"] is False
    assert result["review_status"] == "approved"
    row = client.tables["weekly_email_reports"][0]
    assert row["review_status"] == "approved"
    assert row["send_status"] == "error"


def test_approve_and_send_refuses_rejected_report():
    client = FakeSupabaseClient({"weekly_email_reports": [_report_row(review_status="rejected")]})
    result = wer.approve_and_send(client, {"email": {"enabled": False}}, "r-1", reviewer_id="reviewer-a")
    assert result["ok"] is False


def test_record_send_result_advances_to_sent_on_success():
    client = FakeSupabaseClient({"weekly_email_reports": [_report_row(review_status="approved")]})
    wer.record_send_result(client, "r-1", send_mode="preview", recipients=[], send_status="success")
    row = client.tables["weekly_email_reports"][0]
    assert row["review_status"] == "sent"
    assert row["send_status"] == "success"


def test_list_reports_filters_by_review_status():
    client = FakeSupabaseClient({"weekly_email_reports": [
        _report_row(report_id="r-1", review_status="review_required"),
        _report_row(report_id="r-2", review_status="sent"),
    ]})
    result = wer.list_reports(client, review_status="review_required")
    assert [r["report_id"] for r in result] == ["r-1"]


def test_get_report_returns_none_when_missing():
    client = FakeSupabaseClient({"weekly_email_reports": []})
    assert wer.get_report(client, "missing") is None
