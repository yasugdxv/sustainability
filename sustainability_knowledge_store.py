"""
当社サスティナビリティ知識ベース: 検索・投入モジュール

`knowledge/sustainability_expert/knowledge_documents.jsonl` を初期データとして、
当社サスティナビリティの公式方針・目標・テーマ文脈をキーワード検索/ベクトル検索できる
ようにする。Azure AI Searchが使えない開発環境では、同じインターフェースで動く
ローカル簡易実装（`LocalJsonlKnowledgeStore`）にフォールバックする。

投入(CLI):
    python sustainability_knowledge_store.py ingest              # config.jsonの設定に従って投入
    python sustainability_knowledge_store.py ingest --mode local # ローカル簡易実装を強制
    python sustainability_knowledge_store.py search "水ストレス"  # 動作確認用の検索

同じcontent_hashの文書は再投入しない（変更された文書だけ更新する）。
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sustainability_expert_common as common  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CACHE_DIR = Path(__file__).parent / "cache"
LOCAL_INDEX_CACHE_PATH = CACHE_DIR / "sustainability_knowledge_index.json"
DEFAULT_JSONL_PATH = common.KNOWLEDGE_DIR / "knowledge_documents.jsonl"

# 必須メタデータ（仕様書に記載の最低限のフィールド）
REQUIRED_FIELDS = (
    "document_id", "title", "content", "source_url", "theme_ids",
    "document_type", "section_type", "authority_level", "retrieved_at", "content_hash",
)


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _build_authority_map(expert_base: dict) -> dict:
    """suntory_sustainability_expert_base.json の source_precedence から
    document_type(doc_type) -> authority_level(rank) の対応表を作る"""
    mapping = {}
    for entry in expert_base.get("source_precedence", []):
        for doc_type in entry.get("types", []):
            mapping[doc_type] = entry["rank"]
    return mapping


def load_and_normalize_documents(jsonl_path: Path = None) -> list:
    """knowledge_documents.jsonl を読み込み、共通メタデータ形式に正規化する"""
    jsonl_path = jsonl_path or DEFAULT_JSONL_PATH
    expert_base = common.load_expert_base()
    authority_map = _build_authority_map(expert_base)

    docs = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            doc_type = raw.get("doc_type") or raw.get("document_type") or "external_analysis"
            content = raw.get("content", "")
            docs.append({
                "document_id": raw["id"] if "id" in raw else raw["document_id"],
                "title": raw.get("title", ""),
                "content": content,
                "source_url": raw.get("source_url", ""),
                "theme_ids": raw.get("themes") or raw.get("theme_ids") or [],
                "document_type": doc_type,
                # section_typeは仕様書のjsonlに含まれないため、暫定的にdoc_typeを流用する
                # （将来、見出し単位でP0サイトを再取得する際にsection_typeを正しく分離する）
                "section_type": raw.get("section_type", doc_type),
                "authority_level": authority_map.get(doc_type, 4),
                "retrieved_at": raw.get("retrieved_at", ""),
                "content_hash": _content_hash(content),
            })
    return docs


def _keyword_score(query_tokens: list, doc: dict) -> float:
    haystack = (doc.get("title", "") + " " + doc.get("content", "")).lower()
    score = 0.0
    for tok in query_tokens:
        if not tok:
            continue
        title_hits = doc.get("title", "").lower().count(tok)
        body_hits = haystack.count(tok)
        score += title_hits * 3 + body_hits
    return score


class LocalJsonlKnowledgeStore:
    """Azure AI Search未導入の開発環境向け簡易実装。
    knowledge_documents.jsonl をメモリに読み込み、キーワード一致度で検索する"""

    def __init__(self, config: dict = None, jsonl_path: Path = None):
        self.config = config or {}
        self.jsonl_path = jsonl_path or DEFAULT_JSONL_PATH
        self._docs = None

    def _load(self) -> list:
        if self._docs is None:
            self._docs = load_and_normalize_documents(self.jsonl_path)
        return self._docs

    def ingest(self) -> dict:
        """ローカルモードでは実データはjsonlそのものなので、変更検知の結果を
        cache/sustainability_knowledge_index.json に記録して差分をレポートする"""
        docs = self._load()
        CACHE_DIR.mkdir(exist_ok=True)
        previous = {}
        if LOCAL_INDEX_CACHE_PATH.exists():
            previous = json.loads(LOCAL_INDEX_CACHE_PATH.read_text(encoding="utf-8"))

        new_count = updated_count = unchanged_count = 0
        current = {}
        for doc in docs:
            doc_id = doc["document_id"]
            current[doc_id] = doc["content_hash"]
            if doc_id not in previous:
                new_count += 1
            elif previous[doc_id] != doc["content_hash"]:
                updated_count += 1
            else:
                unchanged_count += 1

        LOCAL_INDEX_CACHE_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "mode": "local", "total": len(docs),
            "new": new_count, "updated": updated_count, "unchanged": unchanged_count,
        }

    def search(self, query: str, themes: list = None, document_type: str = None,
               authority_level: int = None, source_url: str = None, top_k: int = 8) -> list:
        docs = self._load()

        def _match_filters(d):
            if themes and not (set(d["theme_ids"]) & set(themes) or "all" in d["theme_ids"]):
                return False
            if document_type and d["document_type"] != document_type:
                return False
            if authority_level and d["authority_level"] != authority_level:
                return False
            if source_url and d["source_url"] != source_url:
                return False
            return True

        candidates = [d for d in docs if _match_filters(d)]
        query_tokens = common.tokenize(query)
        scored = [(_keyword_score(query_tokens, d), d) for d in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        # スコアが1件も無い場合でも、権威レベルの高い(数値が小さい)文書を優先して返す
        if all(s == 0 for s, _ in scored):
            scored.sort(key=lambda x: x[1]["authority_level"])
        return [d for _, d in scored[:top_k]]


class AzureAISearchKnowledgeStore:
    """本番向け: Azure AI Search（キーワード検索+ベクトル検索のハイブリッド）実装。
    azure-search-documents SDKを遅延importする（ローカル開発では未インストールでも良い）"""

    def __init__(self, config: dict):
        self.config = config
        cfg = (config or {}).get("sustainability_expert", {}).get("knowledge_search", {})
        az_search_cfg = cfg.get("azure_ai_search", {})
        self.endpoint = az_search_cfg.get("endpoint", "")
        self.api_key = az_search_cfg.get("api_key", "")
        self.index_name = az_search_cfg.get("index_name", "suntory-sustainability-expert-v1")
        self.embedding_deployment = cfg.get("embedding_deployment", "")
        if not self.endpoint or not self.api_key:
            raise ValueError(
                "config.json の sustainability_expert.knowledge_search.azure_ai_search に "
                "endpoint / api_key を設定してください")

    def _client(self):
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        return SearchClient(endpoint=self.endpoint, index_name=self.index_name,
                             credential=AzureKeyCredential(self.api_key))

    def ensure_index_exists(self) -> bool:
        """azure_ai_search_index_fields.json の定義からインデックスを作成する（無ければ）。
        作成に失敗した場合は呼び出し側でAzureポータルでの手動作成を案内すること"""
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents.indexes import SearchIndexClient
        from azure.search.documents.indexes.models import (
            SearchIndex, SimpleField, SearchableField, SearchFieldDataType,
        )

        index_client = SearchIndexClient(endpoint=self.endpoint,
                                          credential=AzureKeyCredential(self.api_key))
        try:
            index_client.get_index(self.index_name)
            return False  # 既に存在する
        except Exception:
            pass

        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SearchableField(name="title", type=SearchFieldDataType.String),
            SearchableField(name="content", type=SearchFieldDataType.String),
            SimpleField(name="source_url", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="document_type", type=SearchFieldDataType.String,
                        filterable=True, facetable=True),
            SimpleField(name="theme_ids", type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                        filterable=True, facetable=True),
            SimpleField(name="section_type", type=SearchFieldDataType.String,
                        filterable=True, facetable=True),
            SimpleField(name="authority_level", type=SearchFieldDataType.Int32,
                        filterable=True, sortable=True),
            SimpleField(name="retrieved_at", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="content_hash", type=SearchFieldDataType.String, filterable=True),
        ]
        index_client.create_index(SearchIndex(name=self.index_name, fields=fields))
        return True

    def _embed(self, text: str) -> list:
        if not self.embedding_deployment:
            return None
        from ai_client import make_openai_client
        client, _ = make_openai_client(self.config)
        resp = client.embeddings.create(model=self.embedding_deployment, input=text[:8000])
        return resp.data[0].embedding

    def ingest(self) -> dict:
        docs = load_and_normalize_documents()
        client = self._client()

        existing_hashes = {}
        try:
            for r in client.search(search_text="*", select=["id", "content_hash"], top=1000):
                existing_hashes[r["id"]] = r.get("content_hash")
        except Exception:
            pass  # インデックス未作成 or 空。全件新規投入として扱う

        to_upload = []
        new_count = updated_count = unchanged_count = 0
        for doc in docs:
            doc_id = doc["document_id"]
            if existing_hashes.get(doc_id) == doc["content_hash"]:
                unchanged_count += 1
                continue
            if doc_id not in existing_hashes:
                new_count += 1
            else:
                updated_count += 1

            search_doc = {
                "id": doc_id,
                "title": doc["title"],
                "content": doc["content"],
                "source_url": doc["source_url"],
                "document_type": doc["document_type"],
                "theme_ids": doc["theme_ids"],
                "section_type": doc["section_type"],
                "authority_level": doc["authority_level"],
                "retrieved_at": doc["retrieved_at"],
                "content_hash": doc["content_hash"],
            }
            vector = self._embed(doc["content"])
            if vector:
                search_doc["content_vector"] = vector
            to_upload.append(search_doc)

        if to_upload:
            client.merge_or_upload_documents(documents=to_upload)

        return {"mode": "azure_ai_search", "total": len(docs), "new": new_count,
                "updated": updated_count, "unchanged": unchanged_count}

    def search(self, query: str, themes: list = None, document_type: str = None,
               authority_level: int = None, source_url: str = None, top_k: int = 8) -> list:
        filters = []
        if themes:
            theme_filter = " or ".join(f"theme_ids/any(t: t eq '{t}')" for t in themes)
            filters.append(f"({theme_filter})")
        if document_type:
            filters.append(f"document_type eq '{document_type}'")
        if authority_level:
            filters.append(f"authority_level eq {authority_level}")
        if source_url:
            filters.append(f"source_url eq '{source_url}'")
        filter_str = " and ".join(filters) if filters else None

        client = self._client()
        kwargs = {"search_text": query, "top": top_k, "filter": filter_str}
        vector = self._embed(query)
        if vector:
            from azure.search.documents.models import VectorizedQuery
            kwargs["vector_queries"] = [VectorizedQuery(
                vector=vector, k_nearest_neighbors=top_k, fields="content_vector")]

        results = client.search(**kwargs)
        return [{
            "document_id": r["id"], "title": r.get("title", ""), "content": r.get("content", ""),
            "source_url": r.get("source_url", ""), "theme_ids": r.get("theme_ids", []),
            "document_type": r.get("document_type", ""), "section_type": r.get("section_type", ""),
            "authority_level": r.get("authority_level"), "retrieved_at": r.get("retrieved_at", ""),
            "content_hash": r.get("content_hash", ""),
        } for r in results]


def get_knowledge_store(config: dict):
    """config.json の sustainability_expert.knowledge_search.mode に従い、
    ローカル簡易実装 or Azure AI Search実装を返す"""
    cfg = (config or {}).get("sustainability_expert", {}).get("knowledge_search", {})
    mode = cfg.get("mode", "local")
    if mode == "azure_ai_search":
        return AzureAISearchKnowledgeStore(config)
    return LocalJsonlKnowledgeStore(config)


def main():
    from article_crawler import load_config

    parser = argparse.ArgumentParser(description="サスティナビリティ知識ベースの投入・検索")
    parser.add_argument("command", choices=["ingest", "search"])
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--mode", choices=["local", "azure_ai_search"], default=None)
    args = parser.parse_args()

    config = load_config()
    if args.mode:
        config.setdefault("sustainability_expert", {}).setdefault("knowledge_search", {})["mode"] = args.mode
    store = get_knowledge_store(config)

    if args.command == "ingest":
        result = store.ingest()
        print(f"投入完了 [{result['mode']}] 合計{result['total']}件 "
              f"（新規{result['new']}件 更新{result['updated']}件 変更なし{result['unchanged']}件）")
    else:
        docs = store.search(args.query, top_k=8)
        print(f"検索結果: {len(docs)}件")
        for d in docs:
            print(f"  [{d['document_id']}] {d['title']}（authority={d['authority_level']}, "
                  f"themes={d['theme_ids']}）\n    {d['source_url']}")


if __name__ == "__main__":
    main()
