import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sustainability_knowledge_store as sks  # noqa: E402

FIXTURE_DOCS = [
    {"id": "KB-WATER-001", "title": "水の判断文脈", "doc_type": "theme_context",
     "themes": ["water"], "content": "水ストレス地域における取水規制と水源涵養が論点になる。",
     "source_url": "https://example.com/water", "retrieved_at": "2026-07-17"},
    {"id": "KB-TARGET-001", "title": "中長期目標の判断軸", "doc_type": "targets",
     "themes": ["water", "climate"], "content": "2030年の水使用量削減目標との関係を確認する。",
     "source_url": "https://example.com/targets", "retrieved_at": "2026-07-17"},
    {"id": "KB-CULTURE-001", "title": "生活文化の判断文脈", "doc_type": "theme_context",
     "themes": ["living_culture"], "content": "文化支援やコミュニティ共創の話題。",
     "source_url": "https://example.com/culture", "retrieved_at": "2026-07-17"},
]


def _write_fixture_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "knowledge_documents.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for doc in FIXTURE_DOCS:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return path


def test_load_and_normalize_documents_has_required_fields(tmp_path):
    """知識文書の読み込み: 最低限のメタデータが揃っていること"""
    path = _write_fixture_jsonl(tmp_path)
    docs = sks.load_and_normalize_documents(path)
    assert len(docs) == 3
    for doc in docs:
        for field in sks.REQUIRED_FIELDS:
            assert field in doc, f"{field} が欠けている"
    water_doc = next(d for d in docs if d["document_id"] == "KB-WATER-001")
    assert water_doc["theme_ids"] == ["water"]
    assert water_doc["document_type"] == "theme_context"
    assert water_doc["authority_level"] == 2  # source_precedenceのtheme_context=rank2


def test_search_prioritizes_keyword_match(tmp_path):
    """コンテキスト検索: クエリと関連するテーマの文書が上位に来ること"""
    path = _write_fixture_jsonl(tmp_path)
    store = sks.LocalJsonlKnowledgeStore(jsonl_path=path)
    results = store.search("水ストレス 取水規制", top_k=2)
    assert results[0]["document_id"] == "KB-WATER-001"


def test_search_filters_by_theme(tmp_path):
    """テーマでフィルタできること"""
    path = _write_fixture_jsonl(tmp_path)
    store = sks.LocalJsonlKnowledgeStore(jsonl_path=path)
    results = store.search("目標", themes=["living_culture"], top_k=5)
    assert all("living_culture" in d["theme_ids"] for d in results)
    assert len(results) == 1


def test_ingest_detects_new_and_unchanged(tmp_path, monkeypatch):
    """同じcontent_hashの文書は再投入対象にならず、初回は新規として検知されること"""
    path = _write_fixture_jsonl(tmp_path)
    monkeypatch.setattr(sks, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(sks, "LOCAL_INDEX_CACHE_PATH", tmp_path / "cache" / "index.json")

    store = sks.LocalJsonlKnowledgeStore(jsonl_path=path)
    first = store.ingest()
    assert first["new"] == 3
    assert first["updated"] == 0
    assert first["unchanged"] == 0

    second = store.ingest()
    assert second["new"] == 0
    assert second["unchanged"] == 3


def test_ingest_detects_updated_content(tmp_path, monkeypatch):
    """本文が変更された文書だけ updated として検知されること"""
    path = _write_fixture_jsonl(tmp_path)
    monkeypatch.setattr(sks, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(sks, "LOCAL_INDEX_CACHE_PATH", tmp_path / "cache" / "index.json")

    store = sks.LocalJsonlKnowledgeStore(jsonl_path=path)
    store.ingest()

    updated_docs = [dict(d) for d in FIXTURE_DOCS]
    updated_docs[0]["content"] = "内容が更新された水ストレスの説明。"
    with open(path, "w", encoding="utf-8") as f:
        for doc in updated_docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    store2 = sks.LocalJsonlKnowledgeStore(jsonl_path=path)
    result = store2.ingest()
    assert result["updated"] == 1
    assert result["unchanged"] == 2
