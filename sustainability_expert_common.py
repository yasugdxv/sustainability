"""
当社サスティナビリティ専門家MVP: 共通処理モジュール

記事選定(sustainability_article_selector.py)とコンテンツ生成
(sustainability_content_generator.py)の両方から使う共通部品をまとめる。
    - 機能フラグの判定
    - 専門家の知識ベース(base.json)・プロンプト・JSON Schemaの読み込み
    - 事象クラスタ解決アダプター（既存の article_urls.duplicate_of_article_url_id を再利用）
    - Azure OpenAI構造化出力ヘルパー（対応していれば構造化出力、
      できなければ手動JSON抽出+Schema検証+1回だけ修正再実行）
    - expert_runs へのログ保存

既存のAzure OpenAI呼び出し(run.make_openai_client)・Supabase呼び出し
(article_crawler.SupabaseClient)をそのまま再利用し、新しいクライアントは作らない。
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
from dateutil import parser as dateutil_parser

BASE = Path(__file__).parent
KNOWLEDGE_DIR = BASE / "knowledge" / "sustainability_expert"

EXPERT_BASE_PATH = KNOWLEDGE_DIR / "suntory_sustainability_expert_base.json"
SELECTION_PROMPT_PATH = KNOWLEDGE_DIR / "article_selection_prompt.md"
CONTENT_PROMPT_PATH = KNOWLEDGE_DIR / "content_generation_prompt.md"
ASSESSMENT_SCHEMA_PATH = KNOWLEDGE_DIR / "article_assessment_schema.json"

DEFAULT_EXPERT_VERSION = "0.1.0"

# selector/generator両方で使う共通デフォルト（旧: 両ファイルに個別定義されていた）
MAX_ARTICLE_CHARS = 8000
CONTEXT_TOP_K = 8


def tokenize(text: str) -> list:
    """簡易トークナイズ（旧: sustainability_dashboard_core.py / sustainability_knowledge_store.py
    に同一実装が個別定義されていたものを統合）"""
    return [t for t in re.split(r"[\s、。・,.:：「」『』()（）\[\]]+", (text or "").lower()) if t]


# ─── 機能フラグ ───────────────────────────────────────────────────
def is_enabled(config: dict) -> bool:
    """SUSTAINABILITY_EXPERT_ENABLED（config.json優先、環境変数フォールバック）"""
    cfg = (config or {}).get("sustainability_expert", {})
    if "enabled" in cfg:
        return bool(cfg["enabled"])
    env = os.environ.get("SUSTAINABILITY_EXPERT_ENABLED", "")
    return env.strip().lower() in ("1", "true", "yes")


def get_expert_version(config: dict) -> str:
    cfg = (config or {}).get("sustainability_expert", {})
    if cfg.get("expert_version"):
        return cfg["expert_version"]
    base = load_expert_base()
    return base.get("metadata", {}).get("version", DEFAULT_EXPERT_VERSION)


DEFAULT_WEEKLY_PICK_SINCE_DAYS = 7
DEFAULT_WEEKLY_PICK_TARGET_MIN = 15
DEFAULT_WEEKLY_PICK_TARGET_MAX = 20


def get_weekly_pick_range(config: dict) -> tuple:
    """config.json の weekly_digest.target_min/target_max（無ければ既定15/20）を返す。
    importance_rank_definitionsと同じ、チューニング対象の閾値/件数という位置づけ"""
    cfg = (config or {}).get("weekly_digest", {})
    return (
        int(cfg.get("target_min", DEFAULT_WEEKLY_PICK_TARGET_MIN)),
        int(cfg.get("target_max", DEFAULT_WEEKLY_PICK_TARGET_MAX)),
    )


def get_weekly_pick_since_days(config: dict) -> int:
    """config.json の weekly_digest.since_days（無ければ既定7）を返す"""
    cfg = (config or {}).get("weekly_digest", {})
    return int(cfg.get("since_days", DEFAULT_WEEKLY_PICK_SINCE_DAYS))


# ─── 知識ベース・プロンプト・Schema読み込み ─────────────────────────
def load_expert_base() -> dict:
    """suntory_sustainability_expert_base.json（専門家の役割・当社文脈・判断ルール）"""
    return json.loads(EXPERT_BASE_PATH.read_text(encoding="utf-8"))


def load_selection_prompt() -> str:
    return SELECTION_PROMPT_PATH.read_text(encoding="utf-8")


def load_content_prompt() -> str:
    return CONTENT_PROMPT_PATH.read_text(encoding="utf-8")


def load_assessment_schema() -> dict:
    return json.loads(ASSESSMENT_SCHEMA_PATH.read_text(encoding="utf-8"))


# コンテンツ生成用のJSON Schema。
# 仕様パックにはSchemaファイルが同梱されていないため、
# content_generation_framework.required_sections（base.json）に合わせて定義する。
# 「根拠のない情報を補完しない」「数値・日付・義務には根拠URLを紐づける」を守るため、
# evidenceは最低1件必須とする（0件はSchema検証エラー＝レビュー警告として扱う）。
CONTENT_GENERATION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "SustainabilityContentCandidate",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "what_happened", "why_it_matters", "company_watchpoints",
        "affected_themes", "affected_business_areas", "impact_pathways",
        "questions_to_confirm", "monitoring_signals", "evidence", "uncertainties",
    ],
    "properties": {
        "title": {"type": "string"},
        "what_happened": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "company_watchpoints": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "affected_themes": {"type": "array", "items": {"type": "string"}},
        "affected_business_areas": {"type": "array", "items": {"type": "string"}},
        "impact_pathways": {"type": "array", "items": {"type": "string"}},
        "questions_to_confirm": {"type": "array", "items": {"type": "string"}},
        "monitoring_signals": {"type": "array", "items": {"type": "string"}},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "source_url", "source_type"],
                "properties": {
                    "claim": {"type": "string"},
                    "source_url": {"type": "string", "minLength": 1},
                    "source_type": {
                        "type": "string",
                        "enum": ["primary", "official_company", "secondary_analysis"],
                    },
                },
            },
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
}


# ─── プロンプト構築（selector/generator共通） ──────────────────────
def build_system_prompt(prompt_text: str, expert_base: dict) -> str:
    return (
        prompt_text
        + "\n\n# company_context (suntory_sustainability_expert_base.json)\n"
        + json.dumps(expert_base, ensure_ascii=False)
    )


def format_retrieved_context(docs: list) -> str:
    """当社公式知識ベース(sustainability_knowledge_store)の検索結果を整形する。
    selector/generator/dashboard_coreで別々に実装され既に体裁がずれていたものを統一。"""
    if not docs:
        return "（該当する当社公式コンテキストなし）"
    lines = []
    for d in docs:
        lines.append(
            f"- [{d['document_id']}] {d['title']}（document_type={d['document_type']}, "
            f"authority_level={d['authority_level']}, themes={d['theme_ids']}）\n"
            f"  出典: {d['source_url']}\n"
            f"  本文: {d['content'][:500]}"
        )
    return "\n".join(lines)


# ─── 事象クラスタ解決アダプター ───────────────────────────────────
# 専用のクラスタリング基盤は新設せず、既存の article_urls.duplicate_of_article_url_id
# を辿って「同一事象」をまとめる。まだ重複判定が行われていないURLは、自分自身が
# 単独クラスタの代表（root）として扱われる。
def resolve_cluster_root(url_row: dict, all_urls_by_id: dict, max_depth: int = 10) -> str:
    """article_urls 1行から、重複チェーンを辿ってクラスタ代表のarticle_url_idを返す"""
    current = url_row
    seen = set()
    depth = 0
    while current.get("duplicate_of_article_url_id") and depth < max_depth:
        next_id = current["duplicate_of_article_url_id"]
        if next_id in seen or next_id not in all_urls_by_id:
            break
        seen.add(next_id)
        current = all_urls_by_id[next_id]
        depth += 1
    return current["article_url_id"]


def fetch_all_article_urls(client) -> dict:
    """article_urls全件をarticle_url_idキーの辞書で返す。
    get_cluster/list_candidate_cluster_idsはクラスタ解決のたびにこれを毎回フルスキャンしていたため、
    呼び出し側（selector/generatorのmain()）で1回だけ取得してall_urlsとして使い回すことを想定した
    共通関数として切り出した。1回のパイプライン実行中に他プロセスがarticle_urlsへ書き込んでも
    反映されない（実行開始時点のスナップショットになる）点に注意。"""
    return {u["article_url_id"]: u for u in client.select(
        "article_urls", {"select": "article_url_id,article_url,duplicate_of_article_url_id"})}


def get_cluster(client, article_cluster_id: str, all_urls: dict = None) -> dict | None:
    """article_cluster_id（= article_urls.article_url_id を流用した事象クラスタの代表ID）
    から、クラスタに属する記事一式（代表記事+関連記事）を取得する。
    代表記事は、クラスタ自身のURLの現行記事(is_current)を優先し、無ければ
    メンバー記事のうち最新公開のものを代表とする。

    all_urlsを渡さない場合はここでarticle_urls全件を取得する（従来通りの単発呼び出し用）。
    複数クラスタを連続処理する場合は、呼び出し側でfetch_all_article_urls(client)を1回だけ実行し、
    その結果をall_urlsとして毎回渡すことでフルスキャンの重複を避けられる。"""
    if all_urls is None:
        all_urls = fetch_all_article_urls(client)

    member_url_ids = {article_cluster_id}
    changed = True
    while changed:
        changed = False
        for uid, row in all_urls.items():
            if row.get("duplicate_of_article_url_id") in member_url_ids and uid not in member_url_ids:
                member_url_ids.add(uid)
                changed = True

    articles = client.select("articles", {
        "select": "article_id,article_url_id,title,extracted_text,published_at,final_url,fetched_url,crawl_target_id",
        "article_url_id": f"in.({','.join(member_url_ids)})",
        "is_current": "eq.true",
    })
    if not articles:
        return None

    target_ids = {a["crawl_target_id"] for a in articles if a.get("crawl_target_id")}
    targets = {}
    if target_ids:
        targets = {t["crawl_target_id"]: t for t in client.select(
            "crawl_targets", {"select": "crawl_target_id,publisher_name,domain",
                               "crawl_target_id": f"in.({','.join(target_ids)})"})}
    analyses = {a["article_id"]: a for a in client.select("article_analysis", {
        "select": "article_id,primary_source_status,summary_short",
        "article_id": f"in.({','.join(a['article_id'] for a in articles)})",
        "is_current": "eq.true",
    })}
    for a in articles:
        a["_target"] = targets.get(a["crawl_target_id"], {})
        a["_analysis"] = analyses.get(a["article_id"])

    articles.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    representative = next((a for a in articles if a["article_url_id"] == article_cluster_id), articles[0])
    members = [a for a in articles if a["article_id"] != representative["article_id"]]
    return {"article_cluster_id": article_cluster_id, "representative": representative, "members": members}


# ─── タグ分類・記事取得（weekly_email_report / dashboard_core 共通） ────
def _major_category_name(tag: dict, tag_ref_by_id: dict) -> str:
    """小分類タグを大分類名に解決する（大分類ならそのまま）"""
    if tag["tag_level"] == "大分類":
        return tag["tag_name"]
    parent = tag_ref_by_id.get(tag.get("parent_tag_id"))
    return parent["tag_name"] if parent else tag["tag_name"]


def categorize_tags(tag_ids: list, tag_ref_by_id: dict) -> dict:
    """記事のtag_idリストを軸別（テーマ/横断/主体/マテリアリティ接続）に分類する
    （旧: weekly_email_report.py と sustainability_dashboard_core.py に別々に実装されていた）"""
    by_axis: dict = {}
    for tid in tag_ids:
        t = tag_ref_by_id.get(tid)
        if not t:
            continue
        by_axis.setdefault(t["tag_axis"], []).append(t)

    themes = [_major_category_name(t, tag_ref_by_id) for t in by_axis.get("テーマ", [])]
    cross = [_major_category_name(t, tag_ref_by_id) for t in by_axis.get("横断", [])]
    subjects = [_major_category_name(t, tag_ref_by_id) for t in by_axis.get("主体", [])]
    materiality_codes = [t["tag_code"] for t in by_axis.get("マテリアリティ接続", []) if t.get("tag_code")]

    def _dedup(items):
        return list(dict.fromkeys(items))

    return {
        "themes": _dedup(themes) or ["その他"],
        "cross_tags": _dedup(cross),
        "subject_tags": _dedup(subjects),
        "materiality_codes": _dedup(materiality_codes),
    }


def _select_in_chunks(client, table: str, base_params: dict, id_field: str, ids: list,
                       chunk_size: int = 100) -> list:
    """idsを1つの巨大な in.(id1,id2,...) にまとめると、件数が多い場合にURLが長くなりすぎて
    PostgRESTが400 Bad Requestを返すことがある（実際に記事数が700件を超えて発生した）ため、
    chunk_size件ずつに分割してクエリし、結果を連結する"""
    rows = []
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        rows.extend(client.select(table, {**base_params, id_field: f"in.({','.join(chunk)})"}))
    return rows


def fetch_articles_with_tags(client, since_days: int, ranks: tuple = None) -> list:
    """article_analysisから直近since_days日分（公開日基準）の記事一覧をタグ分類付きで抽出する。
    ranksを指定すればそのimportance_levelのみに絞る（未指定なら全ランク対象）。
    差し戻し(analysis_status='差戻し')と、軽量フィルタ除外(analysis_status='フィルタ除外'、
    LLM分析自体を行っていないスタブ行)は常に除外する。
    （旧: weekly_email_report.fetch_ranked_articles と
    sustainability_dashboard_core.fetch_dashboard_articles にほぼ同一のロジックが
    個別実装されていたものを統合）"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    params = {
        "select": "analysis_id,article_id,summary_short,importance_level,importance_reason,"
                  "publication_category,analysis_status",
        "is_current": "eq.true",
        "analysis_status": "not.in.(差戻し,フィルタ除外)",
    }
    if ranks:
        params["importance_level"] = f"in.({','.join(ranks)})"
    analyses = client.select("article_analysis", params)
    if not analyses:
        return []

    article_ids = [a["article_id"] for a in analyses]
    articles = _select_in_chunks(client, "articles", {
        "select": "article_id,article_url_id,title,extracted_text,published_at,fetched_at,"
                  "final_url,fetched_url,crawl_target_id",
        "is_current": "eq.true",
    }, "article_id", article_ids)
    articles_by_id = {a["article_id"]: a for a in articles}

    targets = {t["crawl_target_id"]: t for t in client.select(
        "crawl_targets", {"select": "crawl_target_id,publisher_name"})}

    tag_rows = _select_in_chunks(client, "article_tags", {
        "select": "article_id,tag_id",
    }, "article_id", article_ids)
    tag_ref_by_id = {t["tag_id"]: t for t in client.select(
        "tag_reference", {"select": "tag_id,tag_axis,tag_level,tag_code,tag_name,parent_tag_id"})}
    tags_by_article: dict = {}
    for r in tag_rows:
        tags_by_article.setdefault(r["article_id"], []).append(r["tag_id"])

    result = []
    for a in analyses:
        article = articles_by_id.get(a["article_id"])
        if not article:
            continue
        pub_dt = article.get("published_at")
        if pub_dt:
            try:
                if dateutil_parser.parse(pub_dt) < cutoff:
                    continue
            except Exception:
                pass

        tag_ids = tags_by_article.get(a["article_id"], [])
        categorized = categorize_tags(tag_ids, tag_ref_by_id)

        result.append({
            **a,
            "title": article.get("title") or "(無題)",
            "extracted_text": article.get("extracted_text") or "",
            "url": article.get("final_url") or article.get("fetched_url") or "",
            "published_at": pub_dt,
            "publisher": targets.get(article.get("crawl_target_id"), {}).get("publisher_name", ""),
            **categorized,
        })

    result.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    return result


# ─── ハッシュ ─────────────────────────────────────────────────────
def compute_input_hash(*parts: str) -> str:
    text = "\x1f".join(p or "" for p in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── LLM呼び出し（構造化出力 → 手動JSON+Schema検証+1回だけ修正再実行） ──
class ExpertLLMError(Exception):
    """Schema検証が最終的に失敗した場合、またはLLM呼び出し自体が失敗した場合"""


def _extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    return json.loads(text)


def _usage_dict(resp) -> dict:
    u = getattr(resp, "usage", None)
    if not u:
        return {}
    details = getattr(u, "completion_tokens_details", None)
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
        "reasoning_tokens": (getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
    }


def call_llm_structured(client, model: str, system_prompt: str, user_prompt: str,
                         schema: dict, schema_name: str) -> dict:
    """schemaに準拠したJSONを1個取得する。

    Azure OpenAIの構造化出力(response_format=json_schema)が使える場合はそれを使う。
    使えない場合（古いAPIバージョン等でエラーになる場合）は、通常のJSON生成 →
    サーバー側でSchema検証 → 検証失敗時に最大1回だけ修正再実行、という流れにフォールバックする。
    それでも失敗した場合は ExpertLLMError を送出する（呼び出し側でexpert_runsにエラー記録する）。

    戻り値: {"data": dict, "mode": str, "token_usage": dict, "latency_ms": int}
    """
    started = time.monotonic()

    # ── まず構造化出力を試す ──
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": False},
            },
            temperature=0.2,
        )
        data = json.loads(resp.choices[0].message.content)
        jsonschema.validate(data, schema)
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"data": data, "mode": "structured_output", "token_usage": _usage_dict(resp),
                "latency_ms": latency_ms, "raw_output": data}
    except Exception:
        pass  # 構造化出力が未対応・失敗（Schema検証エラーも含む）→ 手動JSON抽出にフォールバック

    # ── 手動JSON抽出 + Schema検証 + 1回だけ修正再実行 ──
    messages = [
        {"role": "system", "content": system_prompt + "\n\n必ずJSON1個のみを出力すること。説明文・Markdown装飾は不要。"},
        {"role": "user", "content": user_prompt},
    ]
    resp = client.chat.completions.create(model=model, messages=messages, temperature=0.2)
    raw = resp.choices[0].message.content
    try:
        data = _extract_json_object(raw)
        jsonschema.validate(data, schema)
        latency_ms = int((time.monotonic() - started) * 1000)
        return {"data": data, "mode": "manual_json", "token_usage": _usage_dict(resp),
                "latency_ms": latency_ms, "raw_output": data}
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        first_error = str(e)

    # 1回だけ、検証エラー内容を伝えて修正再実行する
    messages.append({"role": "assistant", "content": raw})
    messages.append({
        "role": "user",
        "content": f"前回の出力はJSON Schema検証に失敗しました: {first_error}\n"
                    f"Schemaに厳密に従うJSON1個のみを再出力してください。",
    })
    resp2 = client.chat.completions.create(model=model, messages=messages, temperature=0.2)
    raw2 = resp2.choices[0].message.content
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        data2 = _extract_json_object(raw2)
        jsonschema.validate(data2, schema)
        combined_usage = _usage_dict(resp)
        u2 = _usage_dict(resp2)
        for k in combined_usage:
            combined_usage[k] += u2.get(k, 0)
        return {"data": data2, "mode": "manual_json_retry", "token_usage": combined_usage,
                "latency_ms": latency_ms, "raw_output": data2}
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        raise ExpertLLMError(f"Schema検証が2回とも失敗しました: {e}") from e


def call_llm_task(client, azure_client, model: str, *, system_prompt: str, user_prompt: str,
                   schema: dict, schema_name: str, article_cluster_id: str, task_type: str,
                   prompt_version: str, expert_version: str, context_chunk_ids: list,
                   input_hash: str) -> dict:
    """call_llm_structuredを呼び、失敗時のexpert_runsへのエラー記録までここで行う
    （selector/generatorに共通していた try/except + log_expert_run のエラー処理部分を集約）。

    成功時はログ保存を行わず、call_llm_structuredの戻り値に"status":"success"を足して返す。
    成功時のログ保存は呼び出し側の責務のまま（selectorはdataのarticle_id上書き、generatorは
    expert_contentsへの追加insertなど、成功後の後処理がタスクごとに異なるため）。

    戻り値: 成功時 {"status": "success", "data":..., "token_usage":..., "latency_ms":..., ...}
            失敗時 {"status": "schema_invalid"|"error", "run_id":..., "error_message":...}
    """
    try:
        result = call_llm_structured(azure_client, model, system_prompt, user_prompt, schema, schema_name)
    except ExpertLLMError as e:
        run_id = log_expert_run(
            client, article_cluster_id=article_cluster_id, task_type=task_type,
            model_deployment=model, prompt_version=prompt_version, expert_version=expert_version,
            context_chunk_ids=context_chunk_ids, input_hash=input_hash, output_json=None,
            token_usage=None, latency_ms=None, status="schema_invalid", error_message=str(e))
        return {"status": "schema_invalid", "run_id": run_id, "error_message": str(e)}
    except Exception as e:
        run_id = log_expert_run(
            client, article_cluster_id=article_cluster_id, task_type=task_type,
            model_deployment=model, prompt_version=prompt_version, expert_version=expert_version,
            context_chunk_ids=context_chunk_ids, input_hash=input_hash, output_json=None,
            token_usage=None, latency_ms=None, status="error", error_message=f"{type(e).__name__}: {e}")
        return {"status": "error", "run_id": run_id, "error_message": str(e)}

    result["status"] = "success"
    return result


# ─── expert_runs への保存 ─────────────────────────────────────────
def log_expert_run(client, *, article_cluster_id: str, task_type: str, model_deployment: str,
                    prompt_version: str, expert_version: str, context_chunk_ids: list,
                    input_hash: str, output_json: dict | None, token_usage: dict | None,
                    latency_ms: int | None, status: str, error_message: str | None = None) -> str:
    """expert_runs に1行保存し、run_idを返す。
    本文・社内コンテキストの全文はログ(print)には出さず、DBのみに保存する"""
    rows = client.insert("expert_runs", [{
        "article_cluster_id": article_cluster_id,
        "task_type": task_type,
        "model_deployment": model_deployment,
        "prompt_version": prompt_version,
        "expert_version": expert_version,
        "context_chunk_ids": context_chunk_ids or [],
        "input_hash": input_hash,
        "output_json": output_json,
        "token_usage": token_usage,
        "latency_ms": latency_ms,
        "status": status,
        "error_message": error_message,
    }])
    return rows[0]["run_id"]


def find_existing_success_run(client, *, article_cluster_id: str, task_type: str,
                               input_hash: str, expert_version: str) -> dict | None:
    """同一クラスタ・同一入力・同一専門家バージョンで成功済みの実行があれば返す
    （重複LLM呼び出し防止）"""
    rows = client.select("expert_runs", {
        "select": "run_id,output_json,created_at",
        "article_cluster_id": f"eq.{article_cluster_id}",
        "task_type": f"eq.{task_type}",
        "input_hash": f"eq.{input_hash}",
        "expert_version": f"eq.{expert_version}",
        "status": "eq.success",
        "order": "created_at.desc",
        "limit": "1",
    })
    return rows[0] if rows else None
