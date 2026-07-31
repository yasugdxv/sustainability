"""
週次メールレポート生成モジュール

サスティナビリティ専門家AI（sustainability_article_selector.py）がテーマ大分類ごとの
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
サスティナビリティ担当者がsustainability_expert_dashboard.pyでレビュー・承認して初めて
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
ARTICLE_REWRITE_SYSTEM_PROMPT = """あなたは当社サスティナビリティ専門家です。
1件の記事について、サステナビリティ担当者(サス推)向け週次メールに掲載する
「見出し」と「要点」を作成してください。

要点の書き方:
- 記事で確定している事実を簡潔に述べる
- そのうえで、サス推が確認・対応すべき観点を「サス推では〜」等の表現で、
  事実と区別できる形で付け加える
- 会社の正式見解として断定しない。推測は推測と分かる書き方にする
- 全体で2〜3文、130文字程度を目安にする
- 出典にない数値・日付・義務を補完しない

出力はJSON1個のみ:
{"headline": "記事の見出し(20文字程度。【】は付けない)", "summary": "要点(事実+サス推への示唆)"}
"""

ARTICLE_REWRITE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "summary"],
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
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


# ─── 週次シンセシス（概況・注目ポイント・テーマ別ダイジェスト） ────────
WEEKLY_SYNTHESIS_SYSTEM_PROMPT = """あなたは当社サスティナビリティ専門家です。
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
                            usage_log: list = None) -> dict:
    if not rewritten_articles:
        return {"overview": "", "highlights": [], "theme_digests": []}

    articles_text = "\n".join(
        f"- [{', '.join(a['themes'])}] {a['headline']}: {a['summary']}"
        for a in rewritten_articles
    )
    user_prompt = (
        f"# company_context\n{json.dumps(expert_base.get('company_context', {}), ensure_ascii=False)}\n\n"
        f"# 今週選定された記事一覧\n{articles_text}\n"
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
    再生成してあわせて保存する（review_statusは変えない）"""
    rewritten = draft_content.get("articles", [])
    synthesis = draft_content.get("synthesis", {})
    subject, html_body = build_email(rewritten, synthesis, since_days)
    client.update("weekly_email_reports", {"report_id": f"eq.{report_id}"}, {
        "draft_content": draft_content, "html_body": html_body, "subject": subject,
    })


def reject_report(client: SupabaseClient, report_id: str, reviewer_id: str,
                   reviewer_feedback: dict = None) -> None:
    """review_status='rejected'に更新するのみ（送信しない）"""
    client.update("weekly_email_reports", {"report_id": f"eq.{report_id}"}, {
        "review_status": "rejected", "reviewer_id": reviewer_id,
        "reviewer_feedback": reviewer_feedback,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })


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


def approve_and_send(client: SupabaseClient, config: dict, report_id: str, reviewer_id: str,
                      reviewer_feedback: dict = None) -> dict:
    """review_status='approved'に更新後、直ちにsend_email()を呼び、結果に応じて
    review_status='sent'（成功）または'approved'のまま（失敗、再送可能な状態として残す）にする。
    rejected状態の行に対しては何もせずエラーを返す（誤操作防止）"""
    report = get_report(client, report_id)
    if report is None:
        return {"ok": False, "error": "report_idが見つかりません"}
    if report.get("review_status") == "rejected":
        return {"ok": False, "error": "却下済みのレポートは送信できません"}

    client.update("weekly_email_reports", {"report_id": f"eq.{report_id}"}, {
        "review_status": "approved", "reviewer_id": reviewer_id,
        "reviewer_feedback": reviewer_feedback,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    })

    result = send_email(report["subject"], report["html_body"], config)
    email_cfg = config.get("email", {})
    record_send_result(
        client, report_id, send_mode=result.get("mode"),
        recipients=email_cfg.get("to_addresses", []),
        send_status="success" if result.get("ok") else "error",
        send_error_message=result.get("error"),
    )
    return {**result, "review_status": "sent" if result.get("ok") else "approved"}


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


def _render_article_row(no: int, a: dict) -> str:
    """記事1件をカード形式のブロックとして描画する。社内の週次ニュースピックアップ資料
    （IARD News Pickup）の.article-blockスタイルを踏襲: 左に太いアクセント罫のカード、
    太字の見出し、箇条書きの要点、末尾に記事リンク。旧デザイン(5列の密なテーブル行)は
    タグ・重要度・見出し・要点が1行に詰め込まれて読みにくかったため全面変更した"""
    theme = _esc(a["themes"][0]) if a["themes"] else "その他"
    tag_line = _tag_line(a)
    meta = f"No.{no}　{theme}　重要度: {_esc(a['importance_level'])}"
    if tag_line:
        meta += f"　{tag_line}"

    # 要点(summary)を文単位に分割し、資料と同じ箇条書き(<ul><li>)で表示する
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", a.get("summary") or "") if s.strip()]
    points_html = "".join(f"<li>{_esc(s)}</li>" for s in sentences) or f"<li>{_esc(a.get('summary', ''))}</li>"

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
    </div>"""


def build_email(rewritten_articles: list, synthesis: dict, since_days: int) -> tuple:
    """(subject, html_body) を返す"""
    subject = build_subject(rewritten_articles)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not rewritten_articles:
        body = '<p style="color:#64748b;">直近期間に該当する記事はありませんでした。</p>'
        html = f"""<!DOCTYPE html><html><body style="font-family:'Hiragino Sans','Meiryo',sans-serif;
        color:#0f172a;max-width:720px;margin:0 auto;"><h1 style="font-size:20px;">🌏 サステナビリティ週次レポート</h1>
        <p style="color:#64748b;font-size:13px;">対象期間: 直近{since_days}日間　生成日: {today}</p>{body}</body></html>"""
        return subject, html

    highlights_html = "".join(f"<li style='margin-bottom:4px;'>{_esc(h)}</li>" for h in synthesis.get("highlights", []))
    article_rows = "\n".join(_render_article_row(i, a) for i, a in enumerate(rewritten_articles, 1))

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

  <p style="font-size:14px;line-height:1.7;">{_esc(synthesis.get('overview', ''))}</p>

  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;">今週の注目ポイント</h2>
  <ul style="font-size:13px;line-height:1.6;padding-left:20px;">{highlights_html}</ul>

  <h2 style="font-size:15px;border-left:4px solid #1d4ed8;padding-left:8px;">
    メール掲載記事：{len(rewritten_articles)}件
  </h2>
  <div style="margin-bottom:24px;">{article_rows}</div>

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


# ─── 送信（またはプレビュー保存） ───────────────────────────────────
def send_email(subject: str, html_body: str, config: dict) -> dict:
    """email.smtp_host が設定されていればSMTP送信し、無ければプレビューHTMLを
    cache/ に保存する（送信方式未確定の間の暫定動作）"""
    email_cfg = config.get("email", {})
    smtp_host = email_cfg.get("smtp_host", "")

    if not email_cfg.get("enabled") or not smtp_host:
        CACHE_DIR.mkdir(exist_ok=True)
        out_path = CACHE_DIR / f"weekly_email_preview_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
        out_path.write_text(html_body, encoding="utf-8")
        return {"ok": True, "mode": "preview", "path": str(out_path)}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from_address", "")
    to_addresses = email_cfg.get("to_addresses", [])
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
def build_draft(since_days: int = None) -> str:
    """選定結果取得→記事組み立て→見出し要点書き換え→シンセシス→HTML組み立てまでを行い、
    review_requiredで1行保存してreport_idを返す。送信は行わない
    （sustainability_expert_dashboard.py でレビュー・承認して初めて送信される）"""
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

    rewritten = []
    for i, a in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] 見出し・要点を生成中: {a['title'][:30]} ...", end=" ", flush=True)
        try:
            rewrite = rewrite_article_for_digest(azure_client, model, expert_base, a, usage_log=usage_log)
            rewritten.append({**a, **rewrite})
            print("完了")
        except Exception as e:
            print(f"エラー（この記事はスキップ）: {type(e).__name__}: {e}")

    print("週次シンセシス（概況・注目ポイント・テーマ別ダイジェスト）を生成中...")
    synthesis_error = None
    try:
        synthesis = build_weekly_synthesis(azure_client, model, expert_base, rewritten, usage_log=usage_log)
    except Exception as e:
        synthesis_error = f"{type(e).__name__}: {e}"
        print(f"シンセシス生成エラー: {synthesis_error}")
        synthesis = {"overview": "", "highlights": [], "theme_digests": []}

    subject, html_body = build_email(rewritten, synthesis, since_days)
    token_usage, latency_ms = _aggregate_usage(usage_log)

    draft_content = {"articles": rewritten, "synthesis": synthesis, "since_days": since_days}
    report_id = save_draft_report(
        client,
        period_start=period_start.date(), period_end=period_end.date(),
        article_ids=[a["article_id"] for a in rewritten],
        model_deployment=model, token_usage=token_usage, latency_ms=latency_ms,
        status="success" if not synthesis_error else "error", error_message=synthesis_error,
        subject=subject, draft_content=draft_content, html_body=html_body,
    )
    if report_id:
        print(f"ドラフトを保存しました（report_id={report_id}, review_status=review_required）。"
              f"送信は行いません。sustainability_expert_dashboard.py でレビュー・承認してください。")
    return report_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="週次メールレポート（ドラフト生成→レビュー→送信）")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_build = subparsers.add_parser("build", help="ドラフトを生成しreview_requiredで保存する（送信しない）")
    p_build.add_argument("--since-days", type=int, default=None)

    p_send = subparsers.add_parser(
        "send", help="承認済みドラフトを送信する（主経路はダッシュボードの承認ボタン。CLIは運用フォールバック）")
    p_send.add_argument("--report-id", required=True)
    p_send.add_argument("--reviewer-id", default="cli")

    cli_args = parser.parse_args()
    if cli_args.cmd == "build":
        build_draft(since_days=cli_args.since_days)
    else:
        _config = load_config()
        _client = SupabaseClient(_config)
        _result = approve_and_send(_client, _config, cli_args.report_id, reviewer_id=cli_args.reviewer_id)
        print(_result)
