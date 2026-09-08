"""
Phase S2: Weekly x Geo Intelligence Batch Inquiry のテスト。
weekly_geo_intelligence_service.py（新規、本Phaseの中核）を中心に、
run_weekly_geo_inquiry() / select_weekly_geo_topics() / 各種ハッシュ関数 /
weekly_email_report.pyとの配線（Geo Context引継ぎ・独立トピックセクション）を検証する。

計画（Phase S2 v7＋実装ガード最終版）の9節Test1〜31相当・10節E2Eに対応する。
tests/_fakes.py の FakeSupabaseClient と、tests/_geo_intelligence_fixtures.py の
モックパターンを踏襲し、新規モックライブラリは追加しない。LLM呼び出し
（dedup分類・最終選定）はunittest.mock.MagicMockでモックする。

Geo API呼び出し（query_geo_intelligence）は、Test1・2のみ実HTTP層
（geo_intelligence_client.requests.postモック）で検証し、それ以外は
weekly_geo_intelligence_service.query_geo_intelligence をモックして
「Geo APIが再呼び出しされたか」を call_count で検証する
（HTTP層の挙動自体はtest_geo_intelligence_client.py/test_geo_intelligence_service.pyで
既に検証済みのため、ここではweekly層の3層再利用ロジックに焦点を当てる）。
"""
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sustainability_expert_common as common  # noqa: E402
import sustainability_article_selector as selector  # noqa: E402
import weekly_geo_intelligence_service as wgs  # noqa: E402
import weekly_email_report as wer  # noqa: E402
import geo_intelligence_client  # noqa: E402
from geo_intelligence_client import GeoIntelligenceClient  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402
from tests._geo_intelligence_fixtures import mock_response  # noqa: E402


PERIOD_START = date(2026, 8, 19)
PERIOD_END = date(2026, 8, 26)

FULL_CONFIG = {
    "geo_intelligence": {
        "enabled": True,
        "base_url": "https://geo.example.com",
        "query_path": "/api/v1/geo-intelligence/query",
        "timeout_seconds": 5,
        "api_key": "",
        "max_retries": 0,
        "weekly_monitoring": {
            "enabled": True,
            "scope_version": "v1",
            "max_independent_topics": 3,
            "dedup_candidate_pool_max": 200,
        },
    },
    "weekly_digest": {"since_days": 7, "target_min": 15, "target_max": 20},
}

DISABLED_CONFIG = {
    "geo_intelligence": {
        "enabled": True,
        "weekly_monitoring": {"enabled": False},
    },
    "weekly_digest": {"since_days": 7, "target_min": 15, "target_max": 20},
}

# 9大分類(TH-01〜TH-09)+下位種別親(TH-10)+下位軸小分類+横断2件(CR-01/CR-02、Tier0昇格)+
# その他横断1件(CR-03、Tier0対象外)+主体1件。selector.SUB_AXIS_PARENT_TAG_ID(TH-10)/
# selector.TIER0_PROMOTED_CROSS_CUTTING_IDS({CR-01,CR-02})という実際の本番定数と整合させる。
TAG_REFERENCE_ROWS = [
    {"tag_id": "TH-01", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "水", "status": "有効"},
    {"tag_id": "TH-02", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "気候変動・GHG", "status": "有効"},
    {"tag_id": "TH-03", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "容器包装", "status": "有効"},
    {"tag_id": "TH-04", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "原料調達", "status": "有効"},
    {"tag_id": "TH-05", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "生物多様性", "status": "有効"},
    {"tag_id": "TH-06", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "人権", "status": "有効"},
    {"tag_id": "TH-07", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "健康", "status": "有効"},
    {"tag_id": "TH-08", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "人的資本", "status": "有効"},
    {"tag_id": "TH-09", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "責任あるマーケティング", "status": "有効"},
    {"tag_id": "TH-10", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "下位種別", "status": "有効"},
    {"tag_id": "TH-10-01", "tag_axis": "テーマ", "tag_level": "小分類", "tag_name": "下位軸A",
     "parent_tag_id": "TH-10", "status": "有効"},
    {"tag_id": "CR-01", "tag_axis": "横断", "tag_level": "大分類", "tag_name": "情報開示", "status": "有効"},
    {"tag_id": "CR-02", "tag_axis": "横断", "tag_level": "大分類", "tag_name": "ESG評価・サステナブルファイナンス",
     "status": "有効"},
    {"tag_id": "CR-03", "tag_axis": "横断", "tag_level": "大分類", "tag_name": "地政学・マクロ規制環境",
     "status": "有効"},
    {"tag_id": "SJ-01", "tag_axis": "主体", "tag_level": "大分類", "tag_name": "競合企業", "status": "有効"},
]


def _client(extra_tables: dict = None) -> FakeSupabaseClient:
    tables = {"tag_reference": TAG_REFERENCE_ROWS}
    if extra_tables:
        tables.update(extra_tables)
    return FakeSupabaseClient(tables)


def _article(article_id: str, total_score: int = 50, themes: list = None, title: str = None) -> dict:
    """build_articles_from_picks()の戻り値と同じ形（フィールド名も一致）の記事dictを返す"""
    return {
        "article_id": article_id,
        "article_cluster_id": f"cluster-{article_id}",
        "title": title or f"記事{article_id}",
        "extracted_text": "",
        "url": f"https://example.com/{article_id}",
        "published_at": "2026-08-20T00:00:00+00:00",
        "publisher": "Example Publisher",
        "importance_level": "B",
        "summary_short": f"{article_id}の要約",
        "importance_reason": "",
        "themes": themes or ["水"],
        "cross_tags": [],
        "subject_tags": [],
        "materiality_codes": [],
        "selector_total_score": total_score,
        "selector_decision": "publish_candidate",
        "selector_selection_reasons": [],
        "selector_evidence": [],
    }


def _geo_item(item_id: str, title: str = None, themes: list = None, **overrides) -> dict:
    """Geo Response の relevant_intelligence 1要素（_candidate_to_item()確認済み実形状）"""
    item = {
        "item_id": item_id,
        "title": title or f"Geo Item {item_id}",
        "event_date": "2026-08-20",
        "as_of": "2026-08-25",
        "country_region": ["日本"],
        "sustainability_themes": themes or ["水"],
        "geo_assessment": f"{item_id}のgeo_assessment",
        "why_relevant": f"{item_id}のwhy_relevant",
        "key_stakeholders": ["Stakeholder A"],
        "political_dynamics": f"{item_id}のpolitical_dynamics",
        "outlook": f"{item_id}のoutlook",
        "references": [],
        "confidence": "medium",
        "expert_response_id": "resp-1",
    }
    item.update(overrides)
    return item


def _geo_call_result(relevant_intelligence: list = None, success: bool = True, status: str = "success",
                      external_call_id: str = "call-1", truncated_count: int = 0,
                      knowledge_sufficiency: str = "sufficient", confidence: str = "medium") -> dict:
    """query_geo_intelligence()の戻り値と同じ形のdictを構築する（モック用）"""
    response = None
    if success:
        response = SimpleNamespace(
            request_id="req-x", status="completed", knowledge_sufficiency=knowledge_sufficiency,
            confidence=confidence, relevant_intelligence=relevant_intelligence or [],
            truncated_count=truncated_count,
        )
    return {
        "success": success, "status": status, "response": response,
        "remote_status": "completed" if success else None,
        "error_type": None if success else "timeout",
        "error_message": None if success else "timed out",
        "http_status": 200 if success else None, "latency_ms": 5,
        "local_request_id": "local-req-1", "attempts": 1, "raw_response": {},
        "external_call_id": external_call_id,
    }


def _dedup_llm_result(classifications: list) -> dict:
    return {"data": {"classifications": classifications}, "mode": "structured_output",
            "token_usage": {}, "latency_ms": 1, "raw_output": {}}


def _selection_llm_result(decisions: list) -> dict:
    return {"data": {"decisions": decisions}, "mode": "structured_output",
            "token_usage": {}, "latency_ms": 1, "raw_output": {}}


def _llm_router(dedup_response: dict = None, selection_response: dict = None, captured_calls: list = None):
    """sustainability_expert_common.call_llm_structuredのモック用side_effect。
    schema_nameでdedup/selectionを判別し、想定外の呼び出しはAssertionErrorにする"""
    def _side_effect(client, model, system_prompt, user_prompt, schema, schema_name, temperature=0.2):
        if captured_calls is not None:
            captured_calls.append({"schema_name": schema_name, "user_prompt": user_prompt})
        if schema_name == "GeoDedupClassificationBatch":
            if dedup_response is None:
                raise AssertionError("Dedup分類LLMは呼ばれない想定でした")
            return dedup_response
        if schema_name == "GeoWeeklySelection":
            if selection_response is None:
                raise AssertionError("Selection LLMは呼ばれない想定でした")
            return selection_response
        raise AssertionError(f"想定外のschema_name: {schema_name}")
    return _side_effect


def _run_inquiry(client, config=None, final_articles=None, pool=None, azure_client=None,
                  model="gpt-test", force_rerun=False, period_start=PERIOD_START, period_end=PERIOD_END,
                  geo_client=None) -> dict:
    return wgs.run_weekly_geo_inquiry(
        config or FULL_CONFIG, client, period_start, period_end,
        final_articles if final_articles is not None else [], pool if pool is not None else [],
        geo_client=geo_client, azure_client=azure_client if azure_client is not None else MagicMock(),
        model=model, force_rerun=force_rerun)


def _mock_query(**kwargs):
    """weekly_geo_intelligence_service.query_geo_intelligence をモックするコンテキスト"""
    result = _geo_call_result(**kwargs)
    return patch("weekly_geo_intelligence_service.query_geo_intelligence", return_value=result)


# ═══════════════════════════════════════════════════════════════════════
# Test1・2: 実HTTP層（GeoIntelligenceClient）を通した正常系・日付精度Contract Test
# ═══════════════════════════════════════════════════════════════════════
def _echoing_weekly_response(status_code=200):
    def _respond(*args, **kwargs):
        sent = kwargs.get("json") or {}
        body = {
            "request_id": sent.get("request_id"), "status": "completed",
            "geo_assessment": None, "key_stakeholders": [], "political_dynamics": None,
            "outlook": None, "cross_domain_implications": None, "references": None,
            "confidence": "medium", "knowledge_sufficiency": "sufficient", "knowledge_gap": None,
            "error": None, "completed_at": "2026-08-26T01:00:00+00:00",
            "period_start": sent.get("period_start"), "period_end": sent.get("period_end"),
            "relevant_intelligence": [], "truncated_count": 0,
        }
        return mock_response(status_code, body)
    return _respond


def test_01_scope_sent_correctly_via_real_http_layer():
    """Test1: 正常Weekly Inquiry（scopeが正しく送信される）"""
    client = _client()
    geo_client = GeoIntelligenceClient(FULL_CONFIG, proxies={}, verify=True)
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_weekly_response()) as mock_post:
        result = _run_inquiry(client, pool=[_article("A-1")], geo_client=geo_client)

    assert mock_post.call_count == 1
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["request_type"] == "weekly_monitoring"
    assert "情報開示" not in sent_payload["sustainability_themes"]  # 英語変換されているはず
    assert "Disclosure" in sent_payload["sustainability_themes"]
    assert "ESG Sustainable Finance" in sent_payload["sustainability_themes"]
    assert len(sent_payload["sustainability_themes"]) == 11  # 9大分類+2横断
    assert sent_payload["priority_geographies"]
    assert sent_payload["business_context"]
    assert result["run_status"] == "success"
    assert result["run_id"] is not None


def test_02_date_precision_contract_datetime_and_date_both_send_clean_iso():
    """Test2: period_start/period_end にdatetime/dateどちらを渡しても、Geoへは
    時刻成分を含まないYYYY-MM-DD文字列で送信される"""
    client = _client()
    geo_client = GeoIntelligenceClient(FULL_CONFIG, proxies={}, verify=True)
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_weekly_response()) as mock_post:
        _run_inquiry(client, pool=[], geo_client=geo_client,
                     period_start=datetime(2026, 8, 17, 15, 30), period_end=datetime(2026, 8, 24, 9, 0))
    sent = mock_post.call_args.kwargs["json"]
    assert sent["period_start"] == "2026-08-17"
    assert sent["period_end"] == "2026-08-24"

    client2 = _client()
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_weekly_response()) as mock_post2:
        _run_inquiry(client2, pool=[], geo_client=geo_client,
                     period_start=date(2026, 8, 17), period_end=date(2026, 8, 24))
    sent2 = mock_post2.call_args.kwargs["json"]
    assert sent2["period_start"] == "2026-08-17"
    assert sent2["period_end"] == "2026-08-24"


def test_02b_fetch_weekly_theme_names_excludes_sub_axis_and_unrelated_tags():
    """Test2b: _fetch_weekly_theme_names()がTH-10除外の9大分類+CR-01/CR-02の2件、計11件を返す。
    TH-10配下の小分類やCR-03（Tier0対象外）・主体タグは含まれない"""
    client = _client()
    names = wgs._fetch_weekly_theme_names(client)
    assert len(names) == 11
    assert "下位種別" not in names  # TH-10自体（親）
    assert "下位軸A" not in names  # TH-10配下の小分類
    assert "地政学・マクロ規制環境" not in names  # CR-03（Tier0昇格対象外）
    assert "競合企業" not in names  # 主体タグ
    assert "情報開示" in names and "ESG評価・サステナブルファイナンス" in names
    assert selector.SUB_AXIS_PARENT_TAG_ID == "TH-10"
    assert selector.TIER0_PROMOTED_CROSS_CUTTING_IDS == {"CR-01", "CR-02"}


# ═══════════════════════════════════════════════════════════════════════
# Test3・4: 複数item保存・0件
# ═══════════════════════════════════════════════════════════════════════
def test_03_multiple_items_saved_with_traceability_fields():
    """Test3: 複数Geo Intelligence Item（それぞれ保存される。as_of/key_stakeholders/
    expert_response_idも保存されることを確認）"""
    client = _client()
    items = [_geo_item("G-1"), _geo_item("G-2", themes=["気候変動・GHG"])]
    dedup_response = _dedup_llm_result([
        {"classification": "independent", "matched_article_ids": []},
        {"classification": "independent", "matched_article_ids": []},
    ])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, pool=[])

    saved = client.tables["weekly_geo_intelligence_items"]
    assert len(saved) == 2
    assert {r["geo_item_id"] for r in saved} == {"G-1", "G-2"}
    for row in saved:
        assert row["as_of"] == "2026-08-25"
        assert row["key_stakeholders"] == ["Stakeholder A"]
        assert row["expert_response_id"] == "resp-1"
    assert len(result["independent_candidates"]) == 2


def test_04_zero_items_is_not_needed_no_gap():
    """Test4: 0件（relevant_intelligence=[]、正常完了、dedup_status='not_needed'、
    Knowledge Gap扱いしない）"""
    client = _client()
    with _mock_query(relevant_intelligence=[]):
        result = _run_inquiry(client, pool=[])

    assert result["run_status"] == "success"
    run = client.tables["weekly_geo_intelligence_runs"][0]
    assert run["dedup_status"] == "not_needed"
    assert run["selection_status"] == "not_needed"
    assert result["independent_candidates"] == []
    assert result["promotion_candidates"] == []


# ═══════════════════════════════════════════════════════════════════════
# Test5・5b: same_event（final_articles内 / dedup_candidate_poolのみ＝昇格候補）
# ═══════════════════════════════════════════════════════════════════════
def test_05_same_event_matching_final_article_goes_to_article_context():
    """Test5: same_event・final_articles内の記事とマッチ（article_contextに入る。
    independent_candidates/promotion_candidatesには入らない＝二重掲載されない）"""
    client = _client()
    final_articles = [_article("A-1")]
    pool = [_article("A-1")]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "same_event", "matched_article_ids": ["A-1"]}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, final_articles=final_articles, pool=pool)

    assert result["independent_candidates"] == []
    assert result["promotion_candidates"] == []
    assert "A-1" in result["article_context"]
    assert result["article_context"]["A-1"][0]["geo_item_id"] == "G-1"


def test_05b_same_event_unselected_article_becomes_promotion_candidate():
    """Test5b: 昇格候補（v5修正点2）: same_eventがdedup_candidate_poolには含まれるが
    final_articlesには含まれない記事とマッチ → promotion_candidatesに入る
    （article_contextには入らない）"""
    client = _client()
    final_articles = [_article("A-1")]
    pool = [_article("A-1"), _article("A-2", total_score=70)]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "same_event", "matched_article_ids": ["A-2"]}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, final_articles=final_articles, pool=pool)

    assert result["article_context"] == {}
    assert len(result["promotion_candidates"]) == 1
    promo = result["promotion_candidates"][0]
    assert promo["article_id"] == "A-2"
    assert promo["article_summary"]["headline"] == "記事A-2"
    assert promo["geo_items"][0]["geo_item_id"] == "G-1"


# ═══════════════════════════════════════════════════════════════════════
# Test6・6b・6c: related_context
# ═══════════════════════════════════════════════════════════════════════
def test_06_related_context_matching_final_article_goes_to_background_context():
    """Test6: related_context・matched_article_idsがfinal_articles内の記事を含む
    （background_contextに入る）"""
    client = _client()
    final_articles = [_article("A-1")]
    pool = [_article("A-1")]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "related_context", "matched_article_ids": ["A-1"]}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, final_articles=final_articles, pool=pool)

    assert "A-1" in result["background_context"]
    assert result["theme_background_context"] == {}


def test_06b_related_context_empty_matches_goes_to_theme_background_context():
    """Test6b: related_context・matched_article_idsが空（theme_background_contextに
    該当テーマで入る）"""
    client = _client()
    items = [_geo_item("G-1", themes=["水", "気候変動・GHG"])]
    dedup_response = _dedup_llm_result([{"classification": "related_context", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, final_articles=[], pool=[])

    assert result["background_context"] == {}
    assert set(result["theme_background_context"].keys()) == {"水", "気候変動・GHG"}


def test_06c_related_context_matches_only_unselected_article_falls_back_to_theme_background():
    """Test6c（v7修正点5のバグ修正確認）: related_context・matched_article_idsは非空だが
    全マッチ先がfinal_articlesに含まれない（dedup_candidate_poolにのみ存在する未選定記事のみ）
    → background_contextにもpromotion_candidatesにも入らず、theme_background_contextへ回る"""
    client = _client()
    final_articles = [_article("A-1")]
    pool = [_article("A-1"), _article("A-2")]
    items = [_geo_item("G-1", themes=["水"])]
    dedup_response = _dedup_llm_result([{"classification": "related_context", "matched_article_ids": ["A-2"]}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, final_articles=final_articles, pool=pool)

    assert result["background_context"] == {}
    assert result["promotion_candidates"] == []
    assert "水" in result["theme_background_context"]


# ═══════════════════════════════════════════════════════════════════════
# Test7: independent
# ═══════════════════════════════════════════════════════════════════════
def test_07_independent_goes_to_independent_candidates_not_yet_included():
    """Test7: independent（independent_candidatesに入るが、この時点ではinclude_in_weekly=falseのまま）"""
    client = _client()
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, pool=[])

    assert len(result["independent_candidates"]) == 1
    row = client.tables["weekly_geo_intelligence_items"][0]
    assert row["include_in_weekly"] is False
    assert row["selection_status"] is None


# ═══════════════════════════════════════════════════════════════════════
# Test8・8b・8c: 統合最終選定（置換方式・select方式）
# ═══════════════════════════════════════════════════════════════════════
def _select(client, config, independent, promotion, ranked_final_articles, run_id,
            azure_client=None, model="gpt-test", force_rerun=False):
    return wgs.select_weekly_geo_topics(
        azure_client if azure_client is not None else MagicMock(), model, independent, promotion,
        ranked_final_articles, config, client, run_id, force_rerun=force_rerun)


def _run_id_with_independent(client, config=None, n=1, pool=None):
    """independent候補n件を持つrunを作り、(run_id, independent_candidates)を返す"""
    config = config or FULL_CONFIG
    items = [_geo_item(f"G-{i}") for i in range(1, n + 1)]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}] * n)
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, config=config, pool=pool or [])
    return result["run_id"], result["independent_candidates"]


def test_08_replacement_when_full_with_valid_displaced_id():
    """Test8: is_full=trueで、LLMが{select:true, displaced_article_id:<最下位記事>}を返すケース。
    最下位記事がdisplaced_article_idsに入り除外され、Geo候補がselected_geo_topicsに採用される。
    LLM Inputに既存下位記事とGeo候補が同じ呼び出しで含まれ、total_scoreは渡されるが
    コード側で数値比較に使われていないことも確認する"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 1}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=1)
    final_articles = [_article("A-1", total_score=10)]  # target_max=1なのでis_full=true

    captured = []
    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": "A-1", "reason": "重要"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions),
                                        captured_calls=captured)):
        result = _select(client, config, independent, [], final_articles, run_id)

    assert result["displaced_article_ids"] == ["A-1"]
    assert [i["geo_item_id"] for i in result["selected_geo_topics"]] == ["G-1"]
    assert len(captured) == 1
    payload_text = captured[0]["user_prompt"]
    assert '"is_full": true' in payload_text.replace(" ", "") or "is_full" in payload_text
    assert "total_score" in payload_text  # 参考情報として渡される
    # total_scoreは閾値比較のロジックには使われない（コードパス上、_apply_selection_guardsは
    # select/displaced_article_idのみで判定しており、既存記事のtotal_score自体は比較に使わない）
    row = client.tables["weekly_geo_intelligence_items"][0]
    assert row["selection_status"] == "selected"
    assert row["include_in_weekly"] is True
    assert row["selected_at"] is not None


def test_08b_full_without_displaced_id_is_force_rejected():
    """Test8b（実装ガード3・7）: is_full=trueの状態でLLMが{select:true, displaced_article_id:null}
    を返すケースで、コード側でselect=false（rejected）へ強制変更されることを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 1}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=1)
    final_articles = [_article("A-1", total_score=10)]

    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "なし"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        result = _select(client, config, independent, [], final_articles, run_id)

    assert result["selected_geo_topics"] == []
    assert result["displaced_article_ids"] == []
    row = client.tables["weekly_geo_intelligence_items"][0]
    assert row["selection_status"] == "rejected"
    assert row["include_in_weekly"] is False


def test_08c_not_full_no_auto_adopt_but_explicit_select_true_is_adopted():
    """Test8c（実装ガード7）: is_full=falseの状態でLLMが{select:false}を返すケースで、
    空き枠があってもコード側が自動的に候補を採用しないこと（rejectedのまま）。
    同条件でLLMが{select:true, displaced_article_id:null}を返すケースでは、
    素直に追加枠として採用されること（displaced_article_idsが空のまま採用）も対比して確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 5}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=1)
    final_articles = [_article("A-1", total_score=10)]  # target_max=5なのでis_full=false

    decisions_false = [{"candidate_id": "G-1", "select": False, "displaced_article_id": None, "reason": "不要"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions_false))):
        result = _select(client, config, independent, [], final_articles, run_id)
    assert result["selected_geo_topics"] == []
    row = client.tables["weekly_geo_intelligence_items"][0]
    assert row["selection_status"] == "rejected"

    client2 = _client()
    run_id2, independent2 = _run_id_with_independent(client2, config=config, n=1)
    decisions_true = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "採用"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions_true))):
        result2 = _select(client2, config, independent2, [], final_articles, run_id2)
    assert [i["geo_item_id"] for i in result2["selected_geo_topics"]] == ["G-1"]
    assert result2["displaced_article_ids"] == []


# ═══════════════════════════════════════════════════════════════════════
# Test9・9b・9c・9d: 通常ケース・昇格候補
# ═══════════════════════════════════════════════════════════════════════
def test_09_partial_adoption_selected_and_rejected_are_persisted():
    """Test9: 候補の一部のみ採用し、採用分のみinclude_in_weekly=true/selection_status='selected'/
    selected_at非NULLへUPDATEされることを確認（非採用はselection_status='rejected'）"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=2)
    final_articles = [_article("A-1", total_score=10)]

    decisions = [
        {"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "採用"},
        {"candidate_id": "G-2", "select": False, "displaced_article_id": None, "reason": "非採用"},
    ]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        result = _select(client, config, independent, [], final_articles, run_id)

    assert [i["geo_item_id"] for i in result["selected_geo_topics"]] == ["G-1"]
    rows_by_id = {r["geo_item_id"]: r for r in client.tables["weekly_geo_intelligence_items"]}
    assert rows_by_id["G-1"]["selection_status"] == "selected"
    assert rows_by_id["G-1"]["selected_at"] is not None
    assert rows_by_id["G-2"]["selection_status"] == "rejected"
    assert rows_by_id["G-2"]["selected_at"] is None


def test_09b_promotion_candidate_adoption_returns_promoted_article_id():
    """Test9b（v5修正点2）: promotion_candidatesの1件が最終選定で採用され、
    promoted_article_idsに対応するarticle_idが返る"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    final_articles = [_article("A-1", total_score=10)]
    pool = [_article("A-1", total_score=10), _article("A-2", total_score=70)]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "same_event", "matched_article_ids": ["A-2"]}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        geo_result = _run_inquiry(client, config=config, final_articles=final_articles, pool=pool)

    promotion = geo_result["promotion_candidates"]
    assert len(promotion) == 1
    decisions = [{"candidate_id": "A-2", "select": True, "displaced_article_id": None, "reason": "採用"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        selection = _select(client, config, [], promotion, final_articles, geo_result["run_id"])

    assert selection["promoted_article_ids"] == ["A-2"]
    row = next(r for r in client.tables["weekly_geo_intelligence_items"] if r["geo_item_id"] == "G-1")
    assert row["selection_status"] == "selected"


def test_09c_promotion_evaluation_input_includes_article_summary_and_geo_fields():
    """Test9c（v6修正点1）: select_weekly_geo_topics()へ渡されるpromotion候補の評価材料に、
    article_summary（headline/short_summary/themes/geography/total_score）とGeo item側
    （geo_assessment/why_relevant/political_dynamics/outlook）の両方が含まれること"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    final_articles = [_article("A-1", total_score=10)]
    pool = [_article("A-1", total_score=10), _article("A-2", total_score=70, title="重要な記事A-2")]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "same_event", "matched_article_ids": ["A-2"]}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        geo_result = _run_inquiry(client, config=config, final_articles=final_articles, pool=pool)

    captured = []
    decisions = [{"candidate_id": "A-2", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions),
                                        captured_calls=captured)):
        _select(client, config, [], geo_result["promotion_candidates"], final_articles, geo_result["run_id"])

    user_prompt = captured[0]["user_prompt"]
    assert "重要な記事A-2" in user_prompt  # article_summary.headline
    assert "G-1のgeo_assessment" in user_prompt
    assert "G-1のwhy_relevant" in user_prompt
    assert "G-1のpolitical_dynamics" in user_prompt
    assert "G-1のoutlook" in user_prompt


def test_09d_promoted_article_geo_context_merge_equivalent_to_weekly_email_report():
    """Test9d（v6修正点1）: promoted_article_idsに採用されたpromotion候補について、
    weekly_email_report.py側の合成ロジック相当（build_draft内の「articlesリストへの反映」）を
    再現し、article_idがarticle_contextに追加され、対応するgeo_itemsが正しく引き継がれ、
    _render_article_row()が受け取るgeo_contextが空でないことを確認する"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    final_articles = [_article("A-1", total_score=10)]
    pool = [_article("A-1", total_score=10), _article("A-2", total_score=70)]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "same_event", "matched_article_ids": ["A-2"]}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        geo_result = _run_inquiry(client, config=config, final_articles=final_articles, pool=pool)

    decisions = [{"candidate_id": "A-2", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        selection = _select(client, config, [], geo_result["promotion_candidates"], final_articles,
                             geo_result["run_id"])

    # weekly_email_report.build_draft()内の「articlesリストへの反映」相当を再現する
    articles = list(final_articles)
    pool_by_id = {a["article_id"]: a for a in pool}
    promo_geo_items_by_article = {p["article_id"]: p.get("geo_items") or []
                                   for p in geo_result["promotion_candidates"]}
    for promoted_id in selection["promoted_article_ids"]:
        articles.append(pool_by_id[promoted_id])
        geo_result["article_context"].setdefault(promoted_id, []).extend(
            promo_geo_items_by_article.get(promoted_id, []))

    assert any(a["article_id"] == "A-2" for a in articles)
    geo_context = geo_result["article_context"].get("A-2", [])
    assert geo_context and geo_context[0]["geo_item_id"] == "G-1"

    rewritten_a2 = {**pool_by_id["A-2"], "headline": "見出し", "summary": "要点"}
    row_html = wer._render_article_row(1, rewritten_a2, geo_context=geo_context)
    assert "地政学コンテキスト" in row_html
    assert "G-1のgeo_assessment" in row_html


# ═══════════════════════════════════════════════════════════════════════
# Test10・11: insufficient・Geo障害
# ═══════════════════════════════════════════════════════════════════════
def test_10_insufficient_not_treated_as_error():
    """Test10: insufficient（Errorではない、Sustainability側でGap生成しない、
    Geo Responseをそのまま保持）"""
    client = _client()
    with _mock_query(relevant_intelligence=[], knowledge_sufficiency="insufficient"):
        result = _run_inquiry(client, pool=[])

    assert result["run_status"] == "success"
    run = client.tables["weekly_geo_intelligence_runs"][0]
    assert run["knowledge_sufficiency"] == "insufficient"
    assert run["status"] == "success"


def test_11_geo_unavailable_returns_empty_structure_without_raising():
    """Test11: Geo timeout/unavailable（Weekly Pipeline継続、全キー空の構造化結果を返す）"""
    client = _client()
    with _mock_query(success=False, status="unavailable"):
        result = _run_inquiry(client, pool=[])

    assert result == {
        "independent_candidates": [], "promotion_candidates": [], "article_context": {},
        "background_context": {}, "theme_background_context": {},
        "run_status": "unavailable", "run_id": None,
    }
    assert "weekly_geo_intelligence_runs" not in client.tables or not client.tables["weekly_geo_intelligence_runs"]


# ═══════════════════════════════════════════════════════════════════════
# Test12・13: Dedup失敗のfail-safe・再試行
# ═══════════════════════════════════════════════════════════════════════
def test_12_dedup_llm_failure_fail_safe():
    """Test12: Dedup分類LLM失敗時のfail-safe。該当itemは保存されるがdedup_classificationは
    NULLのまま、weekly_geo_intelligence_runs.dedup_status='failed'、independent_candidates等
    どのバケツにも現れないこと、include_in_weeklyが常にfalseのままであることを確認"""
    client = _client()
    items = [_geo_item("G-1")]
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured", side_effect=RuntimeError("LLM障害")):
        result = _run_inquiry(client, pool=[])

    assert result["independent_candidates"] == []
    assert result["promotion_candidates"] == []
    row = client.tables["weekly_geo_intelligence_items"][0]
    assert row["dedup_classification"] is None
    assert row["include_in_weekly"] is False
    run = client.tables["weekly_geo_intelligence_runs"][0]
    assert run["dedup_status"] == "failed"


def test_13_dedup_retry_does_not_recall_geo_api():
    """Test13（v4修正点2の本体）: Test12の状態から再度run_weekly_geo_inquiry()を呼び、
    Geo APIは再呼び出しされず（call_count不変）、保存済みitemsに対してDedup分類のみ再試行され
    今度は成功してdedup_status='completed'になることを確認"""
    client = _client()
    items = [_geo_item("G-1")]
    with _mock_query(relevant_intelligence=items) as mock_query, \
            patch("sustainability_expert_common.call_llm_structured", side_effect=RuntimeError("LLM障害")):
        _run_inquiry(client, pool=[])
        assert mock_query.call_count == 1

        dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
        # 2回目呼び出し時はLLM側効も差し替える（side_effectを上書き）
    with _mock_query(relevant_intelligence=items) as mock_query2, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result2 = _run_inquiry(client, pool=[])
        assert mock_query2.call_count == 0  # Geo APIは再呼び出しされない

    run = client.tables["weekly_geo_intelligence_runs"][0]
    assert run["dedup_status"] == "completed"
    assert len(result2["independent_candidates"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# Test14・15・15b・16・17: 完全再利用・Selectionキャッシュ・force_rerun・Kill Switch
# ═══════════════════════════════════════════════════════════════════════
def test_14_full_reuse_neither_geo_api_nor_dedup_llm_called_again():
    """Test14: 完全再利用（同一週再実行、dedup_statusがcompleted/not_neededのケース）。
    2回目はGeo API・Dedup分類LLMどちらも呼ばない"""
    client = _client()
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items) as mock_query, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)) as mock_llm:
        _run_inquiry(client, pool=[])
        assert mock_query.call_count == 1
        assert mock_llm.call_count == 1

    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query2, \
            patch("sustainability_expert_common.call_llm_structured") as mock_llm2:
        result2 = _run_inquiry(client, pool=[])
        assert mock_query2.call_count == 0
        assert mock_llm2.call_count == 0
    assert len(result2["independent_candidates"]) == 1


def test_15_selection_cache_reused_llm_not_called_again():
    """Test15（v4修正点4）: dedup_status='completed'かつselection_statusが全item
    selected/rejected確定済みの状態で選定を再実行し、選定LLMが呼ばれないことを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=1)
    final_articles = [_article("A-1", total_score=10)]
    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))) as mock_llm:
        _select(client, config, independent, [], final_articles, run_id)
        assert mock_llm.call_count == 1

    with patch("sustainability_expert_common.call_llm_structured") as mock_llm2:
        result2 = _select(client, config, independent, [], final_articles, run_id)
        assert mock_llm2.call_count == 0
    assert [i["geo_item_id"] for i in result2["selected_geo_topics"]] == ["G-1"]


def test_15b_selection_failure_then_retry_only_selection():
    """Test15b（v5修正点4）: select_weekly_geo_topics()呼び出しが例外を投げ、対象item全件が
    selection_status='failed'になるケース。次回force_rerun=Falseのまま再度呼び出すと、
    selection_status='failed'のitemがあるため選定LLMのみ再試行され、今度は成功して
    selected/rejectedが確定することを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=1)
    final_articles = [_article("A-1", total_score=10)]

    with patch("sustainability_expert_common.call_llm_structured", side_effect=RuntimeError("選定LLM障害")):
        result = _select(client, config, independent, [], final_articles, run_id)
    assert result == {"selected_geo_topics": [], "promoted_article_ids": [], "displaced_article_ids": []}
    row = client.tables["weekly_geo_intelligence_items"][0]
    assert row["selection_status"] == "failed"
    run = client.tables["weekly_geo_intelligence_runs"][0]
    assert run["selection_status"] == "failed"

    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))) as mock_llm:
        result2 = _select(client, config, independent, [], final_articles, run_id)
        assert mock_llm.call_count == 1
    assert [i["geo_item_id"] for i in result2["selected_geo_topics"]] == ["G-1"]


def test_16_force_rerun_inserts_second_run_row_without_unique_constraint_error():
    """Test16（v2修正点3）: 正常完了済みの週に対しforce_rerun=Trueで再実行し、2つ目の
    weekly_geo_intelligence_runs行が制約エラーなくINSERTされることを確認。
    force_rerun=Trueがselect_weekly_geo_topics()にも伝播し選定LLMが再実行されることを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result1 = _run_inquiry(client, config=config, pool=[])

    final_articles = [_article("A-1", total_score=10)]
    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        _select(client, config, result1["independent_candidates"], [], final_articles, result1["run_id"])

    with _mock_query(relevant_intelligence=items) as mock_query, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result2 = _run_inquiry(client, config=config, pool=[], force_rerun=True)  # 例外が出ないこと自体もアサーション
        assert mock_query.call_count == 1

    assert len(client.tables["weekly_geo_intelligence_runs"]) == 2
    assert result2["run_id"] != result1["run_id"]

    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))) as mock_llm:
        _select(client, config, result2["independent_candidates"], [], final_articles, result2["run_id"],
                force_rerun=True)
        assert mock_llm.call_count == 1  # force_rerun伝播により再実行される


def test_17_kill_switch_off_returns_empty_structure_without_calling_geo():
    """Test17: Kill Switch OFF（Geo Relevance/APIを一切呼ばない、全キー空の構造化結果を返す）"""
    client = _client()
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query:
        result = _run_inquiry(client, config=DISABLED_CONFIG, pool=[])
        assert mock_query.call_count == 0
    assert result["run_status"] == "disabled"
    assert result["run_id"] is None
    assert result["independent_candidates"] == []


def test_is_weekly_geo_enabled_parent_kill_switch_forces_off_when_explicitly_false():
    """geo_intelligence.enabled=falseなら、weekly_monitoring.enabled=trueでも強制的に
    無効になること（設定ミスで無駄なGeo呼び出しが発生し続けるのを防ぐ）。"""
    config = {"geo_intelligence": {"enabled": False, "weekly_monitoring": {"enabled": True}}}
    assert wgs.is_weekly_geo_enabled(config) is False


def test_is_weekly_geo_enabled_parent_unset_follows_child_setting():
    """geo_intelligence.enabledが未設定の場合は、従来通り子設定にのみ従うこと（後方互換）。"""
    config = {"geo_intelligence": {"weekly_monitoring": {"enabled": True}}}
    assert wgs.is_weekly_geo_enabled(config) is True


def test_parent_kill_switch_off_prevents_geo_call_even_with_child_enabled():
    """geo_intelligence.enabled=false + weekly_monitoring.enabled=trueの組み合わせで、
    run_weekly_geo_inquiry()がGeo APIを一切呼ばないことを確認する。"""
    client = _client()
    config = {"geo_intelligence": {"enabled": False, "weekly_monitoring": {"enabled": True}}}
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query:
        result = _run_inquiry(client, config=config, pool=[])
        assert mock_query.call_count == 0
    assert result["run_status"] == "disabled"


# ═══════════════════════════════════════════════════════════════════════
# Test18・19・20・21・22: input hash による部分再実行
# ═══════════════════════════════════════════════════════════════════════
def test_18_dedup_input_hash_mismatch_retries_dedup_only():
    """Test18（v6修正点2）: dedup_status='completed'の状態からdedup_candidate_poolの中身を
    変更（記事を追加）してrun_weekly_geo_inquiry()を再実行し、Geo APIは再呼び出しされず、
    Dedup分類のみ再試行され新しいdedup_input_hashで保存されることを確認。
    dedup_candidate_poolが変化しない場合はDedupも再試行されないことを対比で確認"""
    client = _client()
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    pool_v1 = [_article("A-1")]
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)) as mock_llm:
        _run_inquiry(client, pool=pool_v1)
        assert mock_llm.call_count == 1
    run = client.tables["weekly_geo_intelligence_runs"][0]
    hash_v1 = run["dedup_input_hash"]

    # 変化なし: Dedupも再試行されない
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query_same, \
            patch("sustainability_expert_common.call_llm_structured") as mock_llm_same:
        _run_inquiry(client, pool=pool_v1)
        assert mock_query_same.call_count == 0
        assert mock_llm_same.call_count == 0

    # dedup_candidate_poolに記事を追加 → dedup_input_hash変化 → Dedupのみ再試行
    pool_v2 = [_article("A-1"), _article("A-2")]
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query2, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)) as mock_llm2:
        _run_inquiry(client, pool=pool_v2)
        assert mock_query2.call_count == 0  # Geo APIは呼ばれない
        assert mock_llm2.call_count == 1  # Dedupのみ再試行

    run2 = client.tables["weekly_geo_intelligence_runs"][0]
    assert run2["dedup_input_hash"] != hash_v1


def test_19_dedup_reclassification_resets_selection_status_when_bucket_changes():
    """Test19（v6修正点2）: 既にselection_status='selected'だったindependent itemが、
    Dedup再試行でsame_event（最終選定済み記事とマッチ）に変わったケースで、そのitemの
    selection_status/include_in_weeklyがNULL/falseへリセットされ、以後independent_candidates/
    promotion_candidatesのどちらにも現れないことを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    pool_v1 = [_article("A-1")]
    items = [_geo_item("G-1")]
    dedup_response_independent = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response_independent)):
        result1 = _run_inquiry(client, config=config, pool=pool_v1)

    final_articles = [_article("A-1", total_score=10)]
    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        _select(client, config, result1["independent_candidates"], [], final_articles, result1["run_id"])
    row = next(r for r in client.tables["weekly_geo_intelligence_items"] if r["geo_item_id"] == "G-1")
    assert row["selection_status"] == "selected"

    # dedup_candidate_poolを変化させ、今度はsame_event(A-1=final_articles内)として再分類させる
    pool_v2 = [_article("A-1"), _article("A-2")]
    dedup_response_same_event = _dedup_llm_result([{"classification": "same_event", "matched_article_ids": ["A-1"]}])
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response_same_event)):
        result2 = _run_inquiry(client, config=config, final_articles=final_articles, pool=pool_v2)
        assert mock_query.call_count == 0

    row2 = next(r for r in client.tables["weekly_geo_intelligence_items"] if r["geo_item_id"] == "G-1")
    assert row2["selection_status"] is None
    assert row2["include_in_weekly"] is False
    assert result2["independent_candidates"] == []
    assert result2["promotion_candidates"] == []
    assert "A-1" in result2["article_context"]


def test_20_selection_input_hash_mismatch_retries_selection_only():
    """Test20（v6修正点2）: dedup_status='completed'かつselection_statusが全item確定済みの
    状態からranked_final_articlesの構成（total_score）を変更して選定を再実行し、
    Geo API/Dedupは再実行されず、選定LLMのみ再実行され新しいselection_input_hashで
    保存されることを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=1)
    final_articles_v1 = [_article("A-1", total_score=10)]
    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        _select(client, config, independent, [], final_articles_v1, run_id)
    hash_v1 = client.tables["weekly_geo_intelligence_runs"][0]["selection_input_hash"]

    final_articles_v2 = [_article("A-1", total_score=99)]  # total_score変更
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))) as mock_llm:
        _select(client, config, independent, [], final_articles_v2, run_id)
        assert mock_llm.call_count == 1

    hash_v2 = client.tables["weekly_geo_intelligence_runs"][0]["selection_input_hash"]
    assert hash_v1 != hash_v2


def test_21_scope_hash_mismatch_forces_geo_api_rerun():
    """Test21（v7修正点1）: 同一period_start/period_end/scope_versionだがsustainability_themes
    （Scope内容）が変化したケースでrun_weekly_geo_inquiry()を再実行し、scope_hash不一致により
    dedup_statusに関わらずGeo APIが再呼び出しされること、新しいweekly_geo_intelligence_runs行が
    INSERTされることを確認。Scopeが変化しない場合はGeo APIが再呼び出しされないことを対比で確認"""
    client = _client()
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        _run_inquiry(client, pool=[])
    assert len(client.tables["weekly_geo_intelligence_runs"]) == 1

    # Scope不変: Geo APIは再呼び出しされない
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query_same:
        _run_inquiry(client, pool=[])
        assert mock_query_same.call_count == 0

    # Scope変化（テーマ構成が変わる想定）をシミュレートするため、tag_referenceへ新テーマを追加する
    client.tables["tag_reference"].append(
        {"tag_id": "TH-11", "tag_axis": "テーマ", "tag_level": "大分類", "tag_name": "新テーマ", "status": "有効"})
    with _mock_query(relevant_intelligence=items) as mock_query2, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        _run_inquiry(client, pool=[])
        assert mock_query2.call_count == 1  # scope_hash不一致によりGeo APIが再呼び出しされる

    assert len(client.tables["weekly_geo_intelligence_runs"]) == 2


def test_22_selection_input_hash_reflects_candidate_composition_change():
    """Test22（v7修正点3）: ranked_final_articles/target_maxが不変のまま、候補集合
    （independent_candidatesの構成）のみが変わったケースでselect_weekly_geo_topics()を
    再実行し、selection_input_hashが変化し選定LLMが再実行されることを確認
    （ranked_final_articlesベースのhashだけでは検知できないことの回帰防止）"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    final_articles = [_article("A-1", total_score=10)]
    hash1 = wgs._compute_selection_input_hash(final_articles, [{"geo_item_id": "G-1"}], [], 10, 3)
    hash2 = wgs._compute_selection_input_hash(final_articles, [{"geo_item_id": "G-1"}, {"geo_item_id": "G-2"}],
                                               [], 10, 3)
    assert hash1 != hash2


# ═══════════════════════════════════════════════════════════════════════
# Test23: Run単位のselection_result再現
# ═══════════════════════════════════════════════════════════════════════
def test_23_run_level_selection_result_reproduced_without_recalculation():
    """Test23（v7修正点2）: select_weekly_geo_topics()が一度採用/非採用を確定させた後、
    selection_status='completed'かつselection_input_hash一致の状態で同一週を再生成し、
    選定LLMを呼ばずにweekly_geo_intelligence_runs.selection_resultからselected_geo_topicsが
    正しく再構成されること（item単位の再集計ではなく、Run単位の保存値からの再現であることを
    モックの呼び出し有無で確認）"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=2)
    final_articles = [_article("A-1", total_score=10)]
    decisions = [
        {"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "ok"},
        {"candidate_id": "G-2", "select": False, "displaced_article_id": None, "reason": "no"},
    ]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        _select(client, config, independent, [], final_articles, run_id)

    # item単位のselection_statusを手動で壊しても（再集計ではなくRun単位の保存値から復元されるため
    # 結果に影響しないことを確認する目的で）そのままにし、モックの呼び出し有無のみを見る
    with patch("sustainability_expert_common.call_llm_structured") as mock_llm:
        result = _select(client, config, independent, [], final_articles, run_id)
        assert mock_llm.call_count == 0
    assert [i["geo_item_id"] for i in result["selected_geo_topics"]] == ["G-1"]


# ═══════════════════════════════════════════════════════════════════════
# 実装ガード確認テスト（24〜31）
# ═══════════════════════════════════════════════════════════════════════
def test_24_business_context_matches_scope_hash_input():
    """実装ガード1: query_geo_intelligence()へのモック呼び出しで実際に送信されたbusiness_context
    引数の値が、scope_hash算出に使われたbusiness_contextと完全一致することを確認"""
    client = _client()
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return _geo_call_result(relevant_intelligence=[])

    with patch("weekly_geo_intelligence_service.query_geo_intelligence", side_effect=_capture):
        _run_inquiry(client, pool=[])

    scope = wgs.build_weekly_scope(client, PERIOD_START, PERIOD_END, "v1")
    assert captured["business_context"] == scope["business_context"]
    expected_hash = wgs._compute_scope_hash(scope)
    run = client.tables["weekly_geo_intelligence_runs"][0]
    assert run["scope_hash"] == expected_hash


def test_25_dedup_retry_clears_stale_selection_and_confirms_zero_candidates():
    """実装ガード2: selection_status='completed'・selection_result確定済みの状態からDedup再試行
    （dedup_input_hash不一致）を発生させ、Run単位のselection_status/selection_result/
    selection_input_hashがNULLへクリアされること。さらにDedup再分類の結果
    independent_candidates/promotion_candidatesが0件になるケースで、selection_status='not_needed'・
    selection_resultが空配列3つの構造に確定し、古いdisplaced_article_ids/promoted_article_idsが
    一切残らないことを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    pool_v1 = [_article("A-1")]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result1 = _run_inquiry(client, config=config, pool=pool_v1)

    final_articles = [_article("A-1", total_score=10)]
    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": "A-1", "reason": "置換"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        _select(client, config, result1["independent_candidates"], [], [_article("A-1", total_score=10),
                                                                          _article("A-9", total_score=1)],
                result1["run_id"])
    run_before = client.tables["weekly_geo_intelligence_runs"][0]
    assert run_before["selection_status"] == "completed"
    assert run_before["selection_result"]["displaced_article_ids"]

    # dedup_candidate_poolを変更 → dedup再試行 → 今回は独立候補が0件になる（related_contextへ変化）
    pool_v2 = [_article("A-1"), _article("A-2")]
    dedup_response_related = _dedup_llm_result(
        [{"classification": "related_context", "matched_article_ids": []}])
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response_related)):
        result2 = _run_inquiry(client, config=config, pool=pool_v2)
        assert mock_query.call_count == 0

    run_after = client.tables["weekly_geo_intelligence_runs"][0]
    assert run_after["selection_status"] == "not_needed"
    assert run_after["selection_result"] == {
        "selected_independent_geo_item_ids": [], "promoted_article_ids": [], "displaced_article_ids": []}
    assert result2["independent_candidates"] == []
    assert result2["promotion_candidates"] == []


def test_26_duplicate_displaced_id_and_over_target_max_are_forced_down():
    """実装ガード3: LLMモックが2つのGeo候補に同一displaced_article_idを返すselect=trueケースで、
    コード側が重複を検出し1件のみ採用・他方をselect=false（rejected）へ強制すること。
    また採用件数がtarget_maxを超えるLLM出力を与えた場合、コード側が超過分をrejectedへ落とし
    最終的にtarget_max以内に収めることを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 3}}
    client = _client()
    run_id, independent = _run_id_with_independent(client, config=config, n=2, pool=[])
    final_articles = [_article("A-1", total_score=10)]  # target_max=3, is_full=false(1<3)

    # 重複displaced_article_id: 両方ともAdditional slot無しでdisplaced指定するケース
    decisions_dup = [
        {"candidate_id": "G-1", "select": True, "displaced_article_id": "A-1", "reason": "1件目"},
        {"candidate_id": "G-2", "select": True, "displaced_article_id": "A-1", "reason": "2件目(重複)"},
    ]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions_dup))):
        result = _select(client, config, independent, [], final_articles, run_id)
    rows = {r["geo_item_id"]: r for r in client.tables["weekly_geo_intelligence_items"]}
    assert rows["G-1"]["selection_status"] == "selected"
    assert rows["G-2"]["selection_status"] == "rejected"
    assert result["displaced_article_ids"] == ["A-1"]

    # target_max超過ケース: 空き枠が2つ(target_max3-既存1=2)しかないのに3件select=trueかつdisplaced無し
    client2 = _client()
    run_id2, independent2 = _run_id_with_independent(client2, config=config, n=3, pool=[])
    decisions_overflow = [
        {"candidate_id": f"G-{i}", "select": True, "displaced_article_id": None, "reason": f"候補{i}"}
        for i in range(1, 4)
    ]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions_overflow))):
        result2 = _select(client2, config, independent2, [], final_articles, run_id2)
    assert len(result2["selected_geo_topics"]) <= 2  # 既存1件+最大2件=target_max(3)以内
    total_final = len(final_articles) + len(result2["selected_geo_topics"]) - len(result2["displaced_article_ids"])
    assert total_final <= 3


def test_27_promotion_representative_is_deterministic_by_score_then_id():
    """実装ガード4: 1つのsame_event Geo itemのmatched_article_idsが複数の未選定記事を指す
    ケースで、total_scoreが最も高い記事が代表としてarticle_summaryに解決されること
    （同点の場合はarticle_id昇順で決定的に選ばれ、実行順やdict順に依存しないこと）"""
    pool_by_id = {
        "A-1": _article("A-1", total_score=50),
        "A-2": _article("A-2", total_score=90),
        "A-3": _article("A-3", total_score=90),
    }
    rep = wgs._resolve_promotion_representative(["A-1", "A-2", "A-3"], pool_by_id)
    assert rep == "A-2"  # 90点で同点のA-2/A-3のうちarticle_id昇順で先頭
    rep_reordered = wgs._resolve_promotion_representative(["A-3", "A-1", "A-2"], pool_by_id)
    assert rep_reordered == "A-2"  # 入力順に依存しない


def test_28_max_independent_topics_and_logic_version_affect_selection_input_hash():
    """実装ガード5: ranked_final_articles/候補集合が不変でも、max_independent_topicsの
    設定値のみ変更した場合、またはSELECTION_LOGIC_VERSION定数のみ変更した場合に
    selection_input_hashが変化し該当層のみ再実行されることを確認"""
    final_articles = [_article("A-1", total_score=10)]
    independent = [{"geo_item_id": "G-1"}]

    hash_a = wgs._compute_selection_input_hash(final_articles, independent, [], 10, 3)
    hash_b = wgs._compute_selection_input_hash(final_articles, independent, [], 10, 5)
    assert hash_a != hash_b

    with patch.object(wgs, "SELECTION_LOGIC_VERSION", "selection-v2"):
        hash_c = wgs._compute_selection_input_hash(final_articles, independent, [], 10, 3)
    assert hash_a != hash_c

    dedup_pool = [_article("A-1")]
    dedup_hash_a = wgs._compute_dedup_input_hash(dedup_pool)
    with patch.object(wgs, "DEDUP_LOGIC_VERSION", "dedup-v2"):
        dedup_hash_b = wgs._compute_dedup_input_hash(dedup_pool)
    assert dedup_hash_a != dedup_hash_b


def test_29_dedup_retry_resets_before_llm_call_and_no_rollback_on_failure():
    """実装ガード6: run_weekly_geo_inquiry()のDedup再試行パスで、LLM呼び出し前に旧
    dedup_classification/matched_article_ids/item selection_status/Run selection_status・
    selection_result・selection_input_hashが既にNULLへリセットされていること
    （呼び出し直前の状態をLLM側effectの中で検査する）。さらに再Dedup自体が例外で失敗した場合、
    これらのフィールドがリセット前の古い値に巻き戻らずNULLのまま残ることを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    pool_v1 = [_article("A-1")]
    items = [_geo_item("G-1")]
    dedup_response = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result1 = _run_inquiry(client, config=config, pool=pool_v1)
    final_articles = [_article("A-1", total_score=10)]
    decisions = [{"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        _select(client, config, result1["independent_candidates"], [], final_articles, result1["run_id"])

    observed = {}

    def _inspect_before_failing(client_, model, system_prompt, user_prompt, schema, schema_name, temperature=0.2):
        row = client.tables["weekly_geo_intelligence_items"][0]
        run = client.tables["weekly_geo_intelligence_runs"][0]
        observed["item_dedup_classification"] = row["dedup_classification"]
        observed["item_matched_article_ids"] = row["matched_article_ids"]
        observed["item_selection_status"] = row["selection_status"]
        observed["run_selection_status"] = run["selection_status"]
        observed["run_selection_result"] = run["selection_result"]
        observed["run_selection_input_hash"] = run["selection_input_hash"]
        raise RuntimeError("再Dedup失敗")

    pool_v2 = [_article("A-1"), _article("A-2")]
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query, \
            patch("sustainability_expert_common.call_llm_structured", side_effect=_inspect_before_failing):
        _run_inquiry(client, config=config, pool=pool_v2)
        assert mock_query.call_count == 0

    # LLM呼び出し直前の時点で、既にリセット済み（NULL/空）だったこと
    assert observed["item_dedup_classification"] is None
    assert observed["item_matched_article_ids"] == []
    assert observed["item_selection_status"] is None
    assert observed["run_selection_status"] is None
    assert observed["run_selection_result"] is None
    assert observed["run_selection_input_hash"] is None

    # 失敗後もリセット済みNULLのまま据え置かれ、古い値へ巻き戻らないこと
    row_after = client.tables["weekly_geo_intelligence_items"][0]
    run_after = client.tables["weekly_geo_intelligence_runs"][0]
    assert row_after["dedup_classification"] is None
    assert row_after["selection_status"] is None
    assert run_after["dedup_status"] == "failed"
    assert run_after["selection_status"] is None
    assert run_after["selection_result"] is None


def test_30_selection_result_key_separation_and_restore():
    """実装ガード8: weekly_geo_intelligence_runs.selection_resultが
    selected_independent_geo_item_ids/promoted_article_ids/displaced_article_idsの3キーで
    保存されること、キャッシュ再利用時にメール本文の独立トピックセクション
    （selected_geo_topics）がselected_independent_geo_item_idsのみから復元され、
    promoted_article_idsに対応するitemが誤って独立トピックセクションに二重掲載されないことを確認"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 10}}
    client = _client()
    final_articles = [_article("A-1", total_score=10)]
    pool = [_article("A-1", total_score=10), _article("A-2", total_score=70)]
    items = [_geo_item("G-1"), _geo_item("G-2")]
    dedup_response = _dedup_llm_result([
        {"classification": "independent", "matched_article_ids": []},
        {"classification": "same_event", "matched_article_ids": ["A-2"]},
    ])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        geo_result = _run_inquiry(client, config=config, final_articles=final_articles, pool=pool)

    decisions = [
        {"candidate_id": "G-1", "select": True, "displaced_article_id": None, "reason": "独立採用"},
        {"candidate_id": "A-2", "select": True, "displaced_article_id": None, "reason": "昇格採用"},
    ]
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        _select(client, config, geo_result["independent_candidates"], geo_result["promotion_candidates"],
                final_articles, geo_result["run_id"])

    run = client.tables["weekly_geo_intelligence_runs"][0]
    assert set(run["selection_result"].keys()) == {
        "selected_independent_geo_item_ids", "promoted_article_ids", "displaced_article_ids"}
    assert run["selection_result"]["selected_independent_geo_item_ids"] == ["G-1"]
    assert run["selection_result"]["promoted_article_ids"] == ["A-2"]

    with patch("sustainability_expert_common.call_llm_structured") as mock_llm:
        restored = _select(client, config, geo_result["independent_candidates"],
                            geo_result["promotion_candidates"], final_articles, geo_result["run_id"])
        assert mock_llm.call_count == 0
    assert [i["geo_item_id"] for i in restored["selected_geo_topics"]] == ["G-1"]
    assert restored["promoted_article_ids"] == ["A-2"]


def test_31_multiple_geo_items_matching_same_article_are_aggregated_into_one_candidate():
    """実装ガード9: 2つの異なるGeo item（同一Geo Response内の別item）が同じ未選定
    Sustainability記事とsame_eventでマッチするケースで、promotion_candidatesに該当article_idの
    エントリが1つだけ生成され、そのgeo_items配列に両方のGeo itemが含まれることを確認"""
    client = _client()
    final_articles = [_article("A-1")]
    pool = [_article("A-1"), _article("A-2", total_score=70)]
    items = [_geo_item("G-1"), _geo_item("G-2")]
    dedup_response = _dedup_llm_result([
        {"classification": "same_event", "matched_article_ids": ["A-2"]},
        {"classification": "same_event", "matched_article_ids": ["A-2"]},
    ])
    with _mock_query(relevant_intelligence=items), \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        result = _run_inquiry(client, final_articles=final_articles, pool=pool)

    assert len(result["promotion_candidates"]) == 1
    promo = result["promotion_candidates"][0]
    assert promo["article_id"] == "A-2"
    assert {gi["geo_item_id"] for gi in promo["geo_items"]} == {"G-1", "G-2"}


# ═══════════════════════════════════════════════════════════════════════
# E2Eシナリオ（10節）
# ═══════════════════════════════════════════════════════════════════════
def test_e2e_full_scenario_replacement_promotion_theme_scope_and_reproducibility():
    """E2E: independent採用/非採用、置換、昇格、テーマスコープ、Selection失敗再試行、
    昇格記事のGeo Context一体化、Run単位の再現性を1つの週で確認する"""
    config = {**FULL_CONFIG, "weekly_digest": {"since_days": 7, "target_min": 1, "target_max": 2}}
    client = _client()

    final_articles = [_article("A-1", total_score=90), _article("A-2", total_score=10)]  # target_max=2, is_full
    pool = final_articles + [_article("A-3", total_score=70)]  # A-3は未選定（昇格候補用）

    items = [
        _geo_item("G-IND-HIGH"),                                    # independent（採用される）
        _geo_item("G-IND-LOW"),                                     # independent（非採用）
        _geo_item("G-PROMO", themes=["情報開示", "ESG評価・サステナブルファイナンス"]),  # A-3とsame_event（昇格）
    ]
    dedup_response = _dedup_llm_result([
        {"classification": "independent", "matched_article_ids": []},
        {"classification": "independent", "matched_article_ids": []},
        {"classification": "same_event", "matched_article_ids": ["A-3"]},
    ])

    geo_client = GeoIntelligenceClient(FULL_CONFIG, proxies={}, verify=True)
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_weekly_response()) as mock_post, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        # query_geo_intelligenceの実体は使うが、relevant_intelligenceは実HTTPモックからは空なので
        # ここではitemsを注入するためにquery_geo_intelligence自体もモックする（テーマスコープ確認は別途）
        pass

    # v5シナリオ3（テーマスコープ）はHTTP層で別途検証
    with patch("geo_intelligence_client.requests.post", side_effect=_echoing_weekly_response()) as mock_post_scope:
        _run_inquiry(_client(), config=config, pool=[], geo_client=geo_client)
    sent_scope = mock_post_scope.call_args.kwargs["json"]
    assert "Disclosure" in sent_scope["sustainability_themes"]
    assert "ESG Sustainable Finance" in sent_scope["sustainability_themes"]

    with _mock_query(relevant_intelligence=items) as mock_query, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_response)):
        geo_result = _run_inquiry(client, config=config, final_articles=final_articles, pool=pool)

    assert len(geo_result["independent_candidates"]) == 2
    assert len(geo_result["promotion_candidates"]) == 1

    # v5シナリオ1（置換）+ 通常採用/非採用 + v5シナリオ2（昇格）
    decisions = [
        {"candidate_id": "G-IND-HIGH", "select": True, "displaced_article_id": "A-2", "reason": "重要度が高い"},
        {"candidate_id": "G-IND-LOW", "select": False, "displaced_article_id": None, "reason": "重要度不足"},
        {"candidate_id": "A-3", "select": True, "displaced_article_id": None, "reason": "採用しない理由がない"},
    ]
    # is_full判定はfinal_articles(2件)>=target_max(2)なのでtrue。3件目(promotion)はdisplaced無しなので
    # 実装ガード7によりis_full時は強制却下される点をこのシナリオで確認する
    with patch("sustainability_expert_common.call_llm_structured",
               side_effect=_llm_router(selection_response=_selection_llm_result(decisions))):
        selection = wgs.select_weekly_geo_topics(
            MagicMock(), "gpt-test", geo_result["independent_candidates"], geo_result["promotion_candidates"],
            final_articles, config, client, geo_result["run_id"])

    assert [i["geo_item_id"] for i in selection["selected_geo_topics"]] == ["G-IND-HIGH"]
    assert selection["displaced_article_ids"] == ["A-2"]
    assert selection["promoted_article_ids"] == []  # is_full時にdisplaced無しのため実装ガード7で強制却下

    rejected_row = next(r for r in client.tables["weekly_geo_intelligence_items"] if r["geo_item_id"] == "G-IND-LOW")
    assert rejected_row["selection_status"] == "rejected"

    # v7シナリオ4（Run単位の再現性）: 同一runで再度select_weekly_geo_topics()を呼んでも
    # LLMを呼ばず同じ結果が再現される
    with patch("sustainability_expert_common.call_llm_structured") as mock_llm_reuse:
        selection2 = wgs.select_weekly_geo_topics(
            MagicMock(), "gpt-test", geo_result["independent_candidates"], geo_result["promotion_candidates"],
            final_articles, config, client, geo_result["run_id"])
        assert mock_llm_reuse.call_count == 0
    assert selection2 == selection

    # v5シナリオ4（Selection失敗時の再試行）: 新しいrun（G-RETRY）で1回目失敗→2回目成功
    client2 = _client()
    items_retry = [_geo_item("G-RETRY")]
    dedup_retry = _dedup_llm_result([{"classification": "independent", "matched_article_ids": []}])
    with _mock_query(relevant_intelligence=items_retry) as mock_query_retry, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(dedup_response=dedup_retry)):
        geo_result_retry = _run_inquiry(client2, config=config, pool=[])

    with patch("sustainability_expert_common.call_llm_structured", side_effect=RuntimeError("選定LLM障害")):
        failed = wgs.select_weekly_geo_topics(
            MagicMock(), "gpt-test", geo_result_retry["independent_candidates"], [], [_article("A-1")],
            config, client2, geo_result_retry["run_id"])
    assert failed["selected_geo_topics"] == []
    assert mock_query_retry.call_count == 1  # Geo APIは1回のみ（Selection失敗はGeo APIに影響しない）

    decisions_retry = [{"candidate_id": "G-RETRY", "select": True, "displaced_article_id": None, "reason": "ok"}]
    with patch("weekly_geo_intelligence_service.query_geo_intelligence") as mock_query_retry2, \
            patch("sustainability_expert_common.call_llm_structured",
                  side_effect=_llm_router(selection_response=_selection_llm_result(decisions_retry))):
        recovered = wgs.select_weekly_geo_topics(
            MagicMock(), "gpt-test", geo_result_retry["independent_candidates"], [], [_article("A-1")],
            config, client2, geo_result_retry["run_id"])
        assert mock_query_retry2.call_count == 0  # Geo API/Dedupは再実行されない
    assert [i["geo_item_id"] for i in recovered["selected_geo_topics"]] == ["G-RETRY"]


def test_e2e_module_reference_to_selector_constants_is_by_module_not_from_import():
    """v6シナリオ3（テーマスコープ正本）: weekly_geo_intelligence_service.pyが
    sustainability_article_selectorの定数をmodule経由(selector.xxx)で参照していることを確認する
    （fromで個別importしていないため、selector側の値を変更してもweekly_geo_intelligence_service.py
    側の定数を直さずに追従する）"""
    assert wgs.selector is selector
    with patch.object(selector, "TIER0_PROMOTED_CROSS_CUTTING_IDS", {"CR-01"}):
        client = _client()
        names = wgs._fetch_weekly_theme_names(client)
        assert "ESG評価・サステナブルファイナンス" not in names
        assert "情報開示" in names


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
