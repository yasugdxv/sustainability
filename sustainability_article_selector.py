"""
当社サステナビリティ専門家MVP: 記事選定パイプライン

収集済み記事(事象クラスタ単位)を、当社サステナビリティの公式方針・重点テーマ・
中長期目標との関連性で評価し、publish_candidate / watch_or_archive / not_selected に
分類する。コンテンツ生成(sustainability_content_generator.py)とは必ず別のLLM呼び出しにする。

使い方:
    python sustainability_article_selector.py                       # 未処理クラスタを全件処理
    python sustainability_article_selector.py 5                      # 先頭5クラスタだけ処理（テスト用）
    python sustainability_article_selector.py --since-days 7         # 直近7日分のみ対象
    python sustainability_article_selector.py --article-ids <id1>,<id2>  # 記事ID指定
    python sustainability_article_selector.py --top-n-per-theme 10   # テーマ大分類ごとに
                                                                      # importance_level+スコア上位N件
                                                                      # ＋マテリアリティ接続タグ付き
                                                                      # クラスタ（ワイルドカード）のみ処理
    python sustainability_article_selector.py --batch-size 10        # 1回のLLM呼び出しに10クラスタ分
                                                                      # まとめて評価させ、呼び出し回数を
                                                                      # 減らす（バッチのJSON検証が最終的に
                                                                      # 失敗した場合のみ、そのバッチだけ
                                                                      # 1件ずつの呼び出しにフォールバックする）
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
from sustainability_knowledge_store import get_knowledge_store  # noqa: E402
import article_filter  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROMPT_VERSION = "select-v0.1"


# ─── 対象クラスタの抽出 ───────────────────────────────────────────
def list_candidate_cluster_ids(client, since_days: int = None, article_ids: list = None,
                                all_urls: dict = None) -> list:
    """未処理の事象クラスタ（article_urls.article_url_idを代表IDとする）一覧を返す。
    all_urlsを渡せばcommon.fetch_all_article_urlsの結果を使い回す（未指定ならここで取得する）"""
    params = {"select": "article_id,article_url_id,published_at", "is_current": "eq.true"}
    if article_ids:
        params["article_id"] = f"in.({','.join(article_ids)})"
    articles = client.select("articles", params)

    suppressed_ids = {
        a["article_id"] for a in common._select_in_chunks(
            client, "article_analysis", {"select": "article_id,representative_role"},
            "article_id", [a["article_id"] for a in articles])
        if a.get("representative_role") == "suppressed_duplicate"
    }
    articles = [a for a in articles if a["article_id"] not in suppressed_ids]

    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

        def _within(a):
            pub = a.get("published_at")
            if not pub:
                return True
            try:
                return datetime.fromisoformat(pub.replace("Z", "+00:00")) >= cutoff
            except Exception:
                return True

        articles = [a for a in articles if _within(a)]

    if not articles:
        return []

    if all_urls is None:
        all_urls = common.fetch_all_article_urls(client)

    roots = set()
    for a in articles:
        url_id = a["article_url_id"]
        url_row = all_urls.get(url_id, {"article_url_id": url_id})
        roots.add(common.resolve_cluster_root(url_row, all_urls))
    return sorted(roots)


RANK_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, None: 5}

# Tier0〜3の設定値。2026-08-03 PMOフィードバック（重要度判定仕様・記事掲載ルールへの
# フィードバック v0.1）反映済み。
# - Tier1（マテリアリティ接続タグワイルドカード）は廃止。マテリアリティ紐付けは月次の
#   外部環境スクリーニングシート更新（Step2）が別途担当するため。
# - Tier0の対象は「主要9テーマ＋横断2軸（情報開示／ESG評価・サステナブルファイナンス）」。
#   PMOのテーマ体系（主要9軸＋横断2軸＋下位5軸）に合わせ、この2つの横断タグはTier2ではなく
#   Tier0（無条件・上位N件）側で扱う。
TIER0_RANK_FLOOR = {"S", "A", "B"}
TIER0_PROMOTED_CROSS_CUTTING_IDS = {"CR-01", "CR-02"}  # 情報開示／ESG評価・サステナブルファイナンス
# 自社（サントリーグループ）発の記事は、週次ダイジェスト・重要度判定の候補選定から除外する
# （PMO 2026-08-18フィードバック3章(2)。自社サイトの開示自体は競合モニタリング側で別途収集する）
OWN_COMPANY_TAG_ID = "SJ-12"
# Tier0内で「規制・基準系」と「企業・業界の取組事例系」の双方が一定数含まれるよう配慮する
# （記事種別タグ AT-01規制・法律／AT-03基準・イニシアチブ を規制・基準系、AT-02企業活動を
# 取組事例系とみなす）
REGULATORY_TYPE_ROOT_IDS = {"AT-01", "AT-03"}
CORPORATE_TYPE_ROOT_IDS = {"AT-02"}

CROSS_CUTTING_RANK_FLOOR = {"S", "A"}
CROSS_CUTTING_TOP_N_DEFAULT = 10
SUB_AXIS_PARENT_TAG_ID = "TH-10"  # 下位種別（下位5軸の親大分類）
SUB_AXIS_RANK_FLOOR = {"S", "A"}
SUB_AXIS_TOP_N_DEFAULT = 5
# 下位軸の監視閾値（要件定義案2.5）は次の3条件：
#   (i) 主要地理・グローバルでの規制の新設・重要改定
#   (ii) 業界に波及しうる重大訴訟・不祥事
#   (iii) 基準・団体の統廃合
# 専用のLLM出力フィールドが未実装のため、暫定的に既存の「閾値語（横断）」キーワードグループ
# への本文一致を代理指標として使う（このグループには(i)(ii)を捉える語彙も含める必要がある）。
SUB_AXIS_THRESHOLD_KEYWORD_GROUP = "閾値語（横断）"


def _load_threshold_patterns(client) -> list:
    rows = client.select("filter_keywords", {
        "select": "keyword_text",
        "keyword_group": f"eq.{SUB_AXIS_THRESHOLD_KEYWORD_GROUP}",
        "status": "eq.有効",
    })
    return [article_filter._compile(r["keyword_text"]) for r in rows]


def _matches_threshold_condition(patterns: list, title: str, body: str) -> bool:
    haystack = (title or "") + "\n" + (body or "")[:1000]
    return any(p.search(haystack) for p in patterns)


def _stratified_top_n(candidate_ids: list, clusters: dict, top_n: int) -> list:
    """スコア上位を優先しつつ、規制・基準系(regulatory_type)と企業・業界の取組事例系
    (corporate_type)の双方が一定数含まれるよう配慮する（PMO 2026-08-03フィードバック④）。
    各タイプについて上限top_n//2件を優先確保し、残り枠は全体のスコア順で埋める。"""
    half = -(-top_n // 2)  # 切り上げ
    regulatory = sorted((c for c in candidate_ids if clusters[c]["regulatory_type"]),
                         key=lambda c: clusters[c]["best_key"])[:half]
    corporate = sorted((c for c in candidate_ids if clusters[c]["corporate_type"]),
                        key=lambda c: clusters[c]["best_key"])[:half]

    picked, seen = [], set()
    for cid in regulatory + corporate:
        if cid not in seen:
            seen.add(cid)
            picked.append(cid)

    if len(picked) < top_n:
        remaining = sorted((c for c in candidate_ids if c not in seen),
                            key=lambda c: clusters[c]["best_key"])
        picked.extend(remaining[:top_n - len(picked)])

    picked.sort(key=lambda c: clusters[c]["best_key"])
    return picked[:top_n]


def list_theme_prioritized_cluster_ids(client, all_urls: dict, top_n: int = 10,
                                        top_n_cross_cutting: int = CROSS_CUTTING_TOP_N_DEFAULT,
                                        top_n_sub_axis: int = SUB_AXIS_TOP_N_DEFAULT) -> list:
    """主要9テーマ＋横断2軸（情報開示／ESG評価・サステナブルファイナンス）ごとに、
    ランクB以上かつimportance_total_score上位N件の事象クラスタのみを候補とする（Tier0）。
    各テーマ・軸の上位N件選定では、規制・基準系と企業・業界の取組事例系の双方が
    一定数含まれるよう配慮する（_stratified_top_n）。

    Tier0で拾いきれない記事を取りこぼさないよう、以下の2つのワイルドカードを追加する：
      - Tier2: 横断タグ（Tier0に昇格した2軸を除く）が付き、ランクS・Aの事象クラスタを、
        スコア降順で上位top_n_cross_cutting件まで候補に加える
      - Tier3: 下位軸タグ（テーマ軸のTH-10配下の小分類）が付き、ランクS・A、かつ
        「閾値語（横断）」キーワードへの本文一致（3条件の代理判定）がある事象クラスタを、
        スコア降順で上位top_n_sub_axis件まで候補に加える

    全クラスタをLLMで評価するとコストが大きいため、重要度が既に高い/戦略的に重要とわかっている
    クラスタに絞ってLLM選定を行うための事前フィルタ。"""
    tag_rows = client.select("tag_reference", {"select": "tag_id,tag_axis,tag_level,parent_tag_id"})
    tag_by_id = {t["tag_id"]: t for t in tag_rows}
    theme_major_ids = {t["tag_id"] for t in tag_rows if t["tag_axis"] == "テーマ" and t["tag_level"] == "大分類"}
    cross_cutting_tag_ids = {t["tag_id"] for t in tag_rows if t["tag_axis"] == "横断"}
    cross_cutting_tag_ids -= TIER0_PROMOTED_CROSS_CUTTING_IDS
    sub_axis_tag_ids = {t["tag_id"] for t in tag_rows if t.get("parent_tag_id") == SUB_AXIS_PARENT_TAG_ID}

    tier0_bucket_ids = (theme_major_ids - {SUB_AXIS_PARENT_TAG_ID}) | TIER0_PROMOTED_CROSS_CUTTING_IDS

    def theme_major_of(tag_id: str) -> str | None:
        """tag_idがテーマ軸の（小分類含む）タグなら、対応する大分類のtag_idを返す。テーマ軸以外はNone"""
        seen = set()
        t = tag_by_id.get(tag_id)
        while t and t["tag_id"] not in seen:
            if t["tag_axis"] != "テーマ":
                return None
            if t["tag_id"] in theme_major_ids:
                return t["tag_id"]
            seen.add(t["tag_id"])
            t = tag_by_id.get(t.get("parent_tag_id"))
        return None

    def article_type_root_of(tag_id: str) -> str | None:
        """tag_idが記事種別軸のタグなら、対応する大分類のtag_idを返す。それ以外はNone"""
        t = tag_by_id.get(tag_id)
        if not t or t["tag_axis"] != "記事種別":
            return None
        if t["tag_level"] == "大分類":
            return t["tag_id"]
        parent = tag_by_id.get(t.get("parent_tag_id"))
        return parent["tag_id"] if parent else None

    own_company_target_ids = {
        t["crawl_target_id"] for t in client.select(
            "crawl_targets", {"select": "crawl_target_id,publisher_tag_id"})
        if t.get("publisher_tag_id") == OWN_COMPANY_TAG_ID
    }
    articles = client.select("articles", {
        "select": "article_id,article_url_id,crawl_target_id", "is_current": "eq.true"})
    articles = [a for a in articles if a.get("crawl_target_id") not in own_company_target_ids]
    analysis_by_article = {
        a["article_id"]: a for a in client.select(
            "article_analysis", {"select": "article_id,importance_level,importance_total_score,"
                                            "representative_role"})
    }
    # 一次情報の要約・言い換えに留まると判定された二次記事(representative_role=
    # suppressed_duplicate)は、そもそも選定用LLM評価の候補にしない。一次情報側は
    # 本ロジックと無関係に通常通り評価される（article_analyzer.py参照）
    articles = [a for a in articles
                if analysis_by_article.get(a["article_id"], {}).get("representative_role")
                != "suppressed_duplicate"]
    tags_by_article = defaultdict(set)
    for r in client.select("article_tags", {"select": "article_id,tag_id"}):
        tags_by_article[r["article_id"]].add(r["tag_id"])

    # クラスタ単位に集約：所属テーマ大分類・Tier0昇格軸（複数可）、横断・下位軸タグの有無、
    # 記事種別（規制・基準系／企業取組系）、クラスタ内で最も評価が高い記事の
    # importance_level/スコアを代表値とする
    clusters = defaultdict(lambda: {"themes": set(), "cross_cutting": False, "sub_axis": False,
                                     "regulatory_type": False, "corporate_type": False,
                                     "article_ids": set(), "best_key": None, "best_rank": None})
    for a in articles:
        article_id = a["article_id"]
        url_row = all_urls.get(a["article_url_id"], {"article_url_id": a["article_url_id"]})
        root = common.resolve_cluster_root(url_row, all_urls)
        c = clusters[root]
        c["article_ids"].add(article_id)
        for tag_id in tags_by_article.get(article_id, ()):
            theme = theme_major_of(tag_id)
            if theme:
                c["themes"].add(theme)
            if tag_id in TIER0_PROMOTED_CROSS_CUTTING_IDS:
                c["themes"].add(tag_id)
            if tag_id in cross_cutting_tag_ids:
                c["cross_cutting"] = True
            if tag_id in sub_axis_tag_ids:
                c["sub_axis"] = True
            art_type_root = article_type_root_of(tag_id)
            if art_type_root in REGULATORY_TYPE_ROOT_IDS:
                c["regulatory_type"] = True
            if art_type_root in CORPORATE_TYPE_ROOT_IDS:
                c["corporate_type"] = True

        an = analysis_by_article.get(article_id, {})
        score = an.get("importance_total_score")
        level = an.get("importance_level")
        key = (RANK_ORDER.get(level, 5), -(score if score is not None else -1))
        if c["best_key"] is None or key < c["best_key"]:
            c["best_key"] = key
            c["best_rank"] = level

    # Tier0: 主要9テーマ＋横断2軸ごとに、ランクB以上のクラスタから上位N件
    # （規制・基準系／企業取組系のバランスに配慮）
    selected = set()
    for bucket_id in tier0_bucket_ids:
        candidates = [cid for cid, c in clusters.items()
                      if bucket_id in c["themes"] and c["best_rank"] in TIER0_RANK_FLOOR]
        selected.update(_stratified_top_n(candidates, clusters, top_n))

    # Tier2: 横断タグ（Tier0昇格分を除く）＋S・Aランクの孤立クラスタを、スコア降順で上位N件まで追加
    cross_cutting_pool = [
        cid for cid, c in clusters.items()
        if c["cross_cutting"] and cid not in selected and c["best_rank"] in CROSS_CUTTING_RANK_FLOOR
    ]
    cross_cutting_pool.sort(key=lambda cid: clusters[cid]["best_key"])
    selected.update(cross_cutting_pool[:top_n_cross_cutting])

    # Tier3: 下位軸タグ＋S・Aランク＋3条件（閾値語キーワードでの代理判定）の孤立クラスタを、
    # スコア降順で上位N件まで追加
    sub_axis_pool_raw = [
        cid for cid, c in clusters.items()
        if c["sub_axis"] and cid not in selected and c["best_rank"] in SUB_AXIS_RANK_FLOOR
    ]
    if sub_axis_pool_raw:
        threshold_patterns = _load_threshold_patterns(client)
        candidate_article_ids = sorted({
            aid for cid in sub_axis_pool_raw for aid in clusters[cid]["article_ids"]
        })
        article_text_by_id = {
            a["article_id"]: a for a in client.select(
                "articles", {"select": "article_id,title,extracted_text",
                             "article_id": f"in.({','.join(candidate_article_ids)})"})
        }

        def _cluster_meets_threshold(cid: str) -> bool:
            for aid in clusters[cid]["article_ids"]:
                art = article_text_by_id.get(aid)
                if art and _matches_threshold_condition(threshold_patterns, art.get("title"),
                                                         art.get("extracted_text")):
                    return True
            return False

        sub_axis_pool = [cid for cid in sub_axis_pool_raw if _cluster_meets_threshold(cid)]
        sub_axis_pool.sort(key=lambda cid: clusters[cid]["best_key"])
        selected.update(sub_axis_pool[:top_n_sub_axis])

    return sorted(selected)


def list_weekly_picks(client, since_days: int = 7, expert_version: str = None,
                       target_max: int = 20) -> list:
    """今週のメールに載せる事象クラスタを決定論的に選ぶ（weekly_email_report.pyから使う）。

    直近since_days日以内にtask_type='select'で成功したexpert_runsのうち、クラスタ単位で
    最新の判定のみを残し、decision in ('publish_candidate', 'watch_or_archive')
    （＝not_selectedだけを除外）のものをtotal_score降順に並べ、先頭target_max件に切り詰める。

    LLMの個別判定（decision/total_score）は一切変えず、「今週何件配信するか」という量だけを
    機械的に絞り込む。importance_rank_definitions（S〜Dの点数閾値）と同じく、target_maxは
    実績を見ながら調整するチューニング対象という位置づけ（LLM自身に正確な週次件数を
    守らせようとしない）。

    target_maxに満たない場合は、ある分だけ返す（無理に埋めない）。

    戻り値: total_score降順の
      [{"article_cluster_id": ..., "select_run_id": ..., "decision": ...,
        "total_score": ..., "assessment": {...output_json...}}, ...]
    """
    since_cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    params = {
        "select": "run_id,article_cluster_id,output_json,created_at",
        "task_type": "eq.select",
        "status": "eq.success",
        "created_at": f"gte.{since_cutoff}",
        "order": "created_at.desc",
    }
    if expert_version:
        params["expert_version"] = f"eq.{expert_version}"
    rows = client.select("expert_runs", params)

    latest_by_cluster = {}
    for row in rows:
        cid = row["article_cluster_id"]
        if cid not in latest_by_cluster:  # order=created_at.desc なので最初に来たものが最新
            latest_by_cluster[cid] = row

    eligible = []
    for cid, row in latest_by_cluster.items():
        assessment = row.get("output_json") or {}
        decision = assessment.get("decision")
        if decision == "not_selected":
            continue
        eligible.append({
            "article_cluster_id": cid, "select_run_id": row["run_id"], "decision": decision,
            "total_score": assessment.get("total_score") or 0, "assessment": assessment,
        })

    eligible.sort(key=lambda p: p["total_score"], reverse=True)
    return eligible[:target_max]


def already_processed(client, article_cluster_id: str, expert_version: str) -> bool:
    """このクラスタが、現在の専門家バージョンで既に選定済み（成功）かどうか"""
    rows = client.select("expert_runs", {
        "select": "run_id",
        "article_cluster_id": f"eq.{article_cluster_id}",
        "task_type": "eq.select",
        "expert_version": f"eq.{expert_version}",
        "status": "eq.success",
        "limit": "1",
    })
    return bool(rows)


# ─── プロンプト構築 ───────────────────────────────────────────────
def _format_related_articles(members: list) -> str:
    if not members:
        return "（同一事象と判定された他の記事は無し）"
    lines = []
    for m in members[:5]:
        lines.append(f"- {m.get('title', '')} ({m.get('final_url') or m.get('fetched_url', '')})")
    return "\n".join(lines)


def build_user_prompt(representative: dict, members: list, retrieved_docs: list) -> str:
    text = (representative.get("extracted_text") or "")[:common.MAX_ARTICLE_CHARS]
    target = representative.get("_target", {})
    analysis = representative.get("_analysis") or {}
    primary_status = analysis.get("primary_source_status") or "不明（未分析）"

    return (
        f"# article\n"
        f"article_id: {representative['article_id']}\n"
        f"title: {representative.get('title') or ''}\n"
        f"url: {representative.get('final_url') or representative.get('fetched_url') or ''}\n"
        f"published_at: {representative.get('published_at') or '不明'}\n"
        f"publisher: {target.get('publisher_name') or ''}（{target.get('domain') or ''}）\n"
        f"primary_or_interpretive: {primary_status}\n\n"
        f"本文:\n{text}\n\n"
        f"# duplicate_cluster（同一事象と判定された関連記事）\n{_format_related_articles(members)}\n\n"
        f"# retrieved_context（Azure AI Searchで取得した当社公式コンテキスト）\n"
        f"{common.format_retrieved_context(retrieved_docs)}\n"
    )


# 記事選定タスクではコンテンツ生成用の指示(content_generation_framework)やバージョン管理用の
# metadataは使わないため、system_promptのトークン量を減らすために除いて渡す
_SELECT_IRRELEVANT_EXPERT_BASE_KEYS = ("content_generation_framework", "metadata")


def _select_expert_base(expert_base: dict) -> dict:
    return {k: v for k, v in expert_base.items() if k not in _SELECT_IRRELEVANT_EXPERT_BASE_KEYS}


# ─── 1クラスタの選定処理 ───────────────────────────────────────────
def select_cluster(client, azure_client, model: str, knowledge_store, expert_base: dict,
                    expert_version: str, article_cluster_id: str, all_urls: dict = None) -> dict:
    """1事象クラスタを選定評価する。戻り値: {"status": ..., "run_id": ..., "decision": ...}
    all_urlsはcommon.get_clusterへそのまま渡す（複数クラスタ処理時の重複フルスキャン回避用）"""
    cluster = common.get_cluster(client, article_cluster_id, all_urls=all_urls)
    if cluster is None:
        return {"status": "error", "error_message": "クラスタの代表記事が見つかりません"}

    representative = cluster["representative"]
    members = cluster["members"]

    query = f"{representative.get('title', '')} {(representative.get('extracted_text') or '')[:300]}"
    retrieved_docs = knowledge_store.search(query, top_k=common.CONTEXT_TOP_K)
    context_chunk_ids = [d["document_id"] for d in retrieved_docs]

    input_hash = common.compute_input_hash(
        representative.get("extracted_text") or "",
        ",".join(sorted(context_chunk_ids)),
        expert_version,
    )

    existing = common.find_existing_success_run(
        client, article_cluster_id=article_cluster_id, task_type="select",
        input_hash=input_hash, expert_version=expert_version)
    if existing:
        return {"status": "skipped_duplicate", "run_id": existing["run_id"],
                "decision": existing["output_json"].get("decision")}

    system_prompt = common.build_system_prompt(common.load_selection_prompt(), _select_expert_base(expert_base))
    user_prompt = build_user_prompt(representative, members, retrieved_docs)
    schema = common.load_assessment_schema()

    outcome = common.call_llm_task(
        client, azure_client, model, system_prompt=system_prompt, user_prompt=user_prompt,
        schema=schema, schema_name="SustainabilityArticleAssessment",
        article_cluster_id=article_cluster_id, task_type="select", prompt_version=PROMPT_VERSION,
        expert_version=expert_version, context_chunk_ids=context_chunk_ids, input_hash=input_hash)
    if outcome["status"] != "success":
        return outcome

    data = outcome["data"]
    data["article_id"] = representative["article_id"]  # モデルの自己申告値より実IDを優先する

    run_id = common.log_expert_run(
        client, article_cluster_id=article_cluster_id, task_type="select",
        model_deployment=model, prompt_version=PROMPT_VERSION, expert_version=expert_version,
        context_chunk_ids=context_chunk_ids, input_hash=input_hash, output_json=data,
        token_usage=outcome["token_usage"], latency_ms=outcome["latency_ms"], status="success")

    return {"status": "success", "run_id": run_id, "decision": data["decision"],
            "total_score": data["total_score"], "output": data}


BATCH_PROMPT_VERSION = "select-v0.1-batch"


def _build_batch_schema(base_schema: dict) -> dict:
    return {
        "$schema": base_schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "title": "SustainabilityArticleAssessmentBatch",
        "type": "object",
        "additionalProperties": False,
        "required": ["assessments"],
        "properties": {"assessments": {"type": "array", "items": base_schema}},
    }


def _build_batch_user_prompt(items: list) -> str:
    parts = [
        f"以下は{len(items)}件の独立した事象クラスタです。article_indexの順序を保ったまま、"
        "それぞれを個別に評価し、assessments配列の対応する位置（1件目→0番目）に1件ずつ格納すること。"
        "記事間の内容・出典を混同しないこと。"
    ]
    for i, item in enumerate(items):
        parts.append(f"\n{'='*20} article_index: {i} {'='*20}\n")
        parts.append(build_user_prompt(item["representative"], item["members"], item["retrieved_docs"]))
    return "\n".join(parts)


def select_clusters_batch(client, azure_client, model: str, knowledge_store, expert_base: dict,
                           expert_version: str, cluster_ids: list, all_urls: dict = None) -> list:
    """複数クラスタを1回のLLM呼び出しでまとめて評価する（LLM呼び出し回数の削減が目的）。
    クラスタごとの事前準備（クラスタ解決・検索・重複チェック）はselect_clusterと同じ処理を流用し、
    1件ずつの評価が必要なもの（重複スキップ/クラスタ取得エラー）だけこの時点で確定させる。

    バッチのJSON検証が最終的に（構造化出力→手動抽出→1回だけ修正再実行、を経ても）失敗した場合は、
    バッチ全体を失わないよう、そのバッチの各クラスタをselect_cluster()で1件ずつ評価し直す
    （フォールバック）。

    戻り値: cluster_idsと同じ順序・同じ長さの、select_cluster相当の結果dictのリスト"""
    prepared = {}
    for cid in cluster_ids:
        cluster = common.get_cluster(client, cid, all_urls=all_urls)
        if cluster is None:
            prepared[cid] = {"status": "error", "error_message": "クラスタの代表記事が見つかりません"}
            continue

        representative = cluster["representative"]
        query = f"{representative.get('title', '')} {(representative.get('extracted_text') or '')[:300]}"
        retrieved_docs = knowledge_store.search(query, top_k=common.CONTEXT_TOP_K)
        context_chunk_ids = [d["document_id"] for d in retrieved_docs]
        input_hash = common.compute_input_hash(
            representative.get("extracted_text") or "", ",".join(sorted(context_chunk_ids)), expert_version)

        existing = common.find_existing_success_run(
            client, article_cluster_id=cid, task_type="select",
            input_hash=input_hash, expert_version=expert_version)
        if existing:
            prepared[cid] = {"status": "skipped_duplicate", "run_id": existing["run_id"],
                              "decision": existing["output_json"].get("decision")}
            continue

        prepared[cid] = {
            "cluster_id": cid, "representative": representative, "members": cluster["members"],
            "retrieved_docs": retrieved_docs, "context_chunk_ids": context_chunk_ids,
            "input_hash": input_hash,
        }

    needs_llm = [prepared[cid] for cid in cluster_ids if "cluster_id" in prepared[cid]]
    if not needs_llm:
        return [prepared[cid] for cid in cluster_ids]

    system_prompt = common.build_system_prompt(common.load_selection_prompt(), _select_expert_base(expert_base))
    user_prompt = _build_batch_user_prompt(needs_llm)
    batch_schema = _build_batch_schema(common.load_assessment_schema())

    try:
        result = common.call_llm_structured(
            azure_client, model, system_prompt, user_prompt,
            batch_schema, "SustainabilityArticleAssessmentBatch")
        assessments = result["data"]["assessments"]
        if len(assessments) != len(needs_llm):
            raise common.ExpertLLMError(
                f"assessments件数({len(assessments)})が要求件数({len(needs_llm)})と一致しません")
    except common.ExpertLLMError:
        # バッチ全体を失わないよう、このバッチの分だけ1件ずつ評価し直す
        for item in needs_llm:
            prepared[item["cluster_id"]] = select_cluster(
                client, azure_client, model, knowledge_store, expert_base,
                expert_version, item["cluster_id"], all_urls=all_urls)
        return [prepared[cid] for cid in cluster_ids]

    usage = result["token_usage"] or {}
    per_item_usage = {k: v // len(needs_llm) for k, v in usage.items()} if usage else None
    for item, data in zip(needs_llm, assessments):
        data["article_id"] = item["representative"]["article_id"]  # モデルの自己申告値より実IDを優先する
        run_id = common.log_expert_run(
            client, article_cluster_id=item["cluster_id"], task_type="select",
            model_deployment=model, prompt_version=BATCH_PROMPT_VERSION, expert_version=expert_version,
            context_chunk_ids=item["context_chunk_ids"], input_hash=item["input_hash"], output_json=data,
            token_usage=per_item_usage, latency_ms=result["latency_ms"], status="success")
        prepared[item["cluster_id"]] = {"status": "success", "run_id": run_id, "decision": data["decision"],
                                         "total_score": data.get("total_score"), "output": data}

    return [prepared[cid] for cid in cluster_ids]


# ─── メイン ───────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="当社サステナビリティ記事選定パイプライン")
    parser.add_argument("limit", nargs="?", type=int, default=None, help="先頭N件だけ処理（テスト用）")
    parser.add_argument("--since-days", type=int, default=None, help="直近N日分の記事のみ対象")
    parser.add_argument("--article-ids", type=str, default=None, help="カンマ区切りのarticle_id指定")
    parser.add_argument("--top-n-per-theme", type=int, default=None,
                         help="テーマ大分類ごとにimportance_level+スコア上位N件＋マテリアリティ接続タグ付き"
                              "クラスタ（ワイルドカード）のみを対象にする（--since-days/--article-idsとは併用不可）")
    parser.add_argument("--batch-size", type=int, default=1,
                         help="1回のLLM呼び出しにまとめて評価させるクラスタ数（既定1=1件ずつ、従来通り）。"
                              "バッチのJSON検証が最終的に失敗した場合は、そのバッチだけ1件ずつの呼び出しに"
                              "フォールバックする")
    args = parser.parse_args()
    if args.top_n_per_theme and (args.since_days or args.article_ids):
        parser.error("--top-n-per-theme は --since-days / --article-ids と併用できません")

    config = load_config()
    if not common.is_enabled(config):
        print("SUSTAINABILITY_EXPERT_ENABLED が無効です（config.jsonのsustainability_expert.enabled、"
              "または環境変数で有効化してください）。処理を行わず終了します。")
        return

    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if not azure_client:
        print("Azure OpenAI / OpenAI のAPIキーが設定されていません")
        return

    expert_base = common.load_expert_base()
    expert_version = common.get_expert_version(config)
    knowledge_store = get_knowledge_store(config)

    all_urls = common.fetch_all_article_urls(client)
    if args.top_n_per_theme:
        cluster_ids = list_theme_prioritized_cluster_ids(client, all_urls, top_n=args.top_n_per_theme)
    else:
        article_ids = [a.strip() for a in args.article_ids.split(",")] if args.article_ids else None
        cluster_ids = list_candidate_cluster_ids(client, since_days=args.since_days, article_ids=article_ids,
                                                  all_urls=all_urls)
    if args.limit:
        cluster_ids = cluster_ids[:args.limit]

    print(f"選定対象クラスタ: {len(cluster_ids)}件（expert_version={expert_version}、"
          f"batch_size={args.batch_size}）")
    counts = {"success": 0, "skipped_duplicate": 0, "schema_invalid": 0, "error": 0}
    done = 0
    for start in range(0, len(cluster_ids), args.batch_size):
        chunk = cluster_ids[start:start + args.batch_size]
        try:
            if args.batch_size > 1:
                results = select_clusters_batch(client, azure_client, model, knowledge_store,
                                                 expert_base, expert_version, chunk, all_urls=all_urls)
            else:
                results = [select_cluster(client, azure_client, model, knowledge_store,
                                           expert_base, expert_version, chunk[0], all_urls=all_urls)]
        except Exception as e:
            for cluster_id in chunk:
                done += 1
                print(f"[{done}/{len(cluster_ids)}] {cluster_id} ... 予期しないエラー: {type(e).__name__}: {e}")
                counts["error"] += 1
            continue

        for cluster_id, result in zip(chunk, results):
            done += 1
            print(f"[{done}/{len(cluster_ids)}] {cluster_id} ...", end=" ", flush=True)
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            if result["status"] == "success":
                print(f"完了 decision={result['decision']}({result.get('total_score')}点)")
            elif result["status"] == "skipped_duplicate":
                print(f"スキップ（既に選定済み decision={result.get('decision')}）")
            else:
                print(f"{result['status']}: {result.get('error_message', '')}")

    print("─" * 40)
    print("集計:", counts)


if __name__ == "__main__":
    main()
