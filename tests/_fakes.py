"""
サスティナビリティ専門家MVPのテスト用共通フェイク。
Supabase(PostgREST)への実アクセスをせず、article_crawler.SupabaseClient と
同じインターフェース（select/insert/update）を持つ簡易フェイクを提供する。
（ファイル名を conftest.py にしていないのは、pytestのfixture自動読込の仕組みと
  混同しないようにするため。使う側が明示的にimportする単なるヘルパーモジュール）
"""


class FakeSupabaseClient:
    """PostgRESTクエリ記法(eq./in.())の最小限だけを解釈するフェイクSupabaseClient"""

    def __init__(self, tables: dict = None):
        self.tables = {k: [dict(row) for row in v] for k, v in (tables or {}).items()}
        self.inserted = {}
        self.updated = []
        self._id_counter = 0

    def _next_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}-{self._id_counter}"

    @staticmethod
    def _stringify(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _matches(self, row: dict, params: dict) -> bool:
        for key, value in params.items():
            if key in ("select", "order", "limit") or not isinstance(value, str):
                continue
            if value.startswith("eq."):
                if self._stringify(row.get(key)) != value[3:]:
                    return False
            elif value.startswith("neq."):
                if self._stringify(row.get(key)) == value[4:]:
                    return False
            elif value.startswith("not.in.("):
                items = value[len("not.in.("):-1].split(",")
                if self._stringify(row.get(key)) in items:
                    return False
            elif value.startswith("in.("):
                items = value[len("in.("):-1].split(",")
                if self._stringify(row.get(key)) not in items:
                    return False
            elif value.startswith("gte."):
                if self._stringify(row.get(key)) < value[4:]:
                    return False
            elif value.startswith("lte."):
                if self._stringify(row.get(key)) > value[4:]:
                    return False
        return True

    def select(self, table: str, params: dict) -> list:
        rows = [r for r in self.tables.get(table, []) if self._matches(r, params)]
        order = params.get("order", "")
        if "created_at" in order and "desc" in order:
            rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)
        limit = params.get("limit")
        if limit:
            rows = rows[: int(limit)]
        return rows

    def insert(self, table: str, rows: list, prefer: str = "return=representation") -> list:
        id_field = {"expert_runs": "run_id", "expert_contents": "content_id",
                    "weekly_email_reports": "report_id",
                    "competitor_target_records": "record_id",
                    "competitor_change_events": "change_event_id",
                    "competitor_initiatives": "initiative_id",
                    "competitor_alerts": "alert_id",
                    "competitor_audit_log": "audit_id",
                    "competitor_daily_alert_digests": "digest_id",
                    "monthly_reports": "report_id",
                    "monthly_report_companies": "monthly_report_company_id",
                    "monthly_report_items": "monthly_report_item_id"}.get(table)
        out = []
        for row in rows:
            r = dict(row)
            if id_field and id_field not in r:
                r[id_field] = self._next_id(id_field)
            r.setdefault("created_at", "2026-07-17T00:00:00+00:00")
            self.tables.setdefault(table, []).append(r)
            self.inserted.setdefault(table, []).append(r)
            out.append(r)
        return out

    def update(self, table: str, params: dict, patch: dict) -> None:
        for row in self.tables.get(table, []):
            if self._matches(row, params):
                row.update(patch)
        self.updated.append((table, dict(params), dict(patch)))


class FakeKnowledgeStore:
    """knowledge_store.search() と同じインターフェースの固定応答フェイク"""

    def __init__(self, docs: list = None):
        self.docs = docs or [
            {"document_id": "KB-TEST-001", "title": "テスト用公式コンテキスト",
             "content": "テスト用の当社公式方針の説明。", "source_url": "https://example.com/policy",
             "theme_ids": ["water"], "document_type": "policy", "section_type": "policy",
             "authority_level": 1, "retrieved_at": "2026-07-17", "content_hash": "dummy"},
        ]
        self.calls = []

    def search(self, query, themes=None, document_type=None, authority_level=None,
               source_url=None, top_k=8):
        self.calls.append(query)
        return self.docs[:top_k]
