-- =====================================================================
-- [新規構築時は以下を使用]
--   1節・2節（crawl_logs ALTER）→ sql/build/01_create_schema.sql のCREATE TABLE本体に統合済み
--   3節（weekly_email_reports）→ sql/build/weekly_email/01_create_schema.sql
-- 本ファイルは移行（既存環境へのALTER適用）用・変更履歴として残置。
-- =====================================================================
-- 追加スキーマ: crawl_logsの抽出失敗可視化 + 週次メール実行ログ
-- 作成日: 2026-07-19
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   1. crawl_logs.run_result に '抽出失敗'（候補は見つかったが全件本文抽出に
--      失敗した状態）を追加し、'更新なし'（純粋に新着が無かった状態）と
--      区別できるようにする。
--   2. crawl_logs に extraction_failures 列を追加し、抽出失敗件数を保存する
--      （これまでprocess_target()内で計算だけして捨てられていた）。
--   3. weekly_email_reports（新設）— 週次メールレポートの実行ログ。
--      expert_runsと同じ考え方で、対象記事・トークン使用量・送信結果を記録する。
-- =====================================================================


-- =====================================================================
-- 1. crawl_logs.run_result に '抽出失敗' を追加
-- =====================================================================
do $$
declare
    con record;
begin
    for con in
        select conname
        from pg_constraint
        where conrelid = 'public.crawl_logs'::regclass
          and contype = 'c'
          and pg_get_constraintdef(oid) ilike '%run_result%'
    loop
        execute format('alter table public.crawl_logs drop constraint %I', con.conname);
    end loop;
end $$;

alter table public.crawl_logs
    add constraint crawl_logs_run_result_check
        check (run_result in ('成功', '一部成功', '失敗', '更新なし', '抽出失敗'));

comment on column public.crawl_logs.run_result is
    '成功/一部成功/失敗/更新なし(純粋に新着無し)/抽出失敗(候補はあったが全件本文抽出に失敗)';


-- =====================================================================
-- 2. crawl_logs.extraction_failures 列を追加
-- =====================================================================
alter table public.crawl_logs
    add column if not exists extraction_failures integer not null default 0
        check (extraction_failures >= 0);

comment on column public.crawl_logs.extraction_failures is
    '本文抽出（extract_article）に失敗した候補記事の件数。0件かつnew_items/updated_itemsも0件の場合は
     run_result=更新なし（純粋に新着無し）、1件以上ある場合は run_result=抽出失敗 または 一部成功 になる';


-- =====================================================================
-- 3. weekly_email_reports（新設）
-- =====================================================================
create table public.weekly_email_reports (
    report_id            uuid primary key default gen_random_uuid(),

    period_start          date not null,
    period_end            date not null,
    article_ids           uuid[] not null default '{}',

    model_deployment       text,
    prompt_version         text,
    token_usage            jsonb,
    latency_ms             integer
        check (latency_ms is null or latency_ms >= 0),

    status                 text not null
        check (status in ('success', 'error')),
    error_message           text,

    send_mode               text
        check (send_mode is null or send_mode in ('smtp', 'preview')),
    recipients               text[],
    subject                  text,

    created_at               timestamptz not null default now()
);

create index weekly_email_reports_period_idx
    on public.weekly_email_reports(period_start desc);

comment on table public.weekly_email_reports is
    '週次メールレポート(weekly_email_report.py)の実行ログ。対象記事・トークン使用量・
     送信結果を記録する（サスティナビリティ専門家MVPのexpert_runsとは別系統）';
comment on column public.weekly_email_reports.article_ids is
    'このレポートに掲載した記事(articles.article_id)の一覧';
comment on column public.weekly_email_reports.token_usage is
    '記事ごとの見出し・要点生成＋週次シンセシスまで含めた合計トークン使用量。
     例: {"prompt_tokens": 12345, "completion_tokens": 6789, "total_tokens": 19134, "reasoning_tokens": 0}';

alter table public.weekly_email_reports enable row level security;
