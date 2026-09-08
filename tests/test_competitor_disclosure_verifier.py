import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import competitor_disclosure_verifier as verifier  # noqa: E402
from tests._fakes import FakeSupabaseClient  # noqa: E402


def _mock_search_response(explanation: str, verdict: dict, evidence_urls: list):
    """responses API（web_search_preview使用）のモック。
    output_textは説明文+JSONコードブロック、outputはannotations(url_citation)を含む"""
    json_block = json.dumps(verdict, ensure_ascii=False)
    output_text = f"{explanation}\n```json\n{json_block}\n```"
    annotations = [SimpleNamespace(type="url_citation", url=url, title=f"title-{i}")
                   for i, url in enumerate(evidence_urls)]
    content_item = SimpleNamespace(annotations=annotations)
    message_item = SimpleNamespace(type="message", content=[content_item])
    return SimpleNamespace(output_text=output_text, output=[message_item])


KNOWN_DOMAINS = {"pepsico.com"}


def test_fetch_known_domains_returns_domains_of_registered_sources():
    client = FakeSupabaseClient({
        "competitor_sources": [
            {"company_id": "c-1", "source_url": "https://pepsico.com/sustainability"},
            {"company_id": "c-1", "source_url": "https://www.pepsico.com/investors"},
            {"company_id": "c-2", "source_url": "https://diageo.com/esg"},
        ],
    })
    domains = verifier.fetch_known_domains(client, "c-1")
    assert domains == {"pepsico.com", "www.pepsico.com"}


def test_fetch_known_domains_empty_when_no_sources():
    client = FakeSupabaseClient({"competitor_sources": []})
    assert verifier.fetch_known_domains(client, "c-1") == set()


def test_judge_via_search_keeps_verified_when_evidence_domain_is_official():
    azure_client = MagicMock()
    azure_client.responses.create.return_value = _mock_search_response(
        "公式サイトで確認できました。",
        {"verification_status": "VERIFIED", "verification_reason": "公式サイトで確認",
         "verification_evidence": "official announcement", "primary_source_title": "Official page",
         "primary_source_document_type": "公式サステナビリティWebページ"},
        evidence_urls=["https://pepsico.com/news/target-update"],
    )
    result = verifier.judge_via_search(azure_client, "gpt-4o", "PepsiCo", "目標撤回", KNOWN_DOMAINS)
    assert result["verification_status"] == "VERIFIED"
    assert result["primary_source_domain"] == "pepsico.com"


def test_judge_via_search_downgrades_when_evidence_domain_not_official():
    azure_client = MagicMock()
    azure_client.responses.create.return_value = _mock_search_response(
        "業界メディアの記事で言及がありました。",
        {"verification_status": "VERIFIED", "verification_reason": "メディアが報じている",
         "verification_evidence": "news article", "primary_source_title": "News article",
         "primary_source_document_type": "公式ニュースリリース"},
        evidence_urls=["https://some-news-site.example.com/article"],
    )
    result = verifier.judge_via_search(azure_client, "gpt-4o", "PepsiCo", "目標撤回", KNOWN_DOMAINS)
    assert result["verification_status"] == "UNVERIFIED"
    assert "自動格下げ" in result["verification_reason"]


def test_judge_via_search_unverified_when_no_evidence_found():
    azure_client = MagicMock()
    azure_client.responses.create.return_value = _mock_search_response(
        "公式情報は見つかりませんでした。",
        {"verification_status": "UNVERIFIED", "verification_reason": "公式開示が見つからない",
         "verification_evidence": "", "primary_source_title": None,
         "primary_source_document_type": None},
        evidence_urls=[],
    )
    result = verifier.judge_via_search(azure_client, "gpt-4o", "PepsiCo", "目標撤回", KNOWN_DOMAINS)
    assert result["verification_status"] == "UNVERIFIED"
    assert "primary_source_url" not in result


def test_non_meaningful_change_types_excludes_wording_only_and_republish():
    assert "WORDING_ONLY" in verifier.NON_MEANINGFUL_CHANGE_TYPES
    assert "SIMPLE_REPUBLISH" in verifier.NON_MEANINGFUL_CHANGE_TYPES
    assert "SUBSTANTIVE_CHANGE" not in verifier.NON_MEANINGFUL_CHANGE_TYPES
