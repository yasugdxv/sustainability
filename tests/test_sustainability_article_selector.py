import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import json  # noqa: E402

import sustainability_article_selector as sas  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
from tests._fakes import FakeSupabaseClient, FakeKnowledgeStore  # noqa: E402

EXPERT_VERSION = "test-0.1.0"


def _make_assessment(article_id: str, decision: str = "publish_candidate", total_score: int = 85) -> dict:
    return {
        "article_id": article_id,
        "decision": decision,
        "total_score": total_score,
        "score_breakdown": {
            "strategy_relevance": 22, "business_impact": 18, "urgency": 12,
            "exposure": 12, "signal_strength": 8, "actionability": 8,
            "source_quality": 5, "penalty": 0,
        },
        "facts": ["EUが新しい水規制を採択した。"],
        "company_relevance": "当社の水源涵養目標に直接影響する。",
        "affected_themes": ["water"],
        "affected_business_areas": ["欧州工場"],
        "impact_pathways": ["取水許可の厳格化により操業コストが増加する可能性"],
        "selection_reasons": ["2030年水目標の達成条件に関わるため"],
        "questions_to_confirm": ["対象工場は該当地域に含まれるか"],
        "monitoring_signals": ["施行細則の公表"],
        "evidence": [{"claim": "新規制が採択された", "source_url": "https://example.com/reg",
                      "source_type": "primary"}],
        "uncertainties": ["施行時期は未確定"],
        "confidence": 0.7,
        "human_review_required": False,
    }


def _mock_llm_response(data: dict):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=json.dumps(data, ensure_ascii=False)))]
    resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                            completion_tokens_details=MagicMock(reasoning_tokens=0))
    return resp


def _base_tables(article_url_rows: list, article_rows: list) -> dict:
    return {
        "article_urls": article_url_rows,
        "articles": article_rows,
        "crawl_targets": [{"crawl_target_id": "t1", "publisher_name": "Example News",
                            "domain": "news.example.com"}],
        "article_analysis": [{"article_id": a["article_id"], "primary_source_status": "一次情報",
                               "summary_short": "要約", "is_current": True} for a in article_rows],
        "expert_runs": [],
    }


def _single_article_tables() -> dict:
    urls = [{"article_url_id": "u1", "article_url": "https://news.example.com/a",
             "duplicate_of_article_url_id": None}]
    articles = [{"article_id": "a1", "article_url_id": "u1", "title": "EUが新しい水規制を採択",
                 "extracted_text": "EU当局は新しい取水規制を採択し、来年施行される見込み。",
                 "published_at": "2026-07-10T00:00:00+00:00", "final_url": "https://news.example.com/a",
                 "fetched_url": "https://news.example.com/a", "crawl_target_id": "t1", "is_current": True}]
    return _base_tables(urls, articles)


def test_resolve_cluster_root_follows_duplicate_chain():
    """重複判定チェーンを辿って、クラスタの代表(root)を正しく解決できること"""
    all_urls = {
        "u1": {"article_url_id": "u1", "duplicate_of_article_url_id": None},
        "u2": {"article_url_id": "u2", "duplicate_of_article_url_id": "u1"},
        "u3": {"article_url_id": "u3", "duplicate_of_article_url_id": "u2"},
    }
    assert common.resolve_cluster_root(all_urls["u3"], all_urls) == "u1"
    assert common.resolve_cluster_root(all_urls["u1"], all_urls) == "u1"


def test_duplicate_articles_resolve_to_single_cluster():
    """結合テストケース3: 同一事象を扱う重複記事は1つのクラスタにまとまり、
    選定処理が1回だけ実行対象になること"""
    urls = [
        {"article_url_id": "u1", "article_url": "https://a.example.com/1",
         "duplicate_of_article_url_id": None},
        {"article_url_id": "u2", "article_url": "https://b.example.com/1",
         "duplicate_of_article_url_id": "u1"},
    ]
    articles = [
        {"article_id": "a1", "article_url_id": "u1", "title": "水規制のニュース(一次情報)",
         "extracted_text": "本文1", "published_at": "2026-07-10T00:00:00+00:00",
         "final_url": "https://a.example.com/1", "fetched_url": "https://a.example.com/1",
         "crawl_target_id": "t1", "is_current": True},
        {"article_id": "a2", "article_url_id": "u2", "title": "水規制のニュース(転載)",
         "extracted_text": "本文2", "published_at": "2026-07-10T01:00:00+00:00",
         "final_url": "https://b.example.com/1", "fetched_url": "https://b.example.com/1",
         "crawl_target_id": "t1", "is_current": True},
    ]
    client = FakeSupabaseClient(_base_tables(urls, articles))

    cluster_ids = sas.list_candidate_cluster_ids(client)
    assert cluster_ids == ["u1"]

    cluster = common.get_cluster(client, "u1")
    assert cluster["representative"]["article_id"] == "a1"
    assert [m["article_id"] for m in cluster["members"]] == ["a2"]


def test_select_cluster_important_article_becomes_publish_candidate():
    """結合テストケース1: 当社目標に直接関係する重要記事はpublish_candidateとして保存されること"""
    client = FakeSupabaseClient(_single_article_tables())
    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response(
        _make_assessment("a1", decision="publish_candidate", total_score=85))
    knowledge_store = FakeKnowledgeStore()
    expert_base = common.load_expert_base()

    result = sas.select_cluster(client, azure_client, "gpt-4o", knowledge_store,
                                 expert_base, EXPERT_VERSION, "u1")

    assert result["status"] == "success"
    assert result["decision"] == "publish_candidate"
    saved = client.inserted["expert_runs"][0]
    assert saved["task_type"] == "select"
    assert saved["status"] == "success"
    assert saved["output_json"]["decision"] == "publish_candidate"
    assert saved["context_chunk_ids"] == ["KB-TEST-001"]


def test_select_cluster_weak_relevance_article_not_selected():
    """結合テストケース2: 一般的なESG記事だが当社との影響経路が弱い記事はnot_selectedになること"""
    client = FakeSupabaseClient(_single_article_tables())
    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response(
        _make_assessment("a1", decision="not_selected", total_score=30))
    knowledge_store = FakeKnowledgeStore()
    expert_base = common.load_expert_base()

    result = sas.select_cluster(client, azure_client, "gpt-4o", knowledge_store,
                                 expert_base, EXPERT_VERSION, "u1")

    assert result["status"] == "success"
    assert result["decision"] == "not_selected"


def test_select_cluster_skips_duplicate_execution():
    """同一クラスタ・同一入力・同一専門家バージョンでの重複LLM実行を防ぐこと"""
    client = FakeSupabaseClient(_single_article_tables())
    azure_client = MagicMock()
    azure_client.chat.completions.create.return_value = _mock_llm_response(
        _make_assessment("a1", decision="publish_candidate", total_score=85))
    knowledge_store = FakeKnowledgeStore()
    expert_base = common.load_expert_base()

    first = sas.select_cluster(client, azure_client, "gpt-4o", knowledge_store,
                                expert_base, EXPERT_VERSION, "u1")
    assert first["status"] == "success"
    assert azure_client.chat.completions.create.call_count == 1

    second = sas.select_cluster(client, azure_client, "gpt-4o", knowledge_store,
                                 expert_base, EXPERT_VERSION, "u1")
    assert second["status"] == "skipped_duplicate"
    assert azure_client.chat.completions.create.call_count == 1  # 追加のLLM呼び出しが発生していない


def _expert_run_row(run_id, cluster_id, decision, total_score, created_at, expert_version=EXPERT_VERSION):
    return {
        "run_id": run_id, "article_cluster_id": cluster_id, "task_type": "select",
        "status": "success", "expert_version": expert_version, "created_at": created_at,
        "output_json": {"decision": decision, "total_score": total_score},
    }


def test_list_weekly_picks_excludes_not_selected_and_sorts_by_score():
    client = FakeSupabaseClient({"expert_runs": [
        _expert_run_row("r1", "c1", "publish_candidate", 90, "2026-07-20T00:00:00+00:00"),
        _expert_run_row("r2", "c2", "not_selected", 99, "2026-07-20T00:00:00+00:00"),
        _expert_run_row("r3", "c3", "watch_or_archive", 60, "2026-07-20T00:00:00+00:00"),
    ]})
    picks = sas.list_weekly_picks(client, since_days=7, target_max=20)
    assert [p["article_cluster_id"] for p in picks] == ["c1", "c3"]
    assert picks[0]["total_score"] == 90


def test_list_weekly_picks_truncates_to_target_max():
    rows = [_expert_run_row(f"r{i}", f"c{i}", "publish_candidate", 100 - i, "2026-07-20T00:00:00+00:00")
            for i in range(10)]
    client = FakeSupabaseClient({"expert_runs": rows})
    picks = sas.list_weekly_picks(client, since_days=7, target_max=3)
    assert len(picks) == 3
    assert [p["total_score"] for p in picks] == [100, 99, 98]


def test_list_weekly_picks_keeps_only_latest_run_per_cluster():
    client = FakeSupabaseClient({"expert_runs": [
        _expert_run_row("r1", "c1", "not_selected", 20, "2026-07-18T00:00:00+00:00"),
        _expert_run_row("r2", "c1", "publish_candidate", 88, "2026-07-20T00:00:00+00:00"),
    ]})
    picks = sas.list_weekly_picks(client, since_days=7, target_max=20)
    assert len(picks) == 1
    assert picks[0]["select_run_id"] == "r2"
    assert picks[0]["decision"] == "publish_candidate"


def test_list_weekly_picks_filters_by_expert_version():
    client = FakeSupabaseClient({"expert_runs": [
        _expert_run_row("r1", "c1", "publish_candidate", 90, "2026-07-20T00:00:00+00:00",
                         expert_version="0.1.0"),
        _expert_run_row("r2", "c2", "publish_candidate", 95, "2026-07-20T00:00:00+00:00",
                         expert_version="0.2.0"),
    ]})
    picks = sas.list_weekly_picks(client, since_days=7, expert_version="0.2.0", target_max=20)
    assert [p["article_cluster_id"] for p in picks] == ["c2"]


def test_list_weekly_picks_excludes_stale_runs_outside_since_days():
    client = FakeSupabaseClient({"expert_runs": [
        _expert_run_row("r1", "c1", "publish_candidate", 90, "2020-01-01T00:00:00+00:00"),
        _expert_run_row("r2", "c2", "publish_candidate", 80, "2026-07-20T00:00:00+00:00"),
    ]})
    picks = sas.list_weekly_picks(client, since_days=7, target_max=20)
    assert [p["article_cluster_id"] for p in picks] == ["c2"]
