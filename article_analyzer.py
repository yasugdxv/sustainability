"""
記事分析パイプライン: タグ付け・出典タイプ判定・一次情報のWeb検索確認・重要度採点

対象: articles のうち is_current=true かつ article_analysis(is_current=true) が
      まだ無いもの。Azure OpenAI の responses API を使い、記事1件につき
      最大2回のLLM呼び出しで下記を行う。
        - call1（検索ツールなし）: タグ付け・出典タイプ判定・重要度採点・
          Web検索確認の要否判定
        - call2（call1がWeb検索確認を必要と判断した場合のみ、検索ツールを
          強制した状態で実行）: 一次情報源の実在確認と根拠URLの取得

重要な設計方針:
    1回のLLM呼び出しに全部（タグ付け〜Web検索）を任せると、検索ツールを
    採点の計算等の無関係な用途に使ってしまい、needs_web_verification=true
    と自己申告しつつ実際には検索しない、といった不整合が高頻度で発生する
    ことを確認した。そのため検索が必要な場合は「検索して確認するだけ」に
    役割を絞った専用の2回目の呼び出しに分離し、tool_choice="required"で
    検索ツールの呼び出し自体を強制する。
    また、Web検索ツールは実在しない具体的URLを本文中に生成することがある
    （検証済み）。そのため article_evidence に保存するURLは、APIが返す
    構造化された引用情報（annotations の url_citation）のみを使う。
    本文中の自由記述URLは一切信用しない。

使い方:
    python article_analyzer.py 10      # 先頭10件だけ処理（テスト用）
    python article_analyzer.py         # 未分析全件を処理
"""
import json
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import article_filter  # noqa: E402
import sustainability_expert_common as common  # noqa: E402

# Windowsコンソール(cp932)では海外記事タイトルの一部の文字が出力できずクラッシュ
# するため、標準出力をUTF-8に切り替える（変換できない文字は代替表記にする）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAX_ARTICLE_CHARS = 8000
PROMPT_VERSION = "v1"

SCORE_CRITERIA_ORDER = [
    "business_relevance", "change_severity", "impact_scope",
    "certainty_stage", "urgency", "novelty", "decision_value",
]


# ─── ルール・タグの読み込み（プロンプトに埋め込む） ──────────────────
def load_rubric_text(client: SupabaseClient) -> str:
    criteria = client.select("importance_criteria", {"select": "*", "order": "display_order"})
    bands = client.select("importance_score_bands", {"select": "*"})
    ranks = client.select("importance_rank_definitions", {"select": "*", "order": "display_order"})
    corrections = client.select("importance_correction_rules", {"select": "*", "order": "display_order"})
    reliability = client.select("source_reliability_rules", {"select": "*", "order": "display_order"})

    bands_by_criterion = {}
    for b in bands:
        bands_by_criterion.setdefault(b["criterion_id"], []).append(b)

    lines = ["# 重要度評価項目（各0〜5点、合計0〜35点）"]
    for c in criteria:
        lines.append(f"\n## {c['criterion_id']}: {c['name_ja']} ({c['description']})")
        for b in sorted(bands_by_criterion.get(c["criterion_id"], []), key=lambda x: -x["score"]):
            lines.append(f"  {b['score']}点: {b['definition']}")

    lines.append("\n# ランク（参考。総合点からランクは自動計算するので出力不要）")
    for r in ranks:
        lines.append(f"  {r['rank']}({r['name_ja']}) {r['min_score']}-{r['max_score']}点: {r['definition']}")

    lines.append("\n# 最低ランク・強制補正ルール（該当すれば correction_applied にどれか記載）")
    for r in corrections:
        lines.append(f"  ・{r['condition_text']} → {r['correction']}")

    lines.append("\n# 出典タイプ（primary_source_status。いずれか1つを選ぶ）")
    for r in reliability:
        lines.append(f"  ・{r['state']}: {r['definition']}（掲載ルール: {r['publication_rule']}）")

    return "\n".join(lines)


def load_tags(client: SupabaseClient) -> list:
    return client.select("tag_reference", {
        "select": "tag_id,tag_axis,tag_name,tag_meaning,tag_criteria,tag_level,parent_tag_id",
        "status": "eq.有効",
    })


def build_tag_text(tags: list) -> str:
    by_axis = {}
    for t in tags:
        by_axis.setdefault(t["tag_axis"], []).append(t)

    lines = [
        "# 利用可能なタグ一覧",
        "各行の先頭が tag_id。tags配列にはこの tag_id 単体の文字列のみを入れること"
        "（例: \"TH-02\"。\"TH-02 気候変動・GHG\" のようにタグ名を含めてはいけない）。"
        "ここに無いIDは使わないこと。",
    ]
    for axis, items in by_axis.items():
        lines.append(f"\n## {axis}")
        for t in items:
            lines.append(f"  [{t['tag_id']}] {t['tag_name']}: {t['tag_meaning']}（{t['tag_criteria']}）")
    return "\n".join(lines)


# ─── LLM呼び出し ────────────────────────────────────────────────
# call1: 検索ツールなし。タグ付け・出典判定・採点・検索要否判定＋検証すべき
# 主張(claim)の言語化までを行う（検索そのものはcall2に分離する）
SYSTEM_PROMPT_TEMPLATE = """あなたはサントリーグループのサステナビリティ情報分析AIです。
与えられた記事1件について、以下のキーを持つJSON1個のみを出力してください
（説明文・Markdown装飾は不要）。あなたにはWeb検索ツールは与えられていない
ため、記事本文の記述のみから判断すること。

1. tags: 記事内容に該当するタグのtag_idを配列で（複数可、該当なければ空配列）
2. primary_source_status: 出典タイプを1つ選ぶ（記事本文の記述のみに基づく
   暫定判定。実際の裏取りはこの後の別処理で行う）
3. source_status_reason: 出典タイプ判定の根拠（1-2文）
4. needs_web_verification: 記事が特定の一次情報源（規制当局の発表、企業の
   公式発表、統計機関の統計、国際機関の文書等）に言及・引用しており、
   その実在をWeb検索で確認する価値がある場合はtrue。記事自体が一次情報源
   そのものである場合や、検証可能な一次情報への言及が無い場合はfalse。
5. primary_source_claim: needs_web_verification=trueの場合のみ、検証すべき
   具体的な主張を1文で（例:「英国統計局(ONS)が2026年5月の月次GDP成長率を
   0.1%と発表した」）。固有名詞・数値・年月をできるだけ具体的に含めること。
   false の場合はnull。
6. scores: 7項目それぞれ0-5点の整数（キーは business_relevance, change_severity,
   impact_scope, certainty_stage, urgency, novelty, decision_value）
7. score_reasons: 7項目それぞれの採点理由（1文ずつ）
8. correction_applied: 補正ルールに該当すればその条件文をそのまま、無ければnull
9. summary_short: 記事の要約（180〜250文字程度）。「誰が」「何をした/何が起きたか」
   「なぜサス推にとって重要か」の3点が、この要約だけを読んで分かるように書くこと。
   一般論や記事タイトルの言い換えではなく、記事本文に書かれている具体的な数値・
   固有名詞・時期を含めること
10. importance_reason: 総合的な重要度判断の理由（2-3文）
11. needs_review: 人間の確認が望ましい場合true（例: 判定に自信が無い、内容が
    センシティブ、補正ルールとスコアが矛盾する等）

{rubric_text}

{tag_text}
"""

# call2: primary_source_claim の実在確認だけを行う専用プロンプト。
# tool_choice="required" と組み合わせて、検索ツールの呼び出しを強制する
SECONDARY_REDUNDANCY_SYSTEM_PROMPT = """あなたは二次記事が一次情報に対してどれだけ付加価値を
持つかを判定する専門AIです。あなたの役割は出典の信頼性を判定することではありません。

候補として提示する記事は、タグの一致・公開時期の近さによる機械的な事前絞り込みであり、
実際には別の事象を扱っている可能性があります。まず対象記事が候補群のいずれかと本当に
同一イベントを扱っているかを慎重に確認してください。一致するものが無ければ
matched_primary_article_idはnull、classificationもnullにしてください。

同一イベントの一次情報が見つかった場合のみ、対象記事（二次記事）を次の3種類に分類してください:

1. redundant_summary
   一次情報に既にある事実を要約・言い換えしているだけ。独自の事実、分析、データ、比較、
   意思決定に資する示唆を何も追加していない。

2. value_added_reporting
   独自取材で得た事実、関係者コメント、追加データ、他社比較、現地情報等、一次情報に無い
   独自の事実を提供している。

3. value_added_analysis
   規制変更の事業影響、業界全体への波及、競合比較、背景因果、シナリオ分析、複数一次情報の
   統合等、意味のある解釈・分析を提供している。

重要な注意事項:
- 単に文章が長いという理由だけでvalue-addedと判定しないこと
- 同じ事実の言い換えは付加価値ではない
- 一般的な背景知識を加えただけでは不十分
- 二次記事が意味のある分析・統合を提供している場合、一次情報より意思決定価値が高くなる
  ことは正当にあり得る
- redundant_summaryとvalue-addedの判断に迷う場合は、具体的な付加価値を特定できる場合のみ
  value-added側を選ぶこと（誤ってredundant_summaryと判定するより安全なため）

出力はJSON1個のみ:
{"matched_primary_article_id": "候補のarticle_idまたはnull",
 "classification": "redundant_summary/value_added_reporting/value_added_analysisまたはnull",
 "reasoning": "判定理由（1-2文）"}
"""

SECONDARY_REDUNDANCY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["matched_primary_article_id", "classification", "reasoning"],
    "properties": {
        "matched_primary_article_id": {"type": ["string", "null"]},
        "classification": {"type": ["string", "null"],
                            "enum": ["redundant_summary", "value_added_reporting",
                                     "value_added_analysis", None]},
        "reasoning": {"type": "string"},
    },
}


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def find_candidate_primary_articles(client: SupabaseClient, article: dict, tag_ids: list,
                                     window_days_before: int = 14, window_days_after: int = 3,
                                     limit: int = 5) -> list:
    """対象記事と同一イベントを扱っている可能性がある一次情報記事の候補を、
    タグ重複件数・公開日近接のヒューリスティックで絞り込む。正式なクラスタリング基盤は
    存在しないため（duplicate_of_article_url_idは書き込み経路が無い未実装のスタブ）、
    ここでの絞り込みはあくまで事前フィルタであり、最終的な同一性判定はLLMに委ねる。
    戻り値は空でもよく、その場合は呼び出し元がLLM呼び出しをスキップする"""
    pub_dt = _parse_dt(article.get("published_at"))
    if not pub_dt or not tag_ids:
        return []
    window_start = pub_dt - timedelta(days=window_days_before)
    window_end = pub_dt + timedelta(days=window_days_after)

    recent_articles = client.select("articles", {
        "select": "article_id,title,published_at",
        "is_current": "eq.true",
        "published_at": f"gte.{window_start.isoformat()}",
    })
    in_window = [
        a for a in recent_articles
        if a["article_id"] != article.get("article_id")
        and (a_dt := _parse_dt(a.get("published_at"))) and a_dt <= window_end
    ]
    if not in_window:
        return []

    candidate_ids = [a["article_id"] for a in in_window]
    analyses = common._select_in_chunks(client, "article_analysis", {
        "select": "article_id,summary_short",
        "primary_source_status": "eq.一次情報",
        "is_current": "eq.true",
        "analysis_status": "eq.処理済",
    }, "article_id", candidate_ids)
    summary_by_article = {a["article_id"]: a.get("summary_short") for a in analyses}
    primary_candidates = [
        {**a, "summary_short": summary_by_article[a["article_id"]]}
        for a in in_window if a["article_id"] in summary_by_article
    ]
    if not primary_candidates:
        return []

    tag_rows = common._select_in_chunks(client, "article_tags", {
        "select": "article_id,tag_id",
    }, "article_id", [a["article_id"] for a in primary_candidates])
    tag_set = set(tag_ids)
    overlap_by_article: dict = {}
    for r in tag_rows:
        if r["tag_id"] in tag_set:
            overlap_by_article[r["article_id"]] = overlap_by_article.get(r["article_id"], 0) + 1

    scored = [(overlap_by_article.get(a["article_id"], 0), a) for a in primary_candidates]
    scored = [(score, a) for score, a in scored if score > 0]
    scored.sort(key=lambda x: (x[0], x[1].get("published_at") or ""), reverse=True)
    return [a for _, a in scored[:limit]]


def build_redundancy_user_prompt(article: dict, candidates: list) -> str:
    lines = [
        f"【対象記事（二次記事の可能性がある記事）】",
        f"タイトル: {article.get('title') or ''}",
        f"公開日時: {article.get('published_at') or '不明'}",
        f"本文:\n{(article.get('extracted_text') or '')[:MAX_ARTICLE_CHARS]}",
        "",
        "【一次情報候補（機械的な事前絞り込み、別事象の可能性あり）】",
    ]
    for c in candidates:
        lines.append(f"- article_id: {c['article_id']}")
        lines.append(f"  タイトル: {c.get('title') or ''}")
        lines.append(f"  公開日時: {c.get('published_at') or '不明'}")
        lines.append(f"  要約: {c.get('summary_short') or ''}")
    return "\n".join(lines)


def classify_secondary_redundancy(azure_client, model: str, article: dict, candidates: list) -> dict:
    """common.call_llm_structuredを再利用。temperature未指定（一部モデルがtemperature=0を
    受け付けないため。同一性判定という性質上ブレは避けたいが、モデル互換性を優先する）"""
    user_prompt = build_redundancy_user_prompt(article, candidates)
    result = common.call_llm_structured(
        azure_client, model, SECONDARY_REDUNDANCY_SYSTEM_PROMPT, user_prompt,
        SECONDARY_REDUNDANCY_SCHEMA, "SecondaryRedundancyClassification")
    return result["data"]


VERIFY_SYSTEM_PROMPT = """あなたは一次情報の実在確認だけを行う検証専門AIです。
与えられた「確認すべき主張」について、Web検索ツールを使って、その主張の
根拠となる一次情報源（発表元の公式サイト、政府機関サイト、統計機関サイト等）
が実際に存在し、内容が一致するかを確認してください。

厳守事項:
- 必ずWeb検索ツールを1回以上呼び出すこと。検索せずに回答してはいけない。
- 検索クエリには、主張に含まれる固有名詞・数値・年月をできるだけ具体的に
  含め、一次情報源そのものにたどり着けるものにすること。
- 検索ツールは主張の裏取り以外の目的（計算・言い換え等）には使わないこと。
- 検索結果で主張と一致する一次情報源が見つからない場合は、無理に見つかった
  ことにせず verified=false とすること。

出力は次の2部構成にすること:
1. まず1〜3文の日本語のプレーンな説明文で、検索で確認できた内容と
   参照した情報源について、通常の文章として自然に言及すること
   （情報源のタイトル・発表機関名等に触れること。URLを文字列としてそのまま
   書き写す必要はない）。
2. 次に改行し、コードブロックでJSON1個のみを出力すること:
```json
{{"verified": true/false, "verification_note": "1の説明文の要約"}}
```

確認すべき主張: {claim}
"""


def build_user_prompt(article: dict, target: dict) -> str:
    text = (article.get("extracted_text") or "")[:MAX_ARTICLE_CHARS]
    return (
        f"タイトル: {article.get('title') or ''}\n"
        f"発信元: {target.get('publisher_name') or ''}（{target.get('domain') or ''}）\n"
        f"公開日時: {article.get('published_at') or '不明'}\n"
        f"URL: {article.get('final_url') or article.get('fetched_url') or ''}\n\n"
        f"本文:\n{text}"
    )


def _extract_json(output_text: str) -> dict:
    text = output_text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    return json.loads(text)


def _extract_evidence(resp) -> list:
    """responses APIのannotations(url_citation)のみをエビデンスとして拾う。
    本文中の自由記述URLは信用しない"""
    evidence = []
    seen_urls = set()
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for c in getattr(item, "content", []) or []:
            for ann in getattr(c, "annotations", None) or []:
                if getattr(ann, "type", None) != "url_citation":
                    continue
                url = getattr(ann, "url", None)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                evidence.append({"url": url, "title": getattr(ann, "title", None)})
    return evidence


def verify_claim(azure_client, model: str, claim: str) -> dict:
    """call2: 検索ツールの利用を強制し、主張(claim)の実在確認だけを行う"""
    resp = azure_client.responses.create(
        model=model,
        instructions=VERIFY_SYSTEM_PROMPT.format(claim=claim),
        input="上記の主張を検索で確認してください。",
        tools=[{"type": "web_search_preview"}],
        tool_choice="required",
    )
    data = _extract_json(resp.output_text)
    data["_evidence"] = _extract_evidence(resp)
    return data


CALL1_MAX_ATTEMPTS = 2  # 並列実行時、出力JSONが途中で切れて壊れることがあるため1回だけ再試行する


def analyze_article(azure_client, model: str, rubric_text: str, tag_text: str,
                     article: dict, target: dict, client: SupabaseClient = None) -> dict:
    # call1: 検索ツール無しでタグ付け・出典判定(暫定)・採点・検索要否判定
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(rubric_text=rubric_text, tag_text=tag_text)
    user_prompt = build_user_prompt(article, target)

    data = None
    last_error = None
    for _attempt in range(CALL1_MAX_ATTEMPTS):
        resp = azure_client.responses.create(
            model=model,
            instructions=system_prompt,
            input=user_prompt,
        )
        try:
            data = _extract_json(resp.output_text)
            break
        except json.JSONDecodeError as e:
            last_error = e
    if data is None:
        raise last_error
    data["_evidence"] = []

    # call2: call1がWeb検索確認を必要と判断した場合のみ、検証専用の呼び出しを行う
    claim = data.get("primary_source_claim")
    if data.get("needs_web_verification") and claim:
        try:
            verification = verify_claim(azure_client, model, claim)
        except Exception as e:
            verification = {"verified": False,
                             "verification_note": f"検証呼び出し失敗: {type(e).__name__}: {e}",
                             "_evidence": []}
        data["_verification"] = verification
        data["_evidence"] = verification.get("_evidence") or []
        if verification.get("verified"):
            data["primary_source_status"] = "一次照合済み"
        else:
            data["primary_source_status"] = "一次未確認"
        data["source_status_reason"] = (
            f"{data.get('source_status_reason', '')} "
            f"[Web検索確認] {verification.get('verification_note', '')}"
        ).strip()
        data["needs_review"] = bool(data.get("needs_review")) or not bool(verification.get("verified"))

    # 二次・解釈系記事についてのみ、同一イベントの一次情報候補を探し、見つかった場合だけ
    # 追加のLLM呼び出しで付加価値を判定する（全記事へLLM呼び出しを増やさないため、
    # 一次情報・出所不明の記事はこのブロック自体をスキップする）
    status = data.get("primary_source_status")
    if client is not None and status in ("一次照合済み", "解釈・分析", "一次未確認"):
        try:
            candidates = find_candidate_primary_articles(client, article, data.get("tags") or [])
            if candidates:
                redundancy = classify_secondary_redundancy(azure_client, model, article, candidates)
                data["matched_primary_article_id"] = redundancy.get("matched_primary_article_id")
                data["secondary_redundancy_classification"] = redundancy.get("classification")
                data["redundancy_reason"] = redundancy.get("reasoning")
        except Exception as e:
            # 失敗しても記事分析全体は止めない（Failure Isolation。既存のverify_claim失敗時と同じ方針）
            data["redundancy_reason"] = f"[判定失敗] {type(e).__name__}: {e}"

    return data


# ─── ランク計算（モデルに直接ランクを出させず、点数から機械的に決定） ──
def compute_rank(total_score: int, ranks: list) -> str:
    for r in ranks:
        if r["min_score"] <= total_score <= r["max_score"]:
            return r["rank"]
    return "D"


# ─── マテリアリティ接続タグの付与制約（PMO 2026-08-18フィードバック5章(2)） ──
# - 下位5軸(TH-10配下)のタグが付いた記事には、マテリアリティ接続タグを一切付与しない
# - 人的資本(TH-08)の記事は、制度面のA・H観点のみ許可する
SUB_AXIS_PARENT_TAG_ID = "TH-10"
HUMAN_CAPITAL_THEME_ID = "TH-08"
HUMAN_CAPITAL_ALLOWED_MATERIALITY_IDS = {"MT-A", "MT-H"}
MATERIALITY_AXIS = "マテリアリティ接続"


def _theme_major_of(tag_id: str, tag_by_id: dict) -> str | None:
    """tag_idがテーマ軸の（小分類含む）タグなら、対応する大分類のtag_idを返す。テーマ軸以外はNone"""
    seen = set()
    t = tag_by_id.get(tag_id)
    while t and t["tag_id"] not in seen:
        if t["tag_axis"] != "テーマ":
            return None
        if t["tag_level"] == "大分類":
            return t["tag_id"]
        seen.add(t["tag_id"])
        t = tag_by_id.get(t.get("parent_tag_id"))
    return None


def apply_materiality_constraint(tag_ids: list, tag_by_id: dict) -> list:
    themes = {_theme_major_of(t, tag_by_id) for t in tag_ids}
    has_sub_axis = any(
        tag_by_id.get(t, {}).get("parent_tag_id") == SUB_AXIS_PARENT_TAG_ID for t in tag_ids
    )
    is_human_capital = HUMAN_CAPITAL_THEME_ID in themes

    result = []
    for t in tag_ids:
        if tag_by_id.get(t, {}).get("tag_axis") != MATERIALITY_AXIS:
            result.append(t)
            continue
        if has_sub_axis:
            continue
        if is_human_capital and t not in HUMAN_CAPITAL_ALLOWED_MATERIALITY_IDS:
            continue
        result.append(t)
    return result


# ─── 保存 ─────────────────────────────────────────────────────────
def save_analysis(client: SupabaseClient, article: dict, result: dict, ranks: list,
                   model: str, valid_tag_ids: set, tag_by_id: dict) -> list:
    article_id = article["article_id"]
    now_iso = datetime.now(timezone.utc).isoformat()

    scores = {k: int(result.get("scores", {}).get(k, 0)) for k in SCORE_CRITERIA_ORDER}
    scores_raw = dict(scores)

    # Secondary Redundancy補正: 一次情報の要約・言い換えに留まると判定された二次記事のみ、
    # novelty/decision_valueを機械的に-1する（0未満にはしない）。既存のcorrection_applied
    # （LLM自己申告・人間レビュー待ち）とは別軸で、事実判定に基づく初めての機械補正
    redundancy_classification = result.get("secondary_redundancy_classification")
    reason_parts = [result.get("importance_reason") or ""]
    if redundancy_classification == "redundant_summary":
        for k in ("novelty", "decision_value"):
            scores[k] = max(0, scores[k] - 1)
        reason_parts.append("[機械補正] 一次情報の要約に留まる二次記事と判定されたため"
                             "novelty/decision_valueを各-1")

    total_score = sum(scores.values())
    rank = compute_rank(total_score, ranks)

    correction_applied = result.get("correction_applied")
    needs_review = bool(result.get("needs_review")) or bool(correction_applied)

    default_publication = next((r["default_publication"] for r in ranks if r["rank"] == rank), None)

    if result.get("source_status_reason"):
        reason_parts.append(f"[出典判定] {result['source_status_reason']}")
    if correction_applied:
        reason_parts.append(f"[補正該当] {correction_applied}")

    representative_role = {
        "redundant_summary": "suppressed_duplicate",
        "value_added_reporting": "additional_reporting",
        "value_added_analysis": "supporting_analysis",
    }.get(redundancy_classification, "representative")

    client.insert("article_analysis", [{
        "article_id": article_id,
        "summary_short": result.get("summary_short"),
        "importance_level": rank,
        "importance_reason": "\n".join(p for p in reason_parts if p),
        "importance_total_score": total_score,
        "importance_scores": scores,
        "importance_scores_raw": scores_raw,
        "publication_category": default_publication,
        "needs_review": needs_review,
        "primary_source_status": result.get("primary_source_status"),
        "secondary_redundancy_classification": redundancy_classification,
        "matched_primary_article_id": result.get("matched_primary_article_id"),
        "redundancy_reason": result.get("redundancy_reason"),
        "representative_role": representative_role,
        "analysis_status": "処理済",
        "model_name": model,
        "prompt_version": PROMPT_VERSION,
    }], prefer="return=minimal")

    # タグ付与（AI割当分のみ入れ替え。人手付与分(assigned_by='人手')は残す）
    client.delete("article_tags", {"article_id": f"eq.{article_id}", "assigned_by": "eq.AI"})
    # モデルが同じtag_idを重複して返す/存在しないtag_idを返すことがあるため、
    # 重複除去（順序は保持）＋実在するタグへの絞り込みを行う
    # （存在しないtag_idのままinsertするとFK制約違反で保存全体が失敗するため）
    raw_tag_ids = list(dict.fromkeys(result.get("tags") or []))
    tag_ids = [t for t in raw_tag_ids if t in valid_tag_ids]
    dropped = [t for t in raw_tag_ids if t not in valid_tag_ids]
    if dropped:
        print(f"    [警告] 未知のtag_idを無視: {dropped}")
    tag_ids = apply_materiality_constraint(tag_ids, tag_by_id)
    if tag_ids:
        client.insert("article_tags", [{
            "article_id": article_id,
            "tag_id": tid,
            "assigned_by": "AI",
            "assignment_reason": f"{model}による自動判定（{PROMPT_VERSION}）",
        } for tid in tag_ids], prefer="return=minimal")

    # エビデンス保存（構造化引用のみ）
    evidence = result.get("_evidence") or []
    if evidence:
        client.insert("article_evidence", [{
            "article_id": article_id,
            "evidence_url": e["url"],
            "title": e.get("title"),
            "relevance_note": result.get("source_status_reason"),
        } for e in evidence], prefer="return=minimal")

    return tag_ids


def save_content_filtered_stub(client: SupabaseClient, article: dict, model: str, reason: str) -> None:
    """Azure OpenAIのコンテンツフィルター（暴力・性的表現等）に引っかかり自動分析できなかった
    記事用のスタブ。記事内容起因のブロックは再実行しても必ず同じ理由で失敗するため、
    analysis_status='要確認'（既存のCHECK制約の値をそのまま利用）で保存し、
    以後の実行で毎回リトライされるのを防ぐ。人による確認・タグ付けが必要な状態として残る。"""
    client.insert("article_analysis", [{
        "article_id": article["article_id"],
        "importance_reason": f"[分析不可] Azure OpenAIコンテンツフィルターにより自動分析不可。{reason}",
        "analysis_status": "要確認",
        "model_name": model,
        "prompt_version": PROMPT_VERSION,
    }], prefer="return=minimal")


def delete_filtered_article(client: SupabaseClient, article_id: str) -> None:
    """軽量キーワードフィルタを通過しなかった記事を物理削除する（子テーブルを先に削除して
    から本体を消す）。article_urlsは残し、次回同一URLクロール時の重複防止・
    last_detected_at追跡は維持する。

    2026-08-27現在: **この関数は_prefilter_articles()から呼ばれていない**（下記
    save_keyword_filtered_stub参照）。フィルタ語彙(filter_keywords)がtag_referenceから
    機械的に導出されておらず独立管理のため（過去に「ジェンダー」等の捕捉漏れが実際に
    発生した）、tag_reference由来の導出が完了し語彙の網羅性が確認できるまでは、
    記事本体を物理削除する運用を本番化しない方針とした。関数自体は将来の再有効化に
    備えて残す。

    2026-08-28追記: filter_keyword_tag_mapとcheck_filter_keyword_coverage.pyによる
    tag_reference正本化のカバレッジ保証機構を導入した後も、本関数の再有効化には
    以下の両方をGate条件として必須とする。
        1. Coverage Check NG（check_filter_keyword_coverage.pyのholesが1件でもある）
           時は、article_filter.pass_filter()によるキーワードフィルタリング自体を
           実行しない（全件analyzeへ通す）フェイルセーフを実装すること
        2. 有効なtag_referenceについてfilter_coverage_policyの棚卸しが完了し、
           unreviewedが0件であること（check_filter_keyword_coverage.pyのモジュール
           docstringにも同内容を明記している）"""
    client.delete("article_tags", {"article_id": f"eq.{article_id}"})
    client.delete("article_analysis", {"article_id": f"eq.{article_id}"})
    client.delete("article_files", {"article_id": f"eq.{article_id}"})
    client.delete("articles", {"article_id": f"eq.{article_id}"})


def save_keyword_filtered_stub(client: SupabaseClient, article: dict) -> None:
    """軽量キーワードフィルタ（関連キーワード不一致）で対象外と判定された記事のスタブを
    保存する。記事本体（articles/article_tags/article_files）は削除せずそのまま残す
    （案Aへ復帰。理由はdelete_filtered_article()のdocstring参照）。article_analysisへ
    analysis_status='フィルタ除外'（既存のCHECK制約の値をそのまま利用）のスタブのみ記録し、
    fetch_unanalyzed_articles()のis_current判定により以後の再分析対象から除外する。"""
    client.insert("article_analysis", [{
        "article_id": article["article_id"],
        "importance_reason": "軽量キーワードフィルタ（関連キーワード不一致）により対象外と判定",
        "analysis_status": "フィルタ除外",
    }], prefer="return=minimal")


# ─── 対象記事の抽出 ─────────────────────────────────────────────
def fetch_unanalyzed_articles(client: SupabaseClient, limit: int = None) -> list:
    articles = client.select("articles", {
        "select": "article_id,title,extracted_text,published_at,final_url,fetched_url,crawl_target_id",
        "is_current": "eq.true",
    })
    analyzed = client.select("article_analysis", {"select": "article_id", "is_current": "eq.true"})
    analyzed_ids = {a["article_id"] for a in analyzed}
    targets = {t["crawl_target_id"]: t for t in client.select(
        "crawl_targets", {"select": "crawl_target_id,publisher_name,domain,publisher_tag_id"})}

    result = []
    for a in articles:
        if a["article_id"] in analyzed_ids:
            continue
        a["_target"] = targets.get(a["crawl_target_id"], {})
        result.append(a)
        if limit and len(result) >= limit:
            break
    return result


def save_filter_exclusion_log(client: SupabaseClient, checked_by_tag: dict, excluded_by_tag: dict) -> None:
    """軽量キーワードフィルタの除外件数を出典区分(publisher_tag_id)別に記録する
    （記事本体・タイトル等は一切含めない、件数のみ）。四半期レビューで除外率の急変
    （語彙の劣化・ソース構成変化の兆候）を確認できるようにするためのログ。
    週次集計はfilter_exclusion_log.run_dateをクエリ側でまとめて行う想定
    （バッチの実行タイミング・頻度に依存しないようにするため）。

    Failure Isolation: このログ保存はあくまで補助的な監査機能であり、
    （例: sql/2026-08-27_filter_exclusion_log_schema.sqlが未適用でテーブルが無い場合等）
    失敗してもLLM分析パイプライン本体（_prefilter_articles以降）を絶対に止めない。"""
    rows = [
        {
            "publisher_tag_id": tag_id,
            "excluded_count": excluded_by_tag.get(tag_id, 0),
            "total_checked_count": checked_count,
        }
        for tag_id, checked_count in checked_by_tag.items()
    ]
    if not rows:
        return
    try:
        client.insert("filter_exclusion_log", rows, prefer="return=minimal")
    except Exception as e:
        print(f"[filter_exclusion_log] 除外件数ログの保存に失敗しました（分析処理は継続します）: "
              f"{type(e).__name__}: {e}")


# ─── メイン ───────────────────────────────────────────────────────
def _prefilter_articles(client: SupabaseClient, media_tag_ids: set, articles: list) -> list:
    """タグ付け・重要度判定の前段階として、軽量キーワードフィルタ（メディア・データ提供機関
    カテゴリのみ先行導入）を全件に適用する。通過しなかった記事はスタブ保存のみ行い
    （記事本体は削除しない、save_keyword_filtered_stub参照）、LLM分析フェーズには
    渡さない。戻り値はフィルタを通過した記事のリスト。

    2026-08-27: フィルタ語彙(filter_keywords)がtag_referenceから機械的に導出されておらず
    独立管理であることが判明（過去に「ジェンダー」等の捕捉漏れが実際に発生）。
    tag_reference由来の導出が完了し語彙の網羅性が確認できるまでは、フィルタ除外記事を
    完全非保存（物理削除）にする運用を本番化しない方針とし、物理削除から
    スタブ保存（記事本体は残す）へ変更した。

    除外件数は出典区分(publisher_tag_id)別に集計し、filter_exclusion_logへ記録する
    （PMOレビュー依頼対応。件数集計のみで記事本体は含めない）。"""
    remaining = []
    filtered_out = 0
    checked_by_tag: dict = {}
    excluded_by_tag: dict = {}
    for article in articles:
        title = (article.get("title") or "")[:40]
        target_tag_id = (article.get("_target") or {}).get("publisher_tag_id")
        if target_tag_id in media_tag_ids:
            checked_by_tag[target_tag_id] = checked_by_tag.get(target_tag_id, 0) + 1
            passed, matched_keyword = article_filter.pass_filter(title, article.get("extracted_text") or "")
            if not passed:
                save_keyword_filtered_stub(client, article)
                filtered_out += 1
                excluded_by_tag[target_tag_id] = excluded_by_tag.get(target_tag_id, 0) + 1
                continue
        remaining.append(article)
    if checked_by_tag:
        save_filter_exclusion_log(client, checked_by_tag, excluded_by_tag)
    print(f"フィルタ除外（削除）: {filtered_out}件 / 分析対象: {len(remaining)}件")
    return remaining


def _process_one_article(azure_client, model, rubric_text, tag_text, ranks, valid_tag_ids,
                          tag_by_id, client: SupabaseClient, article: dict) -> tuple:
    """記事1件分のLLM分析（タグ付け・重要度採点）。並列実行される単位。
    フィルタ判定は_prefilter_articlesで完了済みの前提。
    戻り値は(タイトル先頭40文字, 結果メッセージ)。"""
    title = (article.get("title") or "")[:40]
    try:
        result = analyze_article(azure_client, model, rubric_text, tag_text,
                                  article, article["_target"], client=client)
        saved_tag_ids = save_analysis(client, article, result, ranks, model, valid_tag_ids, tag_by_id)
        scores = result.get("scores", {})
        total = sum(int(scores.get(k, 0)) for k in SCORE_CRITERIA_ORDER)
        rank = compute_rank(total, ranks)
        return title, (f"完了 rank={rank}({total}点) tags={len(saved_tag_ids)}件"
                        f" 出典={result.get('primary_source_status')} evidence={len(result.get('_evidence') or [])}件")
    except Exception as e:
        err_str = f"{type(e).__name__}: {e}"
        if "content_filter" in err_str:
            save_content_filtered_stub(client, article, model, err_str)
            return title, "分析不可（コンテンツフィルタ）: 要確認としてスタブ保存・以後リトライ対象外"
        return title, f"エラー: {err_str}"


def main(limit: int = 10, max_workers: int = 4):
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if not azure_client:
        print("Azure OpenAI / OpenAI のAPIキーが設定されていません")
        return

    print("ルール・タグを読み込み中...")
    rubric_text = load_rubric_text(client)
    tags = load_tags(client)
    tag_text = build_tag_text(tags)
    valid_tag_ids = {t["tag_id"] for t in tags}
    tag_by_id = {t["tag_id"]: t for t in tags}
    ranks = client.select("importance_rank_definitions", {"select": "*"})

    media_tag_ids = article_filter.fetch_media_tag_ids(client)
    article_filter.load_keywords(client)

    articles = fetch_unanalyzed_articles(client, limit=limit)
    print(f"対象（フィルタ判定前）: {len(articles)}件")

    # フェーズ1: 軽量キーワードフィルタで除外対象を先に削除する
    articles = _prefilter_articles(client, media_tag_ids, articles)

    # フェーズ2: 残った記事のみLLM分析（タグ付け・重要度採点）を並列実行する
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_process_one_article, azure_client, model, rubric_text, tag_text,
                             ranks, valid_tag_ids, tag_by_id, client, article)
            for article in articles
        ]
        for future in as_completed(futures):
            completed += 1
            title, msg = future.result()
            print(f"[{completed}/{len(articles)}] {title} ... {msg}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    main(limit=n, max_workers=workers)
