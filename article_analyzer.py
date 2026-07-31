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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import article_filter  # noqa: E402

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
        "select": "tag_id,tag_axis,tag_name,tag_meaning,tag_criteria",
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
9. summary_short: 記事の要約（100文字以内）
10. importance_reason: 総合的な重要度判断の理由（2-3文）
11. needs_review: 人間の確認が望ましい場合true（例: 判定に自信が無い、内容が
    センシティブ、補正ルールとスコアが矛盾する等）

{rubric_text}

{tag_text}
"""

# call2: primary_source_claim の実在確認だけを行う専用プロンプト。
# tool_choice="required" と組み合わせて、検索ツールの呼び出しを強制する
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


def analyze_article(azure_client, model: str, rubric_text: str, tag_text: str,
                     article: dict, target: dict) -> dict:
    # call1: 検索ツール無しでタグ付け・出典判定(暫定)・採点・検索要否判定
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(rubric_text=rubric_text, tag_text=tag_text)
    user_prompt = build_user_prompt(article, target)

    resp = azure_client.responses.create(
        model=model,
        instructions=system_prompt,
        input=user_prompt,
    )
    data = _extract_json(resp.output_text)
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

    return data


# ─── ランク計算（モデルに直接ランクを出させず、点数から機械的に決定） ──
def compute_rank(total_score: int, ranks: list) -> str:
    for r in ranks:
        if r["min_score"] <= total_score <= r["max_score"]:
            return r["rank"]
    return "D"


# ─── 保存 ─────────────────────────────────────────────────────────
def save_analysis(client: SupabaseClient, article: dict, result: dict, ranks: list,
                   model: str, valid_tag_ids: set) -> list:
    article_id = article["article_id"]
    now_iso = datetime.now(timezone.utc).isoformat()

    scores = {k: int(result.get("scores", {}).get(k, 0)) for k in SCORE_CRITERIA_ORDER}
    total_score = sum(scores.values())
    rank = compute_rank(total_score, ranks)

    correction_applied = result.get("correction_applied")
    needs_review = bool(result.get("needs_review")) or bool(correction_applied)

    default_publication = next((r["default_publication"] for r in ranks if r["rank"] == rank), None)

    reason_parts = [result.get("importance_reason") or ""]
    if result.get("source_status_reason"):
        reason_parts.append(f"[出典判定] {result['source_status_reason']}")
    if correction_applied:
        reason_parts.append(f"[補正該当] {correction_applied}")

    client.insert("article_analysis", [{
        "article_id": article_id,
        "summary_short": result.get("summary_short"),
        "importance_level": rank,
        "importance_reason": "\n".join(p for p in reason_parts if p),
        "importance_total_score": total_score,
        "importance_scores": scores,
        "publication_category": default_publication,
        "needs_review": needs_review,
        "primary_source_status": result.get("primary_source_status"),
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


def save_filtered_stub(client: SupabaseClient, article: dict) -> None:
    """軽量キーワードフィルタを通過しなかった記事用のスタブ行。
    記事本体(articles)は保存されたまま残し、LLM分析は行わず
    analysis_status='フィルタ除外'のみ記録する（フィルタールール_たたき台_20260720.md 3.4節 案A）。"""
    client.insert("article_analysis", [{
        "article_id": article["article_id"],
        "analysis_status": "フィルタ除外",
        "importance_reason": "[軽量フィルタ] メディア・データ提供機関カテゴリのキーワードフィルタに1件もマッチしなかったため除外",
        "prompt_version": PROMPT_VERSION,
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


# ─── メイン ───────────────────────────────────────────────────────
def main(limit: int = 10):
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
    ranks = client.select("importance_rank_definitions", {"select": "*"})

    media_tag_ids = article_filter.fetch_media_tag_ids(client)
    article_filter.load_keywords(client)

    articles = fetch_unanalyzed_articles(client, limit=limit)
    print(f"分析対象: {len(articles)}件")

    for i, article in enumerate(articles, 1):
        title = (article.get("title") or "")[:40]
        print(f"[{i}/{len(articles)}] {title} ...", end=" ", flush=True)

        # 軽量キーワードフィルタ（メディア・データ提供機関カテゴリのみ先行導入）。
        # 通過しなければLLM分析を行わずスタブだけ残す（案A、記事本体は保存されたまま）。
        target_tag_id = (article.get("_target") or {}).get("publisher_tag_id")
        if target_tag_id in media_tag_ids:
            passed, matched_keyword = article_filter.pass_filter(title, article.get("extracted_text") or "")
            if not passed:
                save_filtered_stub(client, article)
                print("フィルタ除外")
                continue

        try:
            result = analyze_article(azure_client, model, rubric_text, tag_text,
                                      article, article["_target"])
            saved_tag_ids = save_analysis(client, article, result, ranks, model, valid_tag_ids)
            scores = result.get("scores", {})
            total = sum(int(scores.get(k, 0)) for k in SCORE_CRITERIA_ORDER)
            rank = compute_rank(total, ranks)
            print(f"完了 rank={rank}({total}点) tags={len(saved_tag_ids)}件"
                  f" 出典={result.get('primary_source_status')} evidence={len(result.get('_evidence') or [])}件")
        except Exception as e:
            print(f"エラー: {type(e).__name__}: {e}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(limit=n)
