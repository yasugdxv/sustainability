-- =====================================================================
-- [新規構築時はsql/build/sustainability_expert/01_create_schema.sqlを使用]
-- 本ファイルは変更履歴として残置。内容は新規構築版と同一（ALTER無しの新設テーブルのため）。
-- =====================================================================
-- 当社サスティナビリティ専門家MVP 用スキーマ（新規追加のみ・既存テーブル無変更）
-- 作成日: 2026-07-17
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   1. expert_runs     — 記事選定/コンテンツ生成それぞれのLLM呼び出し1回を1行で記録する
--   2. expert_contents — publish_candidateから生成したコンテンツ候補（レビュー待ち）を保存する
--
-- 設計方針:
--   - 記事クラスタ(article_cluster_id)は、専用クラスタリング基盤を新設せず
--     既存の public.article_urls(article_url_id) をそのまま「事象クラスタの代表URL」
--     として再利用するアダプター方式にする（article_urls.duplicate_of_article_url_id
--     を辿って関連記事をまとめる処理はアプリケーション側で行う）。
--   - 選定(select)とコンテンツ生成(generate)は必ず別のLLM呼び出し・別のexpert_runs行にする。
--   - 同一クラスタ・同一入力・同一専門家バージョンでの重複LLM実行を防ぐため、
--     status='success'の行に限りユニーク制約をかける。
-- =====================================================================


-- =====================================================================
-- 1. expert_runs
-- =====================================================================
create table public.expert_runs (
    run_id              uuid primary key default gen_random_uuid(),
    article_cluster_id  uuid not null
        references public.article_urls(article_url_id) on delete cascade,

    task_type           text not null
        check (task_type in ('select', 'generate', 'validate')),

    model_deployment    text not null,
    prompt_version      text not null,
    expert_version      text not null,
    context_chunk_ids   text[] not null default '{}',
    input_hash          text not null,

    output_json         jsonb,
    token_usage         jsonb,
    latency_ms          integer
        check (latency_ms is null or latency_ms >= 0),

    status              text not null
        check (status in ('success', 'schema_invalid', 'error')),
    error_message        text,

    created_at           timestamptz not null default now()
);

create index expert_runs_cluster_idx on public.expert_runs(article_cluster_id);
create index expert_runs_task_type_idx on public.expert_runs(task_type);

-- 同一クラスタ・同一タスク種別・同一入力・同一専門家バージョンでの重複実行防止
-- （status='success'の行同士でのみ重複を禁止。エラーになった行は再実行できるようにする）
create unique index expert_runs_dedup_idx
    on public.expert_runs(article_cluster_id, task_type, input_hash, expert_version)
    where status = 'success';

comment on table public.expert_runs is
    'サスティナビリティ専門家MVP: 記事選定/コンテンツ生成それぞれのLLM呼び出し1回を1行で記録する実行ログ';
comment on column public.expert_runs.article_cluster_id is
    '事象クラスタの代表URL。実体は public.article_urls(article_url_id) を流用（専用クラスタテーブルは新設しない）';
comment on column public.expert_runs.context_chunk_ids is
    '選定/生成時に参照した当社公式コンテキストの文書チャンクID一覧（knowledge_documents.jsonlのid、またはAzure AI Searchのドキュメントid）';
comment on column public.expert_runs.input_hash is
    '代表記事本文+参照チャンクID+専門家バージョンから計算したSHA256ハッシュ。重複実行防止に使う';

alter table public.expert_runs enable row level security;


-- =====================================================================
-- 2. expert_contents
-- =====================================================================
create table public.expert_contents (
    content_id           uuid primary key default gen_random_uuid(),
    article_cluster_id   uuid not null
        references public.article_urls(article_url_id) on delete cascade,

    select_run_id         uuid
        references public.expert_runs(run_id) on delete set null,
    generate_run_id        uuid
        references public.expert_runs(run_id) on delete set null,

    title                 text not null,
    content_json           jsonb not null,

    status                 text not null default 'review_required'
        check (status in ('review_required', 'approved', 'rejected', 'published')),

    reviewer_id             text,
    -- 採用/不採用、スコア妥当性、不足観点、不要記述、追加観点、タイトル修正、
    -- 事実誤認、根拠不備、自由記述フィードバックをまとめて保持する
    -- （後からプロンプト・閾値を改善するための評価データ蓄積。専用テーブルは新設しない）
    reviewer_feedback       jsonb,

    expert_version          text not null,

    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),

    unique (article_cluster_id, generate_run_id)
);

create index expert_contents_cluster_idx on public.expert_contents(article_cluster_id);
create index expert_contents_status_idx on public.expert_contents(status);

create trigger expert_contents_set_updated_at
    before update on public.expert_contents
    for each row execute function public.set_updated_at();

comment on table public.expert_contents is
    'サスティナビリティ専門家MVP: publish_candidateから生成したコンテンツ候補。自動公開せず review_required で保存する';
comment on column public.expert_contents.content_json is
    '構造化コンテンツ本体（title/what_happened/why_it_matters/company_watchpoints/affected_themes/impact_pathways/questions_to_confirm/monitoring_signals/evidence/uncertainties）';
comment on column public.expert_contents.reviewer_feedback is
    '例: {"adopted": true, "score_valid": true, "missing_viewpoints": [...], "unnecessary_points": [...], "added_viewpoints": [...], "title_correction": "...", "factual_errors": [...], "evidence_issues": [...], "free_text": "..."}';

alter table public.expert_contents enable row level security;
