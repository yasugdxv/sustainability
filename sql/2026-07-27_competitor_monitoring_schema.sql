-- =====================================================================
-- 競合サステナビリティモニタリング機能: 新規テーブル一式
-- 作成日: 2026-07-27
-- 対象: Supabase（PostgreSQL）。新規構築、ALTER/DROP/移行処理は含まない。
--
-- 前提: 以下が既に作成済みであること
--   public.set_updated_at()（共通トリガー関数、sql/2026-07-15_crawl_and_tag_schema_v2.sql）
--
-- 競合企業の公式サステナビリティ情報（目標/KPI/実績/ESG評価/取組事例）を継続監視し、
-- 変更検知・構造化・即時アラート・PMOレビュー・月次メールを行うための一式。
-- 既存の記事クロール(article_crawler.py)・週次メール(weekly_email_report.py)と同じ
-- 設計思想（review_status: review_required→approved/rejected→sent のレビューゲート等）
-- を踏襲するが、対象・テーブルは完全に独立させる（既存のcrawl_targets/articles等には
-- 一切手を加えない）。
--
-- 作成順: competitor_companies → competitor_sources → competitor_crawl_logs
--        → competitor_target_records → competitor_initiatives
--        → competitor_change_events → competitor_alerts
--        → competitor_recipients → monthly_competitor_reports
--        → competitor_audit_log
-- =====================================================================


-- ---- 1. competitor_companies（企業マスタ） ----

create table public.competitor_companies (
    company_id      uuid primary key default gen_random_uuid(),
    company_name    text not null,
    company_name_en text,
    is_own_company  boolean not null default false,
    notes           text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create unique index competitor_companies_name_idx
    on public.competitor_companies(company_name);

create trigger competitor_companies_set_updated_at
    before update on public.competitor_companies
    for each row execute function public.set_updated_at();

alter table public.competitor_companies enable row level security;

comment on table public.competitor_companies is
    '競合監視の対象企業マスタ。比較用にサントリー自身も is_own_company=true で1行登録する';


-- ---- 2. competitor_sources（企業ごとの公式情報源、複数可） ----

create table public.competitor_sources (
    source_id   uuid primary key default gen_random_uuid(),
    company_id  uuid not null references public.competitor_companies(company_id) on delete cascade,
    source_url  text not null,
    source_type text not null
        check (source_type in (
            'holding', 'business_unit', 'sustainability_site',
            'newsroom', 'report_site', 'brand_site'
        )),
    crawl_method text
        check (crawl_method is null or crawl_method in ('HTML', 'RSS', 'API', 'ブラウザ操作', '手動')),
    lookback_days integer
        check (lookback_days is null or lookback_days >= 0),
    notes       text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create unique index competitor_sources_url_idx on public.competitor_sources(source_url);
create index competitor_sources_company_idx on public.competitor_sources(company_id);

create trigger competitor_sources_set_updated_at
    before update on public.competitor_sources
    for each row execute function public.set_updated_at();

alter table public.competitor_sources enable row level security;

comment on table public.competitor_sources is
    '1社に対して持株会社サイト/事業会社サイト/サステナ専用サイト/ニュースルーム/レポート配布サイト/'
    'ブランドサイト等、複数の公式情報源を紐づけるためのテーブル';


-- ---- 3. competitor_crawl_logs（クロール監視ログ） ----

create table public.competitor_crawl_logs (
    crawl_log_id  uuid primary key default gen_random_uuid(),
    source_id     uuid not null references public.competitor_sources(source_id) on delete restrict,
    started_at    timestamptz not null,
    finished_at   timestamptz
        check (finished_at is null or finished_at >= started_at),
    run_result    text not null
        check (run_result in ('成功', '一部成功', '失敗', '更新なし', '抽出失敗')),
    http_status   integer
        check (http_status is null or (http_status >= 100 and http_status < 600)),
    items_detected integer not null default 0 check (items_detected >= 0),
    new_items      integer not null default 0 check (new_items >= 0),
    updated_items  integer not null default 0 check (updated_items >= 0),
    extraction_failures integer not null default 0 check (extraction_failures >= 0),
    error_message  text,
    created_at     timestamptz not null default now()
);

create index competitor_crawl_logs_source_idx on public.competitor_crawl_logs(source_id);
create index competitor_crawl_logs_started_idx on public.competitor_crawl_logs(started_at desc);

alter table public.competitor_crawl_logs enable row level security;

comment on table public.competitor_crawl_logs is
    'competitor_crawler.pyの実行結果を1回の情報源アクセスにつき1行記録するクロール監視ログ';


-- ---- 4. competitor_target_records（競合目標DB: TARGET/KPI/ACTUAL/ESG_RATING） ----

create table public.competitor_target_records (
    record_id        uuid primary key default gen_random_uuid(),
    company_id        uuid not null references public.competitor_companies(company_id) on delete cascade,
    source_id         uuid references public.competitor_sources(source_id) on delete set null,
    record_type       text not null
        check (record_type in ('TARGET', 'KPI', 'ACTUAL', 'ESG_RATING')),
    structured_fields jsonb not null default '{}'::jsonb,
    raw_text_hash     text,
    source_url        text,
    is_current        boolean not null default true,
    superseded_by      uuid references public.competitor_target_records(record_id) on delete set null,
    extracted_at       timestamptz not null default now(),
    created_at         timestamptz not null default now()
);

create index competitor_target_records_company_idx
    on public.competitor_target_records(company_id, record_type);
create index competitor_target_records_current_idx
    on public.competitor_target_records(company_id, record_type) where is_current;

alter table public.competitor_target_records enable row level security;

comment on table public.competitor_target_records is
    '競合目標DB。TARGET/KPI/ACTUAL/ESG_RATINGの構造化レコード。'
    '同一company_id+record_typeでバージョン管理し（is_current/superseded_by）、'
    '最新版どうしの差分をcompetitor_change_detector.pyが機械比較する';
comment on column public.competitor_target_records.structured_fields is
    '要件定義6.1の構造化項目一式: target_value/numeric_value/unit/reduction_rate/'
    'base_year/target_year/interim_target_year/target_company/target_region/target_site/'
    'target_product/target_material/target_packaging/scope/boundary/kpi_definition/'
    'achievement_status/actual_fiscal_year/actual_value/esg_score/esg_rank/selection_status'
    '（record_typeにより実際に埋まる項目は異なる）';


-- ---- 5. competitor_initiatives（競合取組事例DB） ----

create table public.competitor_initiatives (
    initiative_id  uuid primary key default gen_random_uuid(),
    company_id      uuid not null references public.competitor_companies(company_id) on delete cascade,
    source_id       uuid references public.competitor_sources(source_id) on delete set null,
    is_new          boolean not null default true,
    title           text not null,
    summary         text,
    source_url      text,
    detected_at      timestamptz not null default now(),
    included_in_monthly_report boolean not null default false,
    created_at       timestamptz not null default now()
);

create index competitor_initiatives_company_idx on public.competitor_initiatives(company_id);
create index competitor_initiatives_monthly_idx
    on public.competitor_initiatives(included_in_monthly_report);

alter table public.competitor_initiatives enable row level security;

comment on table public.competitor_initiatives is
    '競合取組事例DB。新規取組事例・既存取組の更新を蓄積する（即時アラート対象外、月次メール候補）';


-- ---- 6. competitor_change_events（変更イベントDB） ----

create table public.competitor_change_events (
    change_event_id uuid primary key default gen_random_uuid(),
    company_id       uuid not null references public.competitor_companies(company_id) on delete cascade,
    record_type      text not null
        check (record_type in ('TARGET', 'KPI', 'ACTUAL', 'ESG_RATING')),
    before_record_id  uuid references public.competitor_target_records(record_id) on delete set null,
    after_record_id   uuid not null references public.competitor_target_records(record_id) on delete cascade,

    change_status     text not null
        check (change_status in ('NO_CHANGE', 'CHANGE_CONFIRMED')),
    change_type       text,
    changed_fields     jsonb not null default '[]'::jsonb,
    direction          text
        check (direction is null or direction in ('STRENGTHENED', 'WEAKENED', 'NEUTRAL')),
    summary            text,
    reasoning_summary   text,
    confidence          numeric
        check (confidence is null or (confidence >= 0 and confidence <= 1)),
    review_required     boolean not null default false,
    review_reasons       jsonb not null default '[]'::jsonb,
    llm_raw_output        jsonb,

    created_at            timestamptz not null default now()
);

create index competitor_change_events_company_idx on public.competitor_change_events(company_id);
create index competitor_change_events_review_idx
    on public.competitor_change_events(review_required);
create index competitor_change_events_created_idx
    on public.competitor_change_events(created_at desc);

alter table public.competitor_change_events enable row level security;

comment on table public.competitor_change_events is
    '変更イベントDB。competitor_change_detector.pyの機械比較＋LLM判定の結果を1件の変更につき1行保存する';
comment on column public.competitor_change_events.llm_raw_output is
    '要件定義6.2のLLM出力JSONをそのまま保存（record_type/same_entity/change_status/change_type/'
    'changed_fields/direction/summary/reasoning_summary/confidence/review_required/review_reasons）';


-- ---- 7. competitor_alerts（即時アラート） ----

create table public.competitor_alerts (
    alert_id         uuid primary key default gen_random_uuid(),
    change_event_id   uuid not null references public.competitor_change_events(change_event_id) on delete cascade,

    alert_status       text not null default 'review_required'
        check (alert_status in ('auto_sent', 'review_required', 'approved', 'rejected', 'sent', 'error')),
    subject             text,
    html_body           text,

    reviewer_id          text,
    reviewer_feedback     jsonb,
    reviewed_at           timestamptz,

    sent_at               timestamptz,
    send_status           text
        check (send_status is null or send_status in ('success', 'error')),
    send_error_message     text,
    recipients             text[],

    created_at              timestamptz not null default now()
);

create index competitor_alerts_change_event_idx on public.competitor_alerts(change_event_id);
create index competitor_alerts_status_idx on public.competitor_alerts(alert_status);

alter table public.competitor_alerts enable row level security;

comment on table public.competitor_alerts is
    '即時アラート。変更確定後、自動配信可否の判定結果と（必要な場合の）PMOレビュー・送信結果を記録する。'
    'alert_status=auto_sentは自動配信済み、review_required以降は既存weekly_email_reportsと同じ'
    'レビューゲートの考え方を踏襲';


-- ---- 8. competitor_recipients（配信先DB） ----

create table public.competitor_recipients (
    recipient_id             uuid primary key default gen_random_uuid(),
    name                      text not null,
    email                      text not null,
    notify_immediate_alert     boolean not null default true,
    notify_monthly_report       boolean not null default true,
    active                       boolean not null default true,
    created_at                   timestamptz not null default now(),
    updated_at                    timestamptz not null default now()
);

create unique index competitor_recipients_email_idx on public.competitor_recipients(email);

create trigger competitor_recipients_set_updated_at
    before update on public.competitor_recipients
    for each row execute function public.set_updated_at();

alter table public.competitor_recipients enable row level security;

comment on table public.competitor_recipients is
    '即時アラート・月次メールの配信先。config.jsonのemail.to_addressesとは別に、'
    '通知種別ごとにON/OFFできる配信先リストとしてDBで管理する';


-- ---- 9. monthly_competitor_reports（月次メール） ----

create table public.monthly_competitor_reports (
    report_id           uuid primary key default gen_random_uuid(),

    period_start          date not null,
    period_end            date not null,
    change_event_ids       uuid[] not null default '{}',
    initiative_ids          uuid[] not null default '{}',

    model_deployment         text,
    token_usage               jsonb,
    latency_ms                integer
        check (latency_ms is null or latency_ms >= 0),

    status                     text not null
        check (status in ('success', 'error')),
    error_message                text,

    draft_content                 jsonb not null default '{}'::jsonb,
    html_body                      text,
    subject                          text,

    review_status                     text not null default 'review_required'
        check (review_status in ('review_required', 'approved', 'rejected', 'sent')),
    reviewer_id                        text,
    reviewer_feedback                   jsonb,
    reviewed_at                          timestamptz,

    sent_at                               timestamptz,
    send_status                           text
        check (send_status is null or send_status in ('success', 'error')),
    send_error_message                     text,
    send_mode                               text
        check (send_mode is null or send_mode in ('smtp', 'preview')),
    recipients                               text[],

    created_at                                 timestamptz not null default now()
);

create index monthly_competitor_reports_period_idx
    on public.monthly_competitor_reports(period_start desc);
create index monthly_competitor_reports_review_status_idx
    on public.monthly_competitor_reports(review_status);

alter table public.monthly_competitor_reports enable row level security;

comment on table public.monthly_competitor_reports is
    '月次メール(monthly_competitor_report.py)のドラフト〜送信ログ。'
    'weekly_email_reportsと同じreview_status遷移（review_required→approved/rejected→sent）を踏襲する';


-- ---- 10. competitor_audit_log（監査ログ） ----

create table public.competitor_audit_log (
    audit_id     uuid primary key default gen_random_uuid(),
    entity_type   text not null,
    entity_id      uuid,
    action          text not null,
    actor            text not null,
    detail            jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now()
);

create index competitor_audit_log_entity_idx
    on public.competitor_audit_log(entity_type, entity_id);
create index competitor_audit_log_created_idx
    on public.competitor_audit_log(created_at desc);

alter table public.competitor_audit_log enable row level security;

comment on table public.competitor_audit_log is
    '配信・判定・レビュー操作の監査ログ。entity_type(例: change_event/alert/monthly_report)＋'
    'entity_idで対象を、actor(system/llm/レビュー担当者ID)で誰が行った操作かを記録する';


-- =====================================================================
-- ダミーシードデータ（骨組み確認用。実際の競合20社リストは未確定のため、
-- サントリー(自社比較用)＋ダミー2社のみ投入する。実データ投入時はこのINSERT文は
-- 削除・置き換えを前提とする）
-- =====================================================================

insert into public.competitor_companies (company_name, company_name_en, is_own_company, notes) values
    ('サントリー', 'Suntory', true, '自社。比較基準として登録'),
    ('A飲料（ダミー）', 'Beverage A (dummy)', false, '骨組み検証用のダミー企業。実データ投入時に置き換える'),
    ('Bビバレッジ（ダミー）', 'Beverage B (dummy)', false, '骨組み検証用のダミー企業。実データ投入時に置き換える');

insert into public.competitor_sources (company_id, source_url, source_type, crawl_method, lookback_days, notes)
select company_id, 'https://example.com/dummy-sustainability-a', 'sustainability_site', 'HTML', 90,
       '骨組み検証用のダミーURL'
from public.competitor_companies where company_name = 'A飲料（ダミー）';

insert into public.competitor_sources (company_id, source_url, source_type, crawl_method, lookback_days, notes)
select company_id, 'https://example.com/dummy-newsroom-b', 'newsroom', 'HTML', 90,
       '骨組み検証用のダミーURL'
from public.competitor_companies where company_name = 'Bビバレッジ（ダミー）';
