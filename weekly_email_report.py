"""
週次メールレポート生成モジュール

サステナビリティ専門家AI（sustainability_article_selector.py）がテーマ大分類ごとの
上位N件＋マテリアリティ接続タグワイルドカードで絞り込み、publish_candidate/watch_or_archive/
not_selectedを判定した結果から、list_weekly_picks()でtotal_score上位15〜20件（config管理、
weekly_digest.target_min/target_max）を機械的に選び、以下の構成でHTMLメール本文を組み立てる。

    1. 冒頭: 専門家示唆（今週の概況＋注目ポイント）
    2. 記事紹介: 15〜20件程度（No./テーマ/見出し・要点/タグ/重要度）
    3. テーマ別ダイジェスト（今週の読み筋＝テーマトピックス）

記事ごとの「見出し・要点」（事実＋サス推への示唆）と、冒頭の概況・注目ポイント・
テーマ別ダイジェストは、週次メール専用にLLMで書き直す
（sustainability_expert_common.call_llm_structuredを再利用）。

2026-07-22の統合で、記事ごとの詳細コンテンツ生成(旧sustainability_content_generator.py、
_archive/へ移動)はこのテーマ別ダイジェストに統合され、送信前に人がレビュー・承認する
ゲートを追加した（weekly_email_reports.review_status: review_required→approved/rejected→sent）。

2026-08-24追記: 選定された15〜20件（週次メール掲載対象）に限り、見出し・要点生成と
同じLLM呼び出しで詳細ページ（第2層）用の構造化サマリー（decision_facts/applicability/
effective_date/transition_period/key_numbers/background_context/suntory_impact/
summary_detail）も生成し、article_analysisへ保存する（save_detail_summary）。
記事全件に対する別呼び出しは行わず、既存の呼び出しに相乗りさせることでLLMコスト・
時間の増加を避けている。選定外（B/C判定でメール非掲載）の記事はこの詳細サマリーを
生成しない。
サステナビリティ担当者がsustainability_expert_dashboard.pyでレビュー・承認して初めて
実際の送信が行われる（build()とsend()の2フェーズに分離）。

送信方式（社内SMTPリレー／M365／SendGridのいずれか）が未確定のため、送信処理は
標準ライブラリのsmtplibで実装しつつ、config.json に email.smtp_host が
設定されていない場合は自動的にプレビュー用HTMLファイルへ保存する。

使い方:
    python weekly_email_report.py build [--since-days N]           # ドラフト生成→review_requiredで保存（送信しない）
    python weekly_email_report.py send --report-id <id> [--reviewer-id <id>]  # 承認済みドラフトを送信
                                                                     # （主経路はダッシュボードの承認ボタン。
                                                                     #   CLIのsendは運用上のフォールバック）
"""
import argparse
import json
import re
import smtplib
import sys

import requests
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
import sustainability_article_selector as selector  # noqa: E402
import weekly_geo_intelligence_service  # noqa: E402
from weekly_geo_intelligence_service import is_weekly_geo_enabled  # noqa: E402
import strategic_question_service as sqs  # noqa: E402
import decision_insight_service as insight_service  # noqa: E402
import send_state_machine as ssm  # noqa: E402
import identity  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).parent
CACHE_DIR = BASE / "cache"

MAX_ARTICLE_CHARS = 3000       # 見出し・要点生成に使う本文の上限文字数
PROMPT_VERSION = "weekly-digest-v0.1"


# ─── 対象記事の抽出 ─────────────────────────────────────────────
def build_articles_from_picks(client, picks: list, all_urls: dict = None) -> list:
    """selector.list_weekly_picks()の結果から、rewrite_article_for_digestに渡す記事dict一覧を
    組み立てる。common.get_cluster()で代表記事を解決し、common.categorize_tags()でタグ分類を
    付与する。article_analysis.importance_level（表示用、既存のarticle_analyzer.py採点をそのまま
    踏襲）と、selectorのassessment（total_score/selection_reasons/evidence）を書き換え
    プロンプトへの補強材料としてenrichmentで追加する。total_score降順で返す"""
    if all_urls is None:
        all_urls = common.fetch_all_article_urls(client)

    resolved = []
    for pick in picks:
        cluster = common.get_cluster(client, pick["article_cluster_id"], all_urls=all_urls)
        if cluster is None:
            continue
        resolved.append((pick, cluster["representative"]))
    if not resolved:
        return []

    article_ids = [rep["article_id"] for _, rep in resolved]
    analyses = {a["article_id"]: a for a in client.select("article_analysis", {
        "select": "article_id,importance_level,importance_reason,summary_short",
        "article_id": f"in.({','.join(article_ids)})",
        "is_current": "eq.true",
    })}
    tag_rows = client.select("article_tags", {
        "select": "article_id,tag_id", "article_id": f"in.({','.join(article_ids)})",
    })
    tag_ref_by_id = {t["tag_id"]: t for t in client.select(
        "tag_reference", {"select": "tag_id,tag_axis,tag_level,tag_code,tag_name,parent_tag_id"})}
    tags_by_article = defaultdict(list)
    for r in tag_rows:
        tags_by_article[r["article_id"]].append(r["tag_id"])

    result = []
    for pick, rep in resolved:
        analysis = analyses.get(rep["article_id"], {})
        categorized = common.categorize_tags(tags_by_article.get(rep["article_id"], []), tag_ref_by_id)
        assessment = pick.get("assessment") or {}
        result.append({
            "article_id": rep["article_id"],
            "article_cluster_id": pick["article_cluster_id"],
            "title": rep.get("title") or "(無題)",
            "extracted_text": rep.get("extracted_text") or "",
            "url": rep.get("final_url") or rep.get("fetched_url") or "",
            "published_at": rep.get("published_at"),
            "publisher": rep.get("_target", {}).get("publisher_name", ""),
            "importance_level": analysis.get("importance_level") or "",
            "summary_short": analysis.get("summary_short") or "",
            "importance_reason": analysis.get("importance_reason") or "",
            **categorized,
            "selector_total_score": pick.get("total_score"),
            "selector_decision": pick.get("decision"),
            "selector_selection_reasons": assessment.get("selection_reasons") or [],
            "selector_evidence": assessment.get("evidence") or [],
        })

    result.sort(key=lambda r: r.get("selector_total_score") or 0, reverse=True)
    return result


# 後方互換エイリアス（tests/test_weekly_email_report.py が wer._categorize_tags を直接参照するため）
_categorize_tags = common.categorize_tags


# ─── 記事ごとの「見出し・要点」書き換え（LLM、週次メール専用） ─────────
# 週次メール本文用の見出し・要点に加えて、詳細ページ（第2層。選定された記事のみ対象、
# article_analysisのdecision_facts等へ保存）用の構造化サマリーも同じ1回の呼び出しで
# 生成する。記事全件（週数千件）に対して別途LLM呼び出しを行うとコスト・時間が
# 膨らむため、既存の週次選定15〜20件向け呼び出しに相乗りさせ、追加呼び出しを増やさない。
ARTICLE_REWRITE_SYSTEM_PROMPT = """あなたは当社サステナビリティ専門家です。
1件の記事について、サステナビリティ担当者(サス推)向け週次メールに掲載する
「見出し」と「要点」、および詳細ページ（週次メールには載らず、記事をクリックした
場合のみ表示される）用の構造化サマリーを作成してください。

要点(summary)の書き方:
- 記事で確定している事実を簡潔に述べる
- そのうえで、サス推が確認・対応すべき観点を「サス推では〜」等の表現で、
  事実と区別できる形で付け加える
- 会社の正式見解として断定しない。推測は推測と分かる書き方にする
- 全体で2〜3文、130文字程度を目安にする
- 出典にない数値・日付・義務を補完しない

詳細サマリーの各項目（すべて、記事に明記された内容のみを書くこと。記事に無い情報や
推測で数値・日付・義務を補完してはいけない。該当情報が記事に無ければ空文字列にする）:
- summary_detail: 記事の詳しめの概要（3〜5文）
- decision_facts: 決定事項・確定した事実。「・」始まりの箇条書きを改行区切りで（複数可）
- applicability: 適用範囲・対象（誰に/何に適用されるか）
- effective_date: 施行日・発効日
- transition_period: 移行期間・経過措置
- key_numbers: 重要な数値・基準値・罰則等。「・」始まりの箇条書きを改行区切りで（複数可）
- background_context: 背景・先行動向との関係（2〜3文）
- suntory_impact: サス推が確認すべき観点。「サス推では〜」等、事実と区別できる書き方にし、
  会社の正式見解として断定しない（summaryのサス推言及と重複してよい）

出力はJSON1個のみ:
{"headline": "記事の見出し(20文字程度。【】は付けない)", "summary": "要点(事実+サス推への示唆)",
 "summary_detail": "...", "decision_facts": "...", "applicability": "...",
 "effective_date": "...", "transition_period": "...", "key_numbers": "...",
 "background_context": "...", "suntory_impact": "..."}
"""

# 詳細サマリー8項目。additionalProperties:Falseだがrequiredには含めない
# （既存のheadline/summaryのみを返す呼び出し元・テストとの後方互換のため）。
# 実運用では未指定時に空文字列として扱う（save_detail_summary参照）。
DETAIL_SUMMARY_FIELDS = [
    "summary_detail", "decision_facts", "applicability", "effective_date",
    "transition_period", "key_numbers", "background_context", "suntory_impact",
]

ARTICLE_REWRITE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "summary"],
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        **{f: {"type": "string"} for f in DETAIL_SUMMARY_FIELDS},
    },
}


def rewrite_article_for_digest(azure_client, model: str, expert_base: dict, article: dict,
                                usage_log: list = None) -> dict:
    selection_reasons = article.get("selector_selection_reasons") or []
    reasons_line = f"専門家AIによる選定理由: {'; '.join(selection_reasons)}\n" if selection_reasons else ""
    user_prompt = (
        f"# company_context\n{json.dumps(expert_base.get('company_context', {}), ensure_ascii=False)}\n\n"
        f"# article\n"
        f"title: {article['title']}\n"
        f"themes: {', '.join(article['themes'])}\n"
        f"既存要約: {article.get('summary_short') or article.get('importance_reason') or ''}\n"
        f"{reasons_line}\n"
        f"本文:\n{(article.get('extracted_text') or '')[:MAX_ARTICLE_CHARS]}\n"
    )
    result = common.call_llm_structured(
        azure_client, model, ARTICLE_REWRITE_SYSTEM_PROMPT, user_prompt,
        ARTICLE_REWRITE_SCHEMA, "WeeklyDigestArticleRewrite")
    if usage_log is not None:
        usage_log.append({"token_usage": result["token_usage"], "latency_ms": result["latency_ms"]})
    return result["data"]


def save_detail_summary(client, article_id: str, rewrite: dict) -> None:
    """rewrite_article_for_digest()の出力のうち、詳細ページ（第2層）用フィールドを
    article_analysis（既存のis_current=true行）へ保存する。週次メール選定対象
    （15〜20件）のみに適用し、全記事への一括生成は行わない（コスト抑制のため）。
    未指定のフィールドは空文字列として扱う（effective_dateのみ、日付として
    パースできない自由記述（例:「2027年1月から」）が多いためNoneにする。
    sql/2026-08-24_article_analysis_effective_date_to_text.sql適用後はtext型に
    なりそのまま自由記述を保存できるが、未適用でも空更新自体は失敗しないようにする）"""
    patch = {f: (rewrite.get(f) or "") for f in DETAIL_SUMMARY_FIELDS}
    patch["effective_date"] = rewrite.get("effective_date") or None
    try:
        client.update("article_analysis", {"article_id": f"eq.{article_id}", "is_current": "eq.true"}, patch)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400 and "type date" in e.response.text:
            # マイグレーション未適用（effective_dateがまだdate型）: 該当項目を除いて再試行する
            patch.pop("effective_date", None)
            client.update("article_analysis", {"article_id": f"eq.{article_id}", "is_current": "eq.true"}, patch)
        else:
            raise


# ─── 週次シンセシス（概況・注目ポイント・テーマ別ダイジェスト） ────────
WEEKLY_SYNTHESIS_SYSTEM_PROMPT = """あなたは当社サステナビリティ専門家です。
今週選定された記事一覧（見出し・要点・テーマ）を踏まえて、週次メール冒頭に載せる
「概況」「今週の注目ポイント」と、テーマ別の「今週の読み筋」を作成してください。

- 概況: 今週全体の傾向を2〜4文で。個別記事の羅列ではなく、複数記事から共通して
  読み取れる流れを述べる
- 今週の注目ポイント: テーマ横断で3〜6個、それぞれ1文
- テーマ別の読み筋: 記事が存在するテーマごとに1〜2文。担当者が何を確認すべきかを含める
- 断定は避け、「〜の動きが増加」「〜が論点」等、複数記事から読み取れる傾向として書く
- 記事に無い情報を補完しない

出力はJSON1個のみ:
{"overview": "...", "highlights": ["...", "..."], "theme_digests": [{"theme": "水", "digest": "..."}]}
"""

WEEKLY_SYNTHESIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview", "highlights", "theme_digests"],
    "properties": {
        "overview": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}},
        "theme_digests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["theme", "digest"],
                "properties": {
                    "theme": {"type": "string"},
                    "digest": {"type": "string"},
                },
            },
        },
    },
}


def build_weekly_synthesis(azure_client, model: str, expert_base: dict, rewritten_articles: list,
                            usage_log: list = None, theme_background_context: dict = None) -> dict:
    """theme_background_context（Phase S2追加）: Geo Intelligence由来のrelated_context itemのうち
    final_articlesにマッチしなかったもの（テーマ名→item一覧）。既存SYSTEM_PROMPTは変更せず、
    user_promptの材料に数行追加するのみ"""
    if not rewritten_articles:
        return {"overview": "", "highlights": [], "theme_digests": []}

    articles_text = "\n".join(
        f"- [{', '.join(a['themes'])}] {a['headline']}: {a['summary']}"
        for a in rewritten_articles
    )
    theme_bg_text = ""
    if theme_background_context:
        lines = []
        for theme, items in theme_background_context.items():
            for item in items[:3]:
                item_title = item.get("title") or ""
                assessment = (item.get("geo_assessment") or item.get("why_relevant") or "")[:200]
                lines.append(f"- [{theme}] {item_title}: {assessment}")
        if lines:
            theme_bg_text = ("\n\n# theme_background（地政学的背景情報。Geo Intelligence由来、"
                              "Theme Digestの参考材料。記事一覧には含まれない）\n" + "\n".join(lines))
    user_prompt = (
        f"# company_context\n{json.dumps(expert_base.get('company_context', {}), ensure_ascii=False)}\n\n"
        f"# 今週選定された記事一覧\n{articles_text}\n"
        f"{theme_bg_text}"
    )
    result = common.call_llm_structured(
        azure_client, model, WEEKLY_SYNTHESIS_SYSTEM_PROMPT, user_prompt,
        WEEKLY_SYNTHESIS_SCHEMA, "WeeklyDigestSynthesis")
    if usage_log is not None:
        usage_log.append({"token_usage": result["token_usage"], "latency_ms": result["latency_ms"]})
    return result["data"]


# ─── 実行ログ（weekly_email_reports） ───────────────────────────────
def _aggregate_usage(usage_log: list) -> tuple:
    """usage_logから合計トークン使用量と合計レイテンシを算出する"""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reasoning_tokens": 0}
    total_latency_ms = 0
    for entry in usage_log:
        token_usage = entry.get("token_usage") or {}
        for key in totals:
            totals[key] += token_usage.get(key, 0)
        total_latency_ms += entry.get("latency_ms") or 0
    return totals, total_latency_ms


def save_draft_report(client: SupabaseClient, *, period_start, period_end, article_ids: list,
                       model_deployment: str, token_usage: dict, latency_ms: int,
                       status: str, error_message: str, subject: str,
                       draft_content: dict, html_body: str) -> str:
    """weekly_email_reportsに新規1行(review_status='review_required')保存し、report_idを返す。
    テーブル未作成等で保存に失敗した場合はNoneを返す（旧log_weekly_reportと同様、警告print止まりで
    フローを止めない。ただしreport_idが無いとレビュー画面に出せないため、呼び出し側で確認すること）"""
    try:
        rows = client.insert("weekly_email_reports", [{
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "article_ids": article_ids,
            "model_deployment": model_deployment,
            "prompt_version": PROMPT_VERSION,
            "token_usage": token_usage,
            "latency_ms": latency_ms,
            "status": status,
            "error_message": error_message,
            "subject": subject,
            "draft_content": draft_content,
            "html_body": html_body,
            "review_status": "review_required",
        }])
        return rows[0]["report_id"] if rows else None
    except Exception as e:
        print(f"  [警告] weekly_email_reportsへのドラフト保存に失敗しました"
              f"（sql/2026-07-22_weekly_email_review_gate_schema.sql の適用が必要な可能性があります）: "
              f"{type(e).__name__}: {e}")
        return None


def get_report(client: SupabaseClient, report_id: str) -> dict:
    """1件取得（ダッシュボードのカード表示・承認処理用）"""
    rows = client.select("weekly_email_reports", {"report_id": f"eq.{report_id}", "limit": "1"})
    return rows[0] if rows else None


def list_reports(client: SupabaseClient, review_status: str = None) -> list:
    """ダッシュボード一覧用。review_status未指定なら全件、period_end降順"""
    params = {"select": "*", "order": "period_end.desc"}
    if review_status:
        params["review_status"] = f"eq.{review_status}"
    return client.select("weekly_email_reports", params)


def save_draft_edits(client: SupabaseClient, report_id: str, draft_content: dict, since_days: int) -> None:
    """レビュー画面での修正保存用。draft_contentを保存し、build_email()でsubject/html_bodyを
    再生成してあわせて保存する（review_statusは変えない）。
    draft_content["strategic_question"]（PMOが編集した場合はtitle/question_text/options）が
    あれば、埋め込み元のsustainability_strategic_questionsテーブルへも反映する
    （このメール表示用の編集と、質問自体のレコードを同期させる）"""
    rewritten = draft_content.get("articles", [])
    synthesis = draft_content.get("synthesis", {})
    strategic_question = draft_content.get("strategic_question")
    previous_strategic_result = draft_content.get("previous_strategic_result")
    subject, html_body = build_email(rewritten, synthesis, since_days,
                                      strategic_question=strategic_question,
                                      previous_strategic_result=previous_strategic_result)
    client.update("weekly_email_reports", {"report_id": f"eq.{report_id}"}, {
        "draft_content": draft_content, "html_body": html_body, "subject": subject,
    })
    if strategic_question and strategic_question.get("question_id"):
        sqs.save_question_edits(client, strategic_question["question_id"], {
            "title": strategic_question.get("title"),
            "question_text": strategic_question.get("question_text"),
            "options": strategic_question.get("options"),
        })


def reject_report(client: SupabaseClient, report_id: str, reviewer_id: str,
                   reviewer_feedback: dict = None) -> dict:
    """review_status='rejected'に更新する（送信しない）。
    既に送信済み(review_status='sent')の行は却下できないようガードする
    （send_state_machine.py: 却下ガードの共通化）"""
    report = get_report(client, report_id)
    if report is None:
        return {"ok": False, "error": "report_idが見つかりません"}
    state_machine = ssm.SendStateMachine(client, "weekly_email_reports", "report_id")
    return state_machine.guard_reject(
        report, status_field="review_status", blocked_statuses=("sent",),
        patch={
            "review_status": "rejected", "reviewer_id": reviewer_id,
            "reviewer_feedback": reviewer_feedback,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def record_send_result(client: SupabaseClient, report_id: str, *, send_mode: str, recipients: list,
                        send_status: str, send_error_message: str = None) -> None:
    """送信結果をweekly_email_reportsにUPDATEする（sent_at=now()。send_status成功なら
    review_status='sent'に進める。失敗時はapprovedのまま残し、再送できるようにする）"""
    patch = {
        "send_mode": send_mode, "recipients": recipients,
        "send_status": send_status, "send_error_message": send_error_message,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    if send_status == "success":
        patch["review_status"] = "sent"
    client.update("weekly_email_reports", {"report_id": f"eq.{report_id}"}, patch)


def _personalize_strategic_question_section(html_body: str, strategic_question: dict, links: list) -> str:
    """html_body内のSQ_SECTION_START/ENDマーカー間だけを、指定recipient向けの
    個別トークン付きリンクで差し替える（記事一覧等の共通部分は再生成しない）"""
    personalized = _render_strategic_question_section({**strategic_question, "options": [
        {**o, "url": next((l["url"] for l in links if l["option_code"] == o["option_code"]), o["url"])}
        for o in strategic_question["options"]
    ]})
    pattern = re.escape(SQ_SECTION_START) + r".*?" + re.escape(SQ_SECTION_END)
    return re.sub(pattern, personalized, html_body, count=1, flags=re.DOTALL)


def approve_and_send(client: SupabaseClient, config: dict, report_id: str, reviewer_id: str,
                      reviewer_feedback: dict = None, test_mode: bool = False) -> dict:
    """review_status='approved'に更新後、直ちにsend_email()を呼び、結果に応じて
    review_status='sent'（成功）または'approved'のまま（失敗、再送可能な状態として残す）にする。
    rejected状態の行に対しては何もせずエラーを返す（誤操作防止）。
    test_mode=Trueの場合、email_recipientsのis_test=true受信者のみへ送信する
    （本番受信者には届かない。ダッシュボードからの承認は常にtest_mode=False=本番）。

    draft_contentに埋め込み済みの戦略質問(question_id)がある場合、recipientごとに
    個別トークン付きリンクを差し込んだ個別送信に切り替える（Weekly Strategic Question機能）。
    送信自体も冪等にする: send_status='success'のrecipientには絶対に再送しない
    （approve_and_send()が送信途中のプロセス停止後に再実行された場合の二重送信防止）。
    「先週の結果」の掲載済みマークは、1件以上の配信が成功した場合のみ行う
    （全件失敗の場合は掲載済みにせず、次回また表示できるようにする）"""
    report = get_report(client, report_id)
    if report is None:
        return {"ok": False, "error": "report_idが見つかりません"}

    # review_status='approved'への更新と「送信中」claimを1回の条件付きUPDATEで
    # 原子的に行う。既に'rejected'/'sent'ならここで即座に失敗し、send_email()は
    # 一切呼ばれない。同時に2回approve_and_send()が呼ばれても、片方だけがこの
    # claimに成功する（send_state_machine.py: pmo-003/009の二重送信防止）
    state_machine = ssm.SendStateMachine(client, "weekly_email_reports", "report_id")
    claim = state_machine.claim_for_sending(
        report, status_field="review_status", blocked_statuses=("rejected", "sent"),
        extra_patch={
            "review_status": "approved", "reviewer_id": reviewer_id,
            "reviewer_feedback": reviewer_feedback,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if not claim["ok"]:
        return claim
    report = claim["row"]

    recipients = list_recipients(client, test_mode=test_mode)
    draft_content = report.get("draft_content") or {}
    strategic_question = draft_content.get("strategic_question")
    previous_strategic_result = draft_content.get("previous_strategic_result")

    if not strategic_question or not strategic_question.get("question_id"):
        # 戦略質問が埋め込まれていない週は、従来通り1通共有送信のまま
        result = send_email(report["subject"], report["html_body"], config, recipients)
        record_send_result(
            client, report_id, send_mode=result.get("mode"), recipients=recipients,
            send_status="success" if result.get("ok") else "error",
            send_error_message=result.get("error"),
        )
        return {**result, "review_status": "sent" if result.get("ok") else "approved"}

    question_id = strategic_question["question_id"]
    per_recipient = sqs.activate_on_digest_approval(client, config, question_id, recipients)

    successful_count = 0
    last_error = None
    send_mode = None
    for email, info in per_recipient.items():
        if info["send_status"] == "success":
            successful_count += 1  # 既に送信成功済み（冪等: 再送しない）
            continue
        personalized_html = _personalize_strategic_question_section(
            report["html_body"], strategic_question, info["links"])
        result = send_email(report["subject"], personalized_html, config, [email])
        send_mode = result.get("mode")
        sqs.mark_delivery_sent(client, info["delivery_id"], success=bool(result.get("ok")),
                                error_message=result.get("error"))
        if result.get("ok"):
            successful_count += 1
        else:
            last_error = result.get("error")

    ok = successful_count > 0
    record_send_result(
        client, report_id, send_mode=send_mode or "smtp", recipients=list(per_recipient.keys()),
        send_status="success" if ok else "error",
        send_error_message=None if ok else last_error,
    )
    if ok and previous_strategic_result and previous_strategic_result.get("insight_id"):
        sqs.mark_insight_included(client, previous_strategic_result["insight_id"], report_id)

    return {"ok": ok, "successful_count": successful_count, "total": len(per_recipient),
            "review_status": "sent" if ok else "approved"}


# ─── 件名（今週の主要テーマを抽出） ─────────────────────────────────
def build_subject(rewritten_articles: list) -> str:
    theme_counter: Counter = Counter()
    for a in rewritten_articles:
        theme_counter.update(a["themes"])
    top_themes = [t for t, _ in theme_counter.most_common(3)]

    now = datetime.now(timezone.utc)
    week_of_month = (now.day - 1) // 7 + 1
    theme_part = "・".join(top_themes) if top_themes else "サステナビリティ全般"
    return f"【サステナ週次】{now.year}年{now.month}月第{week_of_month}週：{theme_part}を中心に{len(rewritten_articles)}件"


# ─── HTMLメール本文の組み立て ───────────────────────────────────────
def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tag_line(a: dict) -> str:
    """テーマ以外のタグ（横断・主体・マテリアリティ接続）だけを1行にまとめる。
    テーマ自体は呼び出し側(_render_article_row)がメタ情報の先頭に別途表示するため含めない"""
    parts = a.get("cross_tags", [])[:2] + a.get("subject_tags", [])[:1]
    line = "｜".join(_esc(p) for p in parts)
    if a.get("materiality_codes"):
        line += ("｜" if line else "") + "/".join(a["materiality_codes"])
    return line


def _render_article_row(no: int, a: dict, geo_context: list = None) -> str:
    """記事1件をカード形式のブロックとして描画する。社内の週次ニュースピックアップ資料
    （IARD News Pickup）の.article-blockスタイルを踏襲: 左に太いアクセント罫のカード、
    太字の見出し、箇条書きの要点、末尾に記事リンク。旧デザイン(5列の密なテーブル行)は
    タグ・重要度・見出し・要点が1行に詰め込まれて読みにくかったため全面変更した。

    geo_context（Phase S2追加）: Geo Intelligence由来のsame_event/related_context item一覧
    （article_context.get(article_id, []) + background_context.get(article_id, [])）。
    空/未指定なら従来通り追加ブロックを出さない"""
    theme = _esc(a["themes"][0]) if a["themes"] else "その他"
    tag_line = _tag_line(a)
    meta = f"No.{no}　{theme}　重要度: {_esc(a['importance_level'])}"
    if tag_line:
        meta += f"　{tag_line}"

    # 要点(summary)を文単位に分割し、資料と同じ箇条書き(<ul><li>)で表示する
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", a.get("summary") or "") if s.strip()]
    points_html = "".join(f"<li>{_esc(s)}</li>" for s in sentences) or f"<li>{_esc(a.get('summary', ''))}</li>"

    geo_html = ""
    if geo_context:
        geo_lines = []
        for g in geo_context:
            g_title = _esc(g.get("title") or "")
            g_text = _esc((g.get("geo_assessment") or g.get("why_relevant") or "")[:300])
            geo_lines.append(f"<li><strong>{g_title}</strong>: {g_text}</li>" if g_title else f"<li>{g_text}</li>")
        geo_html = f"""
      <div style="margin-top:8px;padding:8px 10px;background:#eef4fb;border-radius:6px;border:1px dashed #9db8d6;">
        <div style="font-size:11px;font-weight:700;color:#2f6fa8;margin-bottom:4px;">🌏 地政学コンテキスト（Geo Intelligence）</div>
        <ul style="margin:0;padding-left:1.1em;font-size:12px;color:#334155;line-height:1.5;">{''.join(geo_lines)}</ul>
      </div>"""

    return f"""
    <div style="background:#fbfdff;border:1px solid #c7d6e6;border-left:5px solid #2f6fa8;
                border-radius:8px;padding:14px 15px 13px;margin:0 0 14px;">
      <div style="font-size:11px;color:#5b7488;font-weight:700;margin-bottom:6px;">{meta}</div>
      <p style="font-size:16px;font-weight:800;color:#082f49;margin:0 0 8px;line-height:1.5;">
        {_esc(a['headline'])}
      </p>
      <ul style="margin:7px 0 10px 1.2em;padding:0;font-size:13px;color:#0b1220;line-height:1.65;">{points_html}</ul>
      <p style="margin:0;">
        <a href="{_esc(a['url'])}" style="font-weight:800;color:#064f8a;text-decoration:underline;font-size:12.5px;">記事リンク</a>
      </p>
      {geo_html}
    </div>"""


def _render_geo_intelligence_section(selected_geo_topics: list) -> str:
    """統合最終選定（select_weekly_geo_topics）を通過したGeo Intelligence独立トピックの
    セクション。個別記事一覧とテーマ別ダイジェストの間に挿入する（Phase S2新規）。
    0件の場合は何も出力しない（既存出力への影響なし）。昇格記事はarticlesに混ざっている
    ためここには出さない（二重掲載防止）"""
    if not selected_geo_topics:
        return ""
    rows = []
    for item in selected_geo_topics:
        title = _esc(item.get("title") or "")
        assessment = _esc(item.get("geo_assessment") or "")
        why = _esc(item.get("why_relevant") or "")
        outlook = _esc(item.get("outlook") or "")
        rows.append(f"""
    <div style="background:#fbfdff;border:1px solid #c7d6e6;border-left:5px solid #7c3aed;
                border-radius:8px;padding:14px 15px 13px;margin:0 0 14px;">
      <p style="font-size:15px;font-weight:800;color:#082f49;margin:0 0 8px;">{title}</p>
      <p style="font-size:13px;color:#0b1220;margin:0 0 6px;">{assessment}</p>
      <p style="font-size:12px;color:#475569;margin:0 0 4px;">関連性: {why}</p>
      <p style="font-size:12px;color:#475569;margin:0;">見通し: {outlook}</p>
    </div>""")
    return f"""
  <h2 style="font-size:15px;border-left:4px solid #7c3aed;padding-left:8px;">
    地政学インテリジェンス（Geo Intelligence由来の独立トピック）：{len(selected_geo_topics)}件
  </h2>
  <div style="margin-bottom:24px;">{''.join(rows)}</div>"""


# Weekly Strategic Question機能: 送信時にリンクを個別recipient向けへ差し替えるための
# 目印コメント。approve_and_send()がこの2つのマーカーの間だけを文字列置換する
# （記事一覧・シンセシス等の共通部分は再生成せずそのまま使い回すため）
SQ_SECTION_START = "<!--SQ_SECTION_START-->"
SQ_SECTION_END = "<!--SQ_SECTION_END-->"


def _render_strategic_question_section(strategic_question: dict = None) -> str:
    """「今週の1問」セクション。strategic_questionが無ければ何も出力しない
    （既存のGeo Intelligenceセクション等と同じ「空なら no-op」規約）。
    optionsの各要素は{"option_code","label","url"}（urlはdraft時点ではプレースホルダー、
    実際の個別トークン付きリンクはapprove_and_send()が差し替える）"""
    if not strategic_question:
        return f"{SQ_SECTION_START}{SQ_SECTION_END}"
    options_html = "".join(
        f'<p style="margin:6px 0;"><a href="{_esc(o["url"])}" style="display:inline-block;'
        f'padding:8px 14px;background:#0f766e;color:#fff;text-decoration:none;'
        f'border-radius:6px;font-size:13px;font-weight:700;">{_esc(o["option_code"])}. {_esc(o["label"])}</a></p>'
        for o in strategic_question["options"]
    )
    return f"""{SQ_SECTION_START}
  <div style="background:#f0fdfa;border:1px solid #99f6e4;border-radius:8px;padding:16px;margin:0 0 20px;">
    <p style="font-size:12px;font-weight:800;color:#0f766e;letter-spacing:0.05em;margin:0 0 8px;">
      ━━━ 今週の1問 ━━━
    </p>
    <p style="font-size:14px;color:#134e4a;margin:0 0 10px;line-height:1.6;">
      {_esc(strategic_question['question_text'])}
    </p>
    {options_html}
  </div>
{SQ_SECTION_END}"""


def _render_previous_result_section(previous_strategic_result: dict = None) -> str:
    """「先週の結果」セクション。回答分布とAI要約を掲載する。
    62%だったので会社方針はこれである、のような表現は絶対にしない（guardrail_labelを必ず併記）"""
    if not previous_strategic_result:
        return ""
    dist_html = "".join(
        f'<p style="margin:3px 0;font-size:13px;color:#334155;">{_esc(o["label"])}　{o["pct"]}%（{o["count"]}件）</p>'
        for o in previous_strategic_result.get("distribution", [])
    )
    return f"""
  <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:0 0 20px;">
    <p style="font-size:12px;font-weight:800;color:#475569;letter-spacing:0.05em;margin:0 0 8px;">先週の1問</p>
    <p style="font-size:13px;font-weight:700;color:#0f172a;margin:0 0 8px;">
      {_esc(previous_strategic_result.get('title', ''))}
    </p>
    {dist_html}
    <p style="font-size:12px;color:#475569;margin:10px 0 2px;font-weight:700;">AIが抽出した判断傾向</p>
    <p style="font-size:12px;color:#334155;margin:0 0 6px;">{_esc(previous_strategic_result.get('observed_tendency', ''))}</p>
    <p style="font-size:11px;color:#94a3b8;margin:0;">{_esc(previous_strategic_result.get('guardrail_label', ''))}</p>
  </div>"""


def build_email(rewritten_articles: list, synthesis: dict, since_days: int,
                 article_context: dict = None, background_context: dict = None,
                 selected_geo_topics: list = None, strategic_question: dict = None,
                 previous_strategic_result: dict = None) -> tuple:
    """(subject, html_body) を返す。article_context/background_context/selected_geo_topics
    （Phase S2追加）、strategic_question/previous_strategic_result（Weekly Strategic Question
    機能追加）は省略時（すべてNone/空）は従来通りの出力になる"""
    article_context = article_context or {}
    background_context = background_context or {}
    subject = build_subject(rewritten_articles)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sq_section_html = _render_strategic_question_section(strategic_question)
    prev_result_html = _render_previous_result_section(previous_strategic_result)

    if not rewritten_articles:
        body = '<p style="color:#64748b;">直近期間に該当する記事はありませんでした。</p>'
        html = f"""<!DOCTYPE html><html><body style="font-family:'Hiragino Sans','Meiryo',sans-serif;
        color:#0f172a;max-width:720px;margin:0 auto;"><h1 style="font-size:20px;">🌏 サステナビリティ週次レポート</h1>
        <p style="color:#64748b;font-size:13px;">対象期間: 直近{since_days}日間　生成日: {today}</p>
        {sq_section_html}{prev_result_html}{body}</body></html>"""
        return subject, html

    highlights_html = "".join(f"<li style='margin-bottom:4px;'>{_esc(h)}</li>" for h in synthesis.get("highlights", []))
    article_rows = "\n".join(
        _render_article_row(i, a, geo_context=(article_context.get(a["article_id"], [])
                                                + background_context.get(a["article_id"], [])))
        for i, a in enumerate(rewritten_articles, 1)
    )
    geo_section_html = _render_geo_intelligence_section(selected_geo_topics or [])

    digest_rows = "\n".join(
        f"""<tr style="border-bottom:1px solid #e5e7eb;">
              <td style="padding:8px 6px;vertical-align:top;font-size:13px;font-weight:600;white-space:nowrap;">{_esc(d['theme'])}</td>
              <td style="padding:8px 6px;vertical-align:top;font-size:13px;color:#334155;">{_esc(d['digest'])}</td>
            </tr>"""
        for d in synthesis.get("theme_digests", [])
    )

    html = f"""<!DOCTYPE html>
<html><body style="font-family:'Hiragino Sans','Meiryo',sans-serif;color:#0f172a;max-width:720px;margin:0 auto;">
  <h1 style="font-size:20px;">🌏 サステナビリティ週次レポート</h1>
  <p style="color:#64748b;font-size:13px;">対象期間: 直近{since_days}日間　生成日: {today}</p>

  {sq_section_html}
  {prev_result_html}

  <p style="font-size:14px;line-height:1.7;">{_esc(synthesis.get('overview', ''))}</p>

  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;">今週の注目ポイント</h2>
  <ul style="font-size:13px;line-height:1.6;padding-left:20px;">{highlights_html}</ul>

  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;">
    メール掲載記事：{len(rewritten_articles)}件
  </h2>
  <div style="margin-bottom:24px;">{article_rows}</div>
  {geo_section_html}
  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;">テーマ別ダイジェスト</h2>
  <table style="width:100%;border-collapse:collapse;">
    <thead>
      <tr style="border-bottom:2px solid #334155;font-size:12px;color:#64748b;text-align:left;">
        <th style="padding:6px;">テーマ</th><th style="padding:6px;">今週の読み筋</th>
      </tr>
    </thead>
    <tbody>{digest_rows}</tbody>
  </table>

  <p style="color:#94a3b8;font-size:11px;margin-top:24px;">
    本メールは自動生成レポートです。要点・示唆はAIによる暫定判断を含み、会社の正式見解ではありません。
  </p>
</body></html>"""
    return subject, html


# ─── 配信先（DB管理。email_recipients、通知種別=notify_weekly_digest） ─────
def list_recipients(client: SupabaseClient, test_mode: bool = False) -> list:
    """週次ダイジェストの配信先をemail_recipientsから取得する。
    test_mode=Falseなら本番受信者、Trueならテスト受信者のみ（両者は排他）。
    config.jsonのemail.to_addresses（固定配列）は廃止した"""
    return common.list_recipients(client, "notify_weekly_digest", test_mode=test_mode)


# ─── 送信（またはプレビュー保存） ───────────────────────────────────
def send_email(subject: str, html_body: str, config: dict, to_addresses: list) -> dict:
    """email.smtp_host が設定されていればto_addressesへSMTP送信し、無ければ
    プレビューHTMLをcache/ に保存する（送信方式未確定の間の暫定動作）。
    宛先はDB（email_recipients）から呼び出し側が解決して渡す
    （list_recipients / competitor_alert.list_recipients参照）"""
    email_cfg = config.get("email", {})
    smtp_host = email_cfg.get("smtp_host", "")

    if not email_cfg.get("enabled") or not smtp_host:
        CACHE_DIR.mkdir(exist_ok=True)
        out_path = CACHE_DIR / f"weekly_email_preview_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
        out_path.write_text(html_body, encoding="utf-8")
        return {"ok": True, "mode": "preview", "path": str(out_path)}

    if not to_addresses:
        return {"ok": False, "mode": "smtp", "error": "配信先が0件です（email_recipients未登録）"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from_address", "")
    msg["To"] = ", ".join(to_addresses)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    smtp_port = int(email_cfg.get("smtp_port", 587))
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            if email_cfg.get("use_starttls", True):
                server.starttls()
            smtp_user = email_cfg.get("smtp_user", "")
            smtp_password = email_cfg.get("smtp_password", "")
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.sendmail(email_cfg.get("from_address", ""), to_addresses, msg.as_string())
        return {"ok": True, "mode": "smtp", "to": to_addresses}
    except Exception as e:
        return {"ok": False, "mode": "smtp", "error": f"{type(e).__name__}: {e}"}


# ─── フェーズ1: ドラフト生成（送信しない） ───────────────────────────
def build_draft(since_days: int = None, force_geo_rerun: bool = False) -> str:
    """選定結果取得→記事組み立て→（Phase S2）Geo Intelligence連携→見出し要点書き換え→
    シンセシス→HTML組み立てまでを行い、review_requiredで1行保存してreport_idを返す。
    送信は行わない（sustainability_expert_dashboard.py でレビュー・承認して初めて送信される）。
    force_geo_rerun: 手動でGeo Intelligence問い合わせ・Dedup・Selectionをすべて強制再実行したい
    場合のみTrue（通常は3層とも自動でキャッシュ再利用される）"""
    config = load_config()
    if not common.is_enabled(config):
        print("SUSTAINABILITY_EXPERT_ENABLED が無効です（config.jsonのsustainability_expert.enabled、"
              "または環境変数で有効化してください）。処理を行わず終了します。")
        return None

    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if not azure_client:
        print("Azure OpenAI / OpenAI のAPIキーが設定されていません")
        return None

    expert_base = common.load_expert_base()
    since_days = since_days if since_days is not None else common.get_weekly_pick_since_days(config)
    _, target_max = common.get_weekly_pick_range(config)
    usage_log: list = []
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=since_days)

    print(f"直近{since_days}日分の専門家AI選定結果から、上位{target_max}件を抽出中...")
    picks = selector.list_weekly_picks(client, since_days=since_days, target_max=target_max)
    print(f"  対象クラスタ: {len(picks)}件")

    all_urls = common.fetch_all_article_urls(client)
    articles = build_articles_from_picks(client, picks, all_urls=all_urls)

    # ─── Phase S2: Geo Intelligence連携（Weekly x Geo Intelligence Batch Inquiry） ───
    # Kill Switch OFF時は空の構造化結果のまま進み、既存出力に一切影響しない。
    # 失敗時もWeekly生成自体は継続する（Failure Isolation）
    geo_result = {"independent_candidates": [], "promotion_candidates": [], "article_context": {},
                  "background_context": {}, "theme_background_context": {}, "run_status": "disabled"}
    selection_result = {"selected_geo_topics": [], "promoted_article_ids": [], "displaced_article_ids": []}
    if is_weekly_geo_enabled(config):
        try:
            # v5修正点2: Dedup比較用の広い母集団を別途取得（新しいSelectorロジックは作らず、
            # 既存list_weekly_picks()のtarget_maxを大きくして呼ぶだけ）
            dedup_pool_max = config.get("geo_intelligence", {}).get("weekly_monitoring", {}).get(
                "dedup_candidate_pool_max", 200)
            wide_picks = selector.list_weekly_picks(client, since_days=since_days, target_max=dedup_pool_max)
            dedup_candidate_pool = build_articles_from_picks(client, wide_picks, all_urls=all_urls)

            geo_result = weekly_geo_intelligence_service.run_weekly_geo_inquiry(
                config, client, period_start.date(), period_end.date(), articles, dedup_candidate_pool,
                azure_client=azure_client, model=model, force_rerun=force_geo_rerun)
            if geo_result["independent_candidates"] or geo_result["promotion_candidates"]:
                selection_result = weekly_geo_intelligence_service.select_weekly_geo_topics(
                    azure_client, model, geo_result["independent_candidates"],
                    geo_result["promotion_candidates"], articles, config, client,
                    geo_result["run_id"], force_rerun=force_geo_rerun)
        except Exception as e:
            print(f"[weekly] Geo Intelligence連携に失敗（Weekly生成は継続）: {type(e).__name__}: {e}")

    # displaced_article_idsを除外し、promoted_article_idsに対応する記事を追加する。
    # 昇格記事にはGeo Contextを引き継ぐ（v6修正点1）ため、article_contextへgeo_itemsを追加する
    if selection_result["displaced_article_ids"]:
        displaced_set = set(selection_result["displaced_article_ids"])
        articles = [a for a in articles if a["article_id"] not in displaced_set]
    if selection_result["promoted_article_ids"]:
        pool_by_id = {a["article_id"]: a for a in dedup_candidate_pool}
        promo_geo_items_by_article = {
            p["article_id"]: p.get("geo_items") or [] for p in geo_result["promotion_candidates"]
        }
        for promoted_id in selection_result["promoted_article_ids"]:
            promoted_article = pool_by_id.get(promoted_id)
            if promoted_article is None or any(a["article_id"] == promoted_id for a in articles):
                continue
            articles.append(promoted_article)
            geo_result["article_context"].setdefault(promoted_id, []).extend(
                promo_geo_items_by_article.get(promoted_id, []))

    rewritten = []
    for i, a in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] 見出し・要点を生成中: {a['title'][:30]} ...", end=" ", flush=True)
        try:
            rewrite = rewrite_article_for_digest(azure_client, model, expert_base, a, usage_log=usage_log)
            rewritten.append({**a, **rewrite})
            save_detail_summary(client, a["article_id"], rewrite)
            print("完了")
        except Exception as e:
            print(f"エラー（この記事はスキップ）: {type(e).__name__}: {e}")

    print("週次シンセシス（概況・注目ポイント・テーマ別ダイジェスト）を生成中...")
    synthesis_error = None
    try:
        synthesis = build_weekly_synthesis(azure_client, model, expert_base, rewritten, usage_log=usage_log,
                                            theme_background_context=geo_result["theme_background_context"])
    except Exception as e:
        synthesis_error = f"{type(e).__name__}: {e}"
        print(f"シンセシス生成エラー: {synthesis_error}")
        synthesis = {"overview": "", "highlights": [], "theme_digests": []}

    # ─── Weekly Strategic Question連携（Failure Isolation: 失敗してもWeekly生成は継続） ───
    strategic_question_payload = None
    previous_result_payload = None
    if sqs.is_strategic_question_enabled(config):
        try:
            draft_question = sqs.get_draft_question_for_digest_embed(
                client, period_start.date().isoformat(), period_end.date().isoformat())
            if draft_question:
                strategic_question_payload = {
                    "question_id": draft_question["question_id"],
                    "title": draft_question["title"], "question_text": draft_question["question_text"],
                    "options": [{"option_code": o["option_code"], "label": o["label"], "url": "#"}
                                for o in draft_question["_options"]],
                }
        except Exception as e:
            print(f"[weekly] Strategic Question埋め込みに失敗（Weekly生成は継続）: {type(e).__name__}: {e}")

        try:
            prev_insight = sqs.get_last_unincluded_insight_for_digest(client, config)
            if prev_insight:
                prev_question = sqs.get_question(client, prev_insight["question_id"])
                dist = (prev_insight.get("distribution_json") or {}).get("by_option", [])
                previous_result_payload = {
                    "insight_id": prev_insight["insight_id"],
                    "title": prev_question["question_text"] if prev_question else "",
                    "distribution": dist,
                    "observed_tendency": prev_insight["observed_tendency"],
                    "guardrail_label": prev_insight["guardrail_label"],
                }
        except Exception as e:
            print(f"[weekly] 先週の結果の取得に失敗（Weekly生成は継続）: {type(e).__name__}: {e}")

    subject, html_body = build_email(
        rewritten, synthesis, since_days,
        article_context=geo_result["article_context"], background_context=geo_result["background_context"],
        selected_geo_topics=selection_result["selected_geo_topics"],
        strategic_question=strategic_question_payload, previous_strategic_result=previous_result_payload)
    token_usage, latency_ms = _aggregate_usage(usage_log)

    draft_content = {"articles": rewritten, "synthesis": synthesis, "since_days": since_days,
                      "strategic_question": strategic_question_payload,
                      "previous_strategic_result": previous_result_payload}
    report_id = save_draft_report(
        client,
        period_start=period_start.date(), period_end=period_end.date(),
        article_ids=[a["article_id"] for a in rewritten],
        model_deployment=model, token_usage=token_usage, latency_ms=latency_ms,
        status="success" if not synthesis_error else "error", error_message=synthesis_error,
        subject=subject, draft_content=draft_content, html_body=html_body,
    )
    if report_id:
        if strategic_question_payload:
            try:
                sqs.mark_embedded(client, strategic_question_payload["question_id"], report_id)
            except Exception as e:
                print(f"[weekly] Strategic Questionのembedded状態更新に失敗: {type(e).__name__}: {e}")
        print(f"ドラフトを保存しました（report_id={report_id}, review_status=review_required）。"
              f"送信は行いません。sustainability_expert_dashboard.py でレビュー・承認してください。")
    return report_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="週次メールレポート（ドラフト生成→レビュー→送信）")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_build = subparsers.add_parser("build", help="ドラフトを生成しreview_requiredで保存する（送信しない）")
    p_build.add_argument("--since-days", type=int, default=None)
    p_build.add_argument("--force-geo-rerun", action="store_true",
                          help="Geo Intelligence問い合わせ・Dedup・Selectionを3層とも強制再実行する"
                               "（通常は指定不要。手動再実行用）")

    p_send = subparsers.add_parser(
        "send", help="承認済みドラフトを送信する（主経路はダッシュボードの承認ボタン。CLIは運用フォールバック）")
    p_send.add_argument("--report-id", required=True)
    p_send.add_argument("--reviewer-id", default=None,
                         help="非推奨・Phase 3以降は無視されます。自由記述のreviewer_idを"
                              "アイデンティティとして使わないため。実際の送信者はDEV AUTHモード"
                              "（環境変数 SUSTAINABILITY_EXPERT_DEV_AUTH_MODE / "
                              "SUSTAINABILITY_EXPERT_DEV_USER_ID）経由でのみ解決される")
    p_send.add_argument("--mode", choices=["test", "production"], default="production",
                         help="test指定時はemail_recipientsのis_test=true受信者のみに送信する"
                              "（本番受信者には届かない）。未指定時はproduction（本番受信者へ送信）")

    cli_args = parser.parse_args()
    if cli_args.cmd == "build":
        build_draft(since_days=cli_args.since_days, force_geo_rerun=cli_args.force_geo_rerun)
    else:
        # Phase 3: --reviewer-idという自由記述文字列はもはやアイデンティティとして使わない
        # （Baseline v1では既定値"cli"がそのままreviewer_id監査列に書き込まれていた）。
        # DEV AUTHモードが明示的に有効な場合のみ環境変数由来のFakePrincipalで送信でき、
        # それ以外（本番相当。Entra ID未接続）は常にfail-closedする。
        if cli_args.reviewer_id is not None:
            print(f"[警告] --reviewer-id は無視されます（Phase 3以降、自由記述のreviewer_idは"
                  f"アイデンティティとして使用しません）: {cli_args.reviewer_id!r}")
        try:
            _principal = identity.resolve_principal(dev_principal=identity.dev_principal_from_env())
        except identity.AuthenticationError as e:
            print(f"[エラー] 認証済みレビュー担当者を解決できないため送信を中止しました（fail-closed）: {e}")
            sys.exit(1)
        if not identity.can_approve(_principal):
            print("[エラー] この操作を行う権限がありません（reviewerロールが必要です）")
            sys.exit(1)
        _config = load_config()
        _client = SupabaseClient(_config)
        _result = approve_and_send(_client, _config, cli_args.report_id,
                                    reviewer_id=identity.audit_reviewer_id(_principal),
                                    test_mode=(cli_args.mode == "test"))
        print(_result)
