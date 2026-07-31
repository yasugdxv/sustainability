-- =====================================================================
-- 週次メールレポート: レビュー承認ゲートの追加
-- 作成日: 2026-07-22
-- 対象: Supabase（PostgreSQL）。既存の weekly_email_reports への ALTER。
--
-- 背景: これまで weekly_email_report.py は「ドラフト生成→即送信」を1回のCLI実行で
-- 行い、人によるレビューを挟まずに配信していた。サスティナビリティ専門家MVP
-- （sustainability_article_selector.py / sustainability_content_generator.py）と
-- 二重運用になっていたのを1本のパイプラインに統合するにあたり、週次メールも
-- expert_contentsと同じ「review_required→approved/rejected→送信」のレビュー
-- ゲートを持たせる。
--
-- 既存カラムの意味は変えない: status/error_messageは引き続き「ドラフト生成
-- （書き換え＋シンセシス）が成功したか」を表す。送信結果は別イベントなので
-- send_status/send_error_messageに分離する。
-- =====================================================================

alter table public.weekly_email_reports
    add column if not exists draft_content    jsonb not null default '{}'::jsonb,
    add column if not exists html_body         text,
    add column if not exists review_status     text not null default 'review_required'
        check (review_status in ('review_required', 'approved', 'rejected', 'sent')),
    add column if not exists reviewer_id        text,
    add column if not exists reviewer_feedback  jsonb,
    add column if not exists reviewed_at        timestamptz,
    add column if not exists sent_at            timestamptz,
    add column if not exists send_status        text
        check (send_status is null or send_status in ('success', 'error')),
    add column if not exists send_error_message text;

create index if not exists weekly_email_reports_review_status_idx
    on public.weekly_email_reports(review_status);

comment on column public.weekly_email_reports.draft_content is
    '編集可能な週次ドラフト本体: {"articles": [...rewrite_article_for_digest結果+enrichment],
     "synthesis": {overview,highlights,theme_digests}, "since_days": N}。build_email()の入力そのもの';
comment on column public.weekly_email_reports.html_body is
    'draft_contentから直近に生成したHTML（レビュー画面プレビュー用キャッシュ、
     承認時にそのまま送信本文として使う）';
comment on column public.weekly_email_reports.review_status is
    'review_required→approved/rejected→sent の状態遷移。
     サスティナビリティ専門家MVPのexpert_contents.statusと同じ考え方';
comment on column public.weekly_email_reports.send_status is
    '実際の送信（SMTP/プレビュー保存）が成功したか。ドラフト生成の成否を表すstatusとは別イベント';
