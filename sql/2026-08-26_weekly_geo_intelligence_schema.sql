-- Weekly x Geo Intelligence Batch Inquiry Phase S2
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   週次レポート生成時にGeo Intelligence側（weekly_monitoring request_type）へ
--   問い合わせた結果を管理するテーブルを新設する。
--
--   raw request/responseの正本は sql/2026-08-26_external_intelligence_calls_schema.sql の
--   external_intelligence_calls（Phase S1）。本テーブルはexternal_call_idで参照するのみで、
--   response_payloadを二重保存しない。
--
--   weekly_geo_intelligence_runs: Weekly 1回の問い合わせ（1週×1scope）の実行管理。
--   同一period_start/period_end/scope_versionでも履歴として複数行残る設計のため、
--   ユニーク制約は設けず、検索用の通常indexのみとする（force_rerun時の重複実行防止は
--   アプリケーション側のロジックで行う）。
--
--   weekly_geo_intelligence_items: 1回のrunで取得したrelevant_intelligence各要素と、
--   Sustainability記事とのdedup分類（same_event/related_context/independent）・
--   最終選定結果（selection_status）を保持する。

create table public.weekly_geo_intelligence_runs (
    id uuid primary key default gen_random_uuid(),
    period_start date not null,
    period_end date not null,
    scope_version text not null default 'v1',
    external_call_id uuid references public.external_intelligence_calls(id),  -- raw request/responseはこちらを参照
    local_request_id uuid,
    remote_request_id text,
    status text not null check (status in ('success','unavailable','failed','invalid_response','disabled')),
    remote_status text,
    scope_hash text,               -- sustainability_themes/priority_geographies/business_contextから算出。不一致ならGeo APIから再実行
    dedup_status text check (dedup_status is null or dedup_status in ('not_needed','completed','failed')),  -- 再利用条件の要
    dedup_input_hash text,        -- dedup_candidate_poolのarticle_id+更新時刻から算出。不一致ならDedupのみ再試行
    selection_status text check (selection_status is null or selection_status in ('not_needed','completed','failed')),  -- Run単位の集計ステータス（item単位のselection_statusとは別物）
    selection_result jsonb,       -- {"selected_independent_geo_item_ids":[...],"promoted_article_ids":[...],"displaced_article_ids":[...]}。独立Geoと昇格を区別して保持し、同一Weekly再生成時はこれを直接読み出して再現する
    selection_input_hash text,    -- final_articles+independent/promotion candidatesのID・種別+target_maxから算出。不一致ならSelectionのみ再試行
    request_scope jsonb,           -- 送信したscope（themes/geographies等）のみ保持。response本体は保持しない
    knowledge_sufficiency text,
    confidence text,
    item_count integer default 0,
    truncated_count integer default 0,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    error_type text,
    error_message text,
    created_at timestamptz not null default now()
);
-- ユニーク制約は設けない（履歴方式）。検索用の通常indexのみ。
create index weekly_geo_intelligence_runs_scope_idx
    on public.weekly_geo_intelligence_runs(period_start, period_end, scope_version, created_at desc);

create table public.weekly_geo_intelligence_items (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null references public.weekly_geo_intelligence_runs(id) on delete cascade,
    geo_item_id text,
    title text,
    event_date text,
    as_of text,                      -- Geo Response由来のトレーサビリティ情報
    key_stakeholders jsonb,
    expert_response_id text,
    country_region jsonb,
    sustainability_themes jsonb,
    geo_assessment text,
    why_relevant text,
    political_dynamics text,
    outlook text,
    "references" jsonb,
    confidence text,
    dedup_classification text check (dedup_classification is null or dedup_classification in ('same_event','related_context','independent')),
    matched_article_ids jsonb not null default '[]'::jsonb,  -- 複数article対応
    include_in_weekly boolean not null default false,        -- 最終選定後にのみtrueへ更新
    selection_status text check (selection_status is null or selection_status in ('selected','rejected','failed')),
    selection_reason text,
    selected_at timestamptz,
    content_origin text not null default 'geo_intelligence',
    created_at timestamptz not null default now()
);
create index weekly_geo_intelligence_items_run_idx on public.weekly_geo_intelligence_items(run_id);

alter table public.weekly_geo_intelligence_runs enable row level security;
alter table public.weekly_geo_intelligence_items enable row level security;

comment on table public.weekly_geo_intelligence_runs is
    '週次レポート生成時のGeo Intelligence（weekly_monitoring）問い合わせ1回分の実行管理。
     raw request/responseの正本はexternal_intelligence_calls（external_call_idで参照）。
     同一週でも履歴として複数行残る設計（ユニーク制約なし）。';
comment on table public.weekly_geo_intelligence_items is
    '1回のrunで取得したrelevant_intelligence各要素と、Sustainability記事とのdedup分類・
     最終選定結果を保持する。dedup_classification IS NULLの行は分類未了/失敗であり、
     Weekly側のどの構造にも使われない。';
