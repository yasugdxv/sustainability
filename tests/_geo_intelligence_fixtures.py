"""
geo_intelligence_client / geo_intelligence_service のテスト共通フィクスチャ。
（ファイル名を conftest.py にしていないのは、tests/_fakes.py と同じく
 pytestのfixture自動読込の仕組みと混同しないため。使う側が明示的にimportする）
"""
from unittest.mock import MagicMock


def mock_response(status_code=200, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body if json_body is not None else {}
    resp.text = text
    return resp


def sufficient_body(request_id="req-1"):
    return {
        "request_id": request_id,
        "status": "completed",
        "geo_assessment": "台湾情勢は現時点で半導体供給網に直接的な混乱を与えていない。",
        "key_stakeholders": ["TSMC", "台湾政府", "米国商務省"],
        "political_dynamics": "米中間の緊張は継続しているが軍事的エスカレーションの兆候はない。",
        "outlook": "3ヶ月以内に大きな変化は見込まれない。",
        "cross_domain_implications": "半導体調達コストへの短期的影響は限定的。",
        "references": [{"reference_id": "R1", "reference_type": "internal_report",
                         "title": "台湾リスク四半期レビュー", "source": "internal",
                         "date": "2026-07-01", "internal_external": "internal"}],
        "confidence": "high",
        "knowledge_sufficiency": "sufficient",
        "knowledge_gap": None,
        "error": None,
        "completed_at": "2026-08-26T01:00:00+00:00",
    }


def partial_body(request_id="req-1"):
    body = sufficient_body(request_id)
    body.update({
        "status": "partial",
        "confidence": "medium",
        "knowledge_sufficiency": "partial",
        "knowledge_gap": {"exists": True, "gap_id": "GAP-1", "description": "直近1週間の一次情報が不足",
                           "merged_into_existing": False, "missing_categories": ["observed_fact"],
                           "note": "AIはKnowledge不足を推測で補完せず記録した。"},
    })
    return body


def insufficient_body(request_id="req-1"):
    return {
        "request_id": request_id,
        "status": "partial",
        "geo_assessment": None,
        "key_stakeholders": [],
        "political_dynamics": None,
        "outlook": None,
        "cross_domain_implications": None,
        "references": None,
        "confidence": "low",
        "knowledge_sufficiency": "insufficient",
        "knowledge_gap": {"exists": True, "gap_id": "GAP-2", "description": "関連Knowledgeが存在しない",
                           "merged_into_existing": False, "missing_categories": ["foundation", "precedent"],
                           "note": "AIはKnowledge不足を推測で補完せず記録した。"},
        "error": None,
        "completed_at": "2026-08-26T01:00:00+00:00",
    }


TEST_GEO_CONFIG = {
    "geo_intelligence": {
        "enabled": True,
        "base_url": "https://geo.example.com",
        "query_path": "/api/v1/geo-intelligence/query",
        "timeout_seconds": 5,
        "api_key": "sk-test-secret-XYZ",
        "max_retries": 2,
    },
}
