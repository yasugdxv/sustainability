"""
当社サステナビリティ専門家MVP: レビュー画面（Streamlitプロトタイプ）

生成されたコンテンツ候補(review_required)を一覧・確認し、承認/却下/修正と
レビューフィードバックを記録する。自動公開はしない（承認までがこのMVPの範囲）。

実行: streamlit run projects/world_monitor_dashboard/sustainability_expert_dashboard.py
"""
import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config  # noqa: E402
import sustainability_expert_common as common  # noqa: E402
import sustainability_expert_api as expert_api  # noqa: E402
import weekly_email_report as wer  # noqa: E402
import monthly_competitor_report as monthly_competitor  # noqa: E402
import competitor_source_discovery as source_discovery  # noqa: E402
import competitor_daily_digest as daily_digest  # noqa: E402
import strategic_question_service as sq_service  # noqa: E402

CANDIDATE_URL_TYPES = ["SUSTAINABILITY_HOME", "TARGETS", "PROGRESS", "REPORTS", "NEWS", "DISCLOSURE", "OTHER"]

WEEKLY_STATUS_LABELS = {
    "review_required": "🕒 レビュー待ち",
    "approved":         "✅ 承認済み（送信待ち/再送可）",
    "rejected":          "🚫 却下",
    "sent":              "📧 送信済み",
}

COMPETITOR_ALERT_STATUS_LABELS = {
    "auto_sent":         "⚡ 自動配信済み",
    "review_required":   "🕒 レビュー待ち",
    "approved":           "✅ 承認済み（送信待ち/再送可）",
    "rejected":            "🚫 却下",
    "sent":                 "📧 送信済み",
    "error":                 "⚠️ 送信エラー",
}

COMPETITOR_MONTHLY_STATUS_LABELS = {
    "DRAFT":              "📝 下書き",
    "GENERATED":          "🕒 レビュー待ち",
    "UNDER_REVIEW":       "🔎 レビュー中",
    "REVISION_REQUIRED":  "✏️ 修正要",
    "APPROVED":           "✅ 承認済み（送信待ち/再送可）",
    "SENT":               "📧 送信済み",
    "CANCELLED":          "🚫 却下",
}


@st.cache_resource
def _load_config() -> dict:
    return load_config()


def _get_client(config: dict) -> SupabaseClient:
    return SupabaseClient(config)


def _render_geo_intelligence_summary(report: dict, config: dict):
    """Geo Intelligence（Phase S2）実行結果の簡易表示のみ。大規模UIは作らない。
    weekly_geo_intelligence_runs/itemsが無い（未適用/Kill Switch OFF）場合は何も表示しない"""
    period_start, period_end = report.get("period_start"), report.get("period_end")
    if not period_start or not period_end:
        return
    try:
        client = _get_client(config)
        runs = client.select("weekly_geo_intelligence_runs", {
            "select": "*", "period_start": f"eq.{period_start}", "period_end": f"eq.{period_end}",
            "order": "created_at.desc", "limit": "1",
        })
        if not runs:
            return
        run = runs[0]
        items = client.select("weekly_geo_intelligence_items", {"select": "*", "run_id": f"eq.{run['id']}"})
        same_event = sum(1 for i in items if i.get("dedup_classification") == "same_event")
        related = sum(1 for i in items if i.get("dedup_classification") == "related_context")
        independent_selected = sum(1 for i in items if i.get("dedup_classification") == "independent"
                                    and i.get("selection_status") == "selected")
        st.caption(f"🌏 Geo Intelligence: 取得{len(items)}件 / 採用{independent_selected}件（independent） / "
                   f"記事へ紐付け{same_event}件（same_event） / 背景情報{related}件（related_context）")
    except Exception:
        pass  # 取得に失敗してもレビュー画面自体は継続する（大規模UIは作らない最小追加のため）


def _render_weekly_report_card(report: dict, config: dict):
    """1週分のドラフト全体を1カードとして表示する。表示内容は送信される
    メール本文そのもの（html_bodyのプレビュー）のみとし、概況・注目ポイント・
    テーマ別ダイジェスト・記事一覧をStreamlit側で別途表示することはしない
    （メール本文に既に含まれており二重表示になるため）。
    承認して送信/却下/修正のみ保存の3操作を提供する"""
    draft = report.get("draft_content") or {}
    articles = draft.get("articles", [])
    report_id = report["report_id"]
    label = WEEKLY_STATUS_LABELS.get(report["review_status"], report["review_status"])

    with st.expander(f"{label}　{report.get('subject', '')}　"
                      f"（{report.get('period_start', '')}〜{report.get('period_end', '')}）"):
        st.caption(f"report_id: {report_id}　記事数: {len(articles)}件")

        if report.get("status") == "error":
            st.warning(f"ドラフト生成中にエラーが発生しています: {report.get('error_message')}")

        if report.get("html_body"):
            st.components.v1.html(report["html_body"], height=700, scrolling=True)
        else:
            st.info("メール本文がまだ生成されていません。")

        _render_geo_intelligence_summary(report, config)

        st.divider()
        st.markdown("**レビュー**")
        reviewer_id = st.text_input("レビュー担当者ID", key=f"weekly_reviewer_{report_id}")
        free_text = st.text_area("自由記述フィードバック", key=f"weekly_free_{report_id}", height=60)

        edited_json_text = st.text_area(
            "週次ドラフト本体（修正する場合はJSONを直接編集）",
            value=json.dumps(draft, ensure_ascii=False, indent=2),
            key=f"weekly_json_{report_id}", height=200)

        btn_col1, btn_col2, btn_col3 = st.columns(3)
        for label_text, status_value, col in (
            ("✅ 承認して送信", "approved", btn_col1),
            ("🚫 却下", "rejected", btn_col2),
            ("💾 修正のみ保存（レビュー待ちのまま）", "review_required", btn_col3),
        ):
            with col:
                if st.button(label_text, key=f"weekly_btn_{status_value}_{report_id}",
                             use_container_width=True, disabled=report["review_status"] == "rejected"):
                    body = {
                        "status": status_value, "reviewer_id": reviewer_id,
                        "reviewer_feedback": {"free_text": free_text or None},
                    }
                    try:
                        edited_draft = json.loads(edited_json_text)
                        if edited_draft != draft:
                            body["draft_content"] = edited_draft
                    except json.JSONDecodeError:
                        st.error("週次ドラフト本体のJSONが不正です。修正内容は保存されませんでした。")
                    else:
                        result = expert_api.review_weekly_report(report_id, body, config)
                        if result.get("ok"):
                            st.success("保存しました")
                            st.rerun()
                        else:
                            st.error(f"処理に失敗しました: {result.get('error')}")


def _render_weekly_review_view(config: dict):
    client = _get_client(config)
    with st.sidebar:
        status_filter = st.selectbox(
            "表示するステータス", ["review_required", "approved", "rejected", "sent", "すべて"],
            format_func=lambda s: WEEKLY_STATUS_LABELS.get(s, "📋 すべて"), key="weekly_status_filter")
        if st.button("🔄 更新", use_container_width=True, key="weekly_refresh"):
            st.rerun()

    try:
        reports = wer.list_reports(client, review_status=None if status_filter == "すべて" else status_filter)
    except Exception as e:
        st.error(f"weekly_email_reports の取得に失敗しました: {e}"
                 "（sql/2026-07-22_weekly_email_review_gate_schema.sql の適用が必要な可能性があります）")
        return

    if not reports:
        st.info("該当する週次ドラフトがありません。先に `python weekly_email_report.py build` を実行してください。")
        return

    st.caption(f"{len(reports)}件")
    for report in reports:
        _render_weekly_report_card(report, config)


def _render_daily_digest_card(digest: dict, config: dict):
    """1日分の変更イベントダイジェストをカード表示する。auto_sent/sentは参照のみ、
    review_requiredのみ承認/却下ボタンを表示する"""
    digest_id = digest["digest_id"]
    label = COMPETITOR_ALERT_STATUS_LABELS.get(
        "auto_sent" if digest.get("auto_send_eligible") and digest["review_status"] == "sent"
        else digest["review_status"], digest["review_status"])
    event_count = len(digest.get("change_event_ids") or [])

    with st.expander(f"{label}　{digest.get('subject', '')}　（{digest.get('digest_date', '')}）"):
        st.caption(f"digest_id: {digest_id}　変更イベント数: {event_count}件　"
                   f"自動配信対象日: {'はい' if digest.get('auto_send_eligible') else 'いいえ'}")

        if digest.get("html_body"):
            st.components.v1.html(digest["html_body"], height=600, scrolling=True)
        else:
            st.info("メール本文がまだ生成されていません。")

        if digest["review_status"] != "review_required":
            st.caption(f"reviewer_id: {digest.get('reviewer_id') or '—'}　"
                       f"send_status: {digest.get('send_status') or '—'}")
            return

        st.divider()
        st.markdown("**レビュー**")
        reviewer_id = st.text_input("レビュー担当者ID", key=f"digest_reviewer_{digest_id}")
        free_text = st.text_area("自由記述フィードバック", key=f"digest_free_{digest_id}", height=60)

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("✅ 承認して送信", key=f"digest_approve_{digest_id}", use_container_width=True):
                client = _get_client(config)
                result = daily_digest.approve_and_send(
                    client, config, digest_id, reviewer_id or "unknown",
                    reviewer_feedback={"free_text": free_text or None})
                if result.get("ok"):
                    st.success("承認・送信しました")
                    st.rerun()
                else:
                    st.error(f"処理に失敗しました: {result.get('error')}")
        with btn_col2:
            if st.button("🚫 却下", key=f"digest_reject_{digest_id}", use_container_width=True):
                client = _get_client(config)
                daily_digest.reject_digest(
                    client, digest_id, reviewer_id or "unknown",
                    reviewer_feedback={"free_text": free_text or None})
                st.success("却下しました")
                st.rerun()


def _render_daily_digest_view(config: dict):
    client = _get_client(config)
    with st.sidebar:
        status_filter = st.selectbox(
            "表示するステータス", ["review_required", "approved", "rejected", "sent", "すべて"],
            format_func=lambda s: COMPETITOR_ALERT_STATUS_LABELS.get(s, "📋 すべて"),
            key="digest_status_filter")
        if st.button("🔄 更新", use_container_width=True, key="digest_refresh"):
            st.rerun()

    try:
        digests = daily_digest.list_digests(
            client, review_status=None if status_filter == "すべて" else status_filter)
    except Exception as e:
        st.error(f"competitor_daily_alert_digests の取得に失敗しました: {e}"
                 "（sql/2026-07-27d_daily_alert_digest.sql の適用が必要な可能性があります）")
        return

    if not digests:
        st.info("該当する日次ダイジェストがありません。先に `python competitor_crawler.py` を実行してください。")
        return

    st.caption(f"{len(digests)}件")
    for digest in digests:
        _render_daily_digest_card(digest, config)


def _render_competitor_monthly_card(report: dict, config: dict):
    report_id = report["report_id"]
    label = COMPETITOR_MONTHLY_STATUS_LABELS.get(report["status"], report["status"])
    summary = report.get("summary_json") or {}

    with st.expander(f"{label}　{report.get('subject', '')}　（{report.get('report_month', '')}）"):
        st.caption(f"report_id: {report_id}　対象期間: {report.get('period_start', '')}〜{report.get('period_end', '')}")
        if summary:
            st.caption(f"監視対象{summary.get('monitored_companies', '—')}社　"
                       f"更新企業{summary.get('updated_companies', '—')}社　"
                       f"目標変更{summary.get('target_change_count', '—')}件　"
                       f"取組{summary.get('initiative_count', '—')}件")

        if report.get("html_body"):
            st.components.v1.html(report["html_body"], height=700, scrolling=True)
        else:
            st.info("メール本文がまだ生成されていません。")

        if report["status"] not in ("GENERATED", "UNDER_REVIEW", "REVISION_REQUIRED"):
            st.caption(f"reviewed_by: {report.get('reviewed_by') or '—'}　"
                       f"send_status: {report.get('send_status') or '—'}")
            return

        st.divider()
        st.markdown("**レビュー**")
        reviewer_id = st.text_input("レビュー担当者ID", key=f"comp_monthly_reviewer_{report_id}")
        free_text = st.text_area("自由記述フィードバック", key=f"comp_monthly_free_{report_id}", height=60)

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("✅ 承認して送信", key=f"comp_monthly_approve_{report_id}", use_container_width=True):
                client = _get_client(config)
                result = monthly_competitor.approve_and_send(
                    client, config, report_id, reviewer_id or "unknown",
                    reviewer_feedback={"free_text": free_text or None})
                if result.get("ok"):
                    st.success("承認・送信しました")
                    st.rerun()
                else:
                    st.error(f"処理に失敗しました: {result.get('error')}")
        with btn_col2:
            if st.button("🚫 却下", key=f"comp_monthly_reject_{report_id}", use_container_width=True):
                client = _get_client(config)
                monthly_competitor.reject_report(
                    client, report_id, reviewer_id or "unknown",
                    reviewer_feedback={"free_text": free_text or None})
                st.success("却下しました")
                st.rerun()


def _render_competitor_monthly_view(config: dict):
    client = _get_client(config)
    with st.sidebar:
        status_filter = st.selectbox(
            "表示するステータス",
            ["GENERATED", "APPROVED", "SENT", "CANCELLED", "すべて"],
            format_func=lambda s: COMPETITOR_MONTHLY_STATUS_LABELS.get(s, "📋 すべて"),
            key="comp_monthly_status_filter")
        if st.button("🔄 更新", use_container_width=True, key="comp_monthly_refresh"):
            st.rerun()

    try:
        reports = monthly_competitor.list_reports(
            client, status=None if status_filter == "すべて" else status_filter)
    except Exception as e:
        st.error(f"monthly_reports の取得に失敗しました: {e}"
                 "（sql/2026-07-27g_monthly_report_redesign.sql の適用が必要な可能性があります）")
        return

    if not reports:
        st.info("該当する競合月次ドラフトがありません。先に `python monthly_competitor_report.py build` を実行してください。")
        return

    st.caption(f"{len(reports)}件")
    for report in reports:
        _render_competitor_monthly_card(report, config)


def _render_candidate_card(candidate: dict, config: dict):
    candidate_id = candidate["candidate_id"]
    status = "✅ 有効" if candidate["is_active"] else "🕒 未確認"
    crawlable = "○" if candidate.get("is_crawlable") else "×（直近チェック時に取得失敗）"

    with st.expander(f"{status}　[{candidate.get('url_type_candidate')}]　"
                      f"{candidate.get('page_title') or candidate['url']}"):
        st.markdown(f"**URL**: [{candidate['url']}]({candidate['url']})")
        st.caption(f"リンク元source_id: {candidate.get('discovered_from_source_id')}　"
                   f"最終確認日時: {candidate.get('last_checked_at')}　クロール可否: {crawlable}")

        if candidate["is_active"]:
            if st.button("🚫 無効化", key=f"cand_deactivate_{candidate_id}"):
                client = _get_client(config)
                source_discovery.deactivate_candidate(client, candidate_id)
                st.success("無効化しました")
                st.rerun()
            return

        chosen_type = st.selectbox(
            "種別（自動推測。誤っていれば修正してから有効化してください）",
            CANDIDATE_URL_TYPES,
            index=CANDIDATE_URL_TYPES.index(candidate.get("url_type_candidate", "OTHER")),
            key=f"cand_type_{candidate_id}")
        if st.button("✅ 有効化して監視対象に追加", key=f"cand_activate_{candidate_id}"):
            client = _get_client(config)
            result = source_discovery.activate_candidate(client, candidate_id, chosen_type)
            if result.get("ok"):
                st.success("有効化しました")
                st.rerun()
            else:
                st.error(f"処理に失敗しました: {result.get('error')}")


def _render_candidate_view(config: dict):
    client = _get_client(config)
    with st.sidebar:
        status_filter = st.selectbox("表示するステータス", ["未確認", "有効", "すべて"], key="cand_status_filter")
        if st.button("🔄 更新", use_container_width=True, key="cand_refresh"):
            st.rerun()

    is_active = {"未確認": False, "有効": True, "すべて": None}[status_filter]
    try:
        candidates = source_discovery.list_candidates(client, is_active=is_active)
    except Exception as e:
        st.error(f"competitor_source_candidates の取得に失敗しました: {e}"
                 "（sql/2026-07-27c_source_types_and_candidates.sql の適用が必要な可能性があります）")
        return

    if not candidates:
        st.info("候補URLがまだありません。先に `python competitor_crawler.py` を実行してください"
                 "（SUSTAINABILITY_HOME種別の情報源から自動発見されます）。")
        return

    st.caption(f"{len(candidates)}件")
    for candidate in candidates:
        _render_candidate_card(candidate, config)


def _render_strategic_question_view(config: dict):
    """Weekly Strategic Questionの閲覧画面。承認アクションは持たない（承認は
    週次メールレビュー画面のJSON編集にバンドルされている）。質問の履歴と、
    LLM失敗でinsight_status='error'のまま残っている質問への注意喚起、
    蓄積されたDecision Insight（組織内の判断傾向）の一覧を表示する"""
    client = _get_client(config)

    st.markdown("### 戦略質問の履歴")
    try:
        questions = sq_service.list_questions(client)
    except Exception as e:
        st.error(f"sustainability_strategic_questions の取得に失敗しました: {e}"
                 "（sql/2026-09-01_strategic_question_schema.sql の適用が必要な可能性があります）")
        return

    error_questions = [q for q in questions if q.get("insight_status") == "error"]
    if error_questions:
        st.warning(f"⚠️ Decision Insight生成が失敗したまま残っている質問が{len(error_questions)}件あります。"
                   "`python generate_strategic_question.py insights` を再実行してください。")
        for q in error_questions:
            st.caption(f"  - {q['period_start']} {q['title']}: {q.get('insight_error_message', '')}")

    if not questions:
        st.info("まだ戦略質問がありません。`python generate_strategic_question.py build` を実行してください。")
    for q in questions:
        lifecycle = {"draft": "📝 下書き（未embedded）", "embedded": "📨 週次メールに埋め込み済み（承認待ち）",
                     "open": "🟢 回答受付中", "closed": "📊 クローズ済み"}.get(q["question_status"], q["question_status"])
        with st.expander(f"{lifecycle}　{q.get('period_start', '')}　[{q.get('theme', '')}] {q.get('title', '')}"):
            st.caption(f"question_id: {q['question_id']}　対立軸: {q.get('decision_dimension_label', '')}"
                       f"（{q.get('decision_dimension_key', '')}）")
            st.markdown(f"**{q.get('question_text', '')}**")
            for o in q.get("_options", []):
                badge = "（現状維持寄り）" if o.get("is_status_quo") else ""
                st.write(f"- {o['option_code']}. {o['label']}{badge}: {o.get('description', '')}")
            with st.expander("分析の根拠（Signal→示唆→当社戦略関連→競合比較→対立軸）"):
                st.json(q.get("analysis_json", {}))
            with st.expander("他の候補・ランキング根拠"):
                st.json({"candidates": q.get("candidates_json", []), "ranking_score": q.get("ranking_score"),
                         "ranking_reasons": q.get("ranking_reasons", [])})
            if q["question_status"] in ("open", "closed"):
                try:
                    import strategic_question_responses as sqr
                    agg = sqr.aggregate_results(client, q["question_id"])
                    st.caption(f"回答数: {agg['total_responses']} / 配信数: {agg['delivery_count']} "
                               f"/ 回答率: {agg['response_rate']*100:.1f}%")
                except Exception:
                    pass

    st.divider()
    st.markdown("### 組織判断ナレッジ一覧（Decision Insight）")
    st.caption("いずれも「観測された傾向」であり、会社の正式方針ではありません。")
    insights = client.select("sustainability_decision_insights", {"select": "*", "order": "observed_at.desc"})
    if not insights:
        st.info("蓄積された組織判断ナレッジがまだありません。")
    for ins in insights:
        publishable = "🟢 掲載条件を満たす" if _is_publishable(ins, config) else "⚪ 回答数/回答率が閾値未満（非掲載）"
        with st.expander(f"{publishable}　[{ins.get('theme', '')}] {ins.get('decision_dimension_label', '')}"):
            st.caption(f"observed_at: {ins.get('observed_at', '')}　回答数: {ins.get('response_count')} / "
                       f"配信数: {ins.get('delivery_count')}　confidence: {ins.get('confidence')}")
            st.warning(ins.get("guardrail_label", ""))
            st.write(ins.get("observed_tendency", ""))
            st.json(ins.get("distribution_json", {}))
            st.write("**理由の要約**:", ins.get("reasoning_summary", ""))
            if ins.get("representative_reasoning"):
                st.write("**代表的な理由づけパターン**:")
                for r in ins["representative_reasoning"]:
                    st.write(f"- {r}")


def _is_publishable(insight: dict, config: dict) -> bool:
    import decision_insight_service as insight_service
    try:
        return insight_service.publishable(insight, config)
    except Exception:
        return False


def main():
    st.set_page_config(page_title="サステナビリティ専門家レビュー", page_icon="🌿", layout="wide")
    config = _load_config()

    st.title("🌿 サステナビリティ専門家 レビュー")

    if not common.is_enabled(config):
        st.warning("⚠️ SUSTAINABILITY_EXPERT_ENABLED が無効です。config.json の "
                   "sustainability_expert.enabled を true にするか、環境変数で有効化してください。")
        return

    with st.sidebar:
        view = st.radio("表示", [
            "週次メールレビュー", "競合アラート日次ダイジェスト", "競合月次レビュー",
            "競合クロール候補管理", "戦略質問・組織判断ナレッジ",
        ])

    if view == "週次メールレビュー":
        st.caption("週次メールのドラフトは自動送信されません。内容を確認のうえ、承認して送信/却下/修正してください。")
        _render_weekly_review_view(config)
    elif view == "競合アラート日次ダイジェスト":
        st.caption("その日確定した変更イベントをまとめた日次ダイジェストです。"
                   "確信度の高い日は自動配信済みのため参照のみ、それ以外は内容を確認のうえ承認して送信/却下してください。")
        _render_daily_digest_view(config)
    elif view == "競合月次レビュー":
        st.caption("競合サステナビリティ月次レポートのドラフトは自動送信されません。"
                   "内容を確認のうえ、承認して送信/却下してください。")
        _render_competitor_monthly_view(config)
    elif view == "戦略質問・組織判断ナレッジ":
        st.caption("戦略質問の承認は週次メールレビュー画面のJSON編集にバンドルされています"
                   "（このページは閲覧専用です）。")
        _render_strategic_question_view(config)
    else:
        st.caption("サステナビリティ起点ページから自動発見された候補URLです。"
                   "種別を確認・修正し、問題なければ有効化して監視対象に追加してください。")
        _render_candidate_view(config)


main()
