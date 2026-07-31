-- =====================================================================
-- 競合サステナビリティモニタリング: 月次メール全面リニューアル
-- 作成日: 2026-07-27（2026-07-27〜2026-07-27fの後に実行する）
--
-- 内容:
--   1. competitor_companies に display_order を追加（カテゴリ内の並び順）
--   2. competitor_target_records / competitor_initiatives に themes を追加
--      （記事ダッシュボード側と同じ9テーマ体系。分類・変更判定LLMの追加出力として付与）
--   3. 旧 monthly_competitor_reports（フラットな1レコード方式、実データ0件）を廃止し、
--      monthly_reports / monthly_report_companies / monthly_report_items の
--      3テーブル構成に置き換える（企業別・項目別にPMOが掲載可否・順序・要約を編集できるようにする）
-- =====================================================================

alter table public.competitor_companies
    add column if not exists display_order integer;

update public.competitor_companies set display_order = ord.n from (values
    ('ABインベブ', 1), ('ハイネケン', 2), ('カールスバーグ', 3),
    ('モルソン・クアーズ', 4), ('サッポロホールディングス', 5),
    ('ディアジオ', 6), ('ペルノ・リカール', 7), ('ブラウン・フォーマン', 8), ('バカルディ', 9),
    ('コカ・コーラ・カンパニー', 10), ('ペプシコ', 11), ('キューリグ・ドクター・ペッパー', 12),
    ('アサヒグループホールディングス', 13), ('キリンホールディングス', 14), ('伊藤園', 15),
    ('Coca-Cola Europacific Partners（CCEP）', 16),
    ('ネスレ', 17), ('ダノン', 18), ('ユニリーバ', 19), ('モンデリーズ・インターナショナル', 20)
) as ord(company_name, n)
where public.competitor_companies.company_name = ord.company_name;

comment on column public.competitor_companies.display_order is
    'カテゴリ内の並び順。カテゴリ自体の並び順（ビール→蒸留酒→清涼飲料→日本・総合→ボトラー→FMCG）は'
    'アプリケーション側の固定リストで管理する';


alter table public.competitor_target_records
    add column if not exists themes text[] not null default '{}';
alter table public.competitor_target_records
    add constraint competitor_target_records_themes_check
    check (themes <@ array['水','気候変動・GHG','容器包装','原料調達','生物多様性',
                            '人権','健康','人的資本','責任あるマーケティング']::text[]);

alter table public.competitor_initiatives
    add column if not exists themes text[] not null default '{}';
alter table public.competitor_initiatives
    add constraint competitor_initiatives_themes_check
    check (themes <@ array['水','気候変動・GHG','容器包装','原料調達','生物多様性',
                            '人権','健康','人的資本','責任あるマーケティング']::text[]);

comment on column public.competitor_target_records.themes is
    '記事ダッシュボード(api_server.py THEME_META)と同じ9テーマ体系。複数可。分類・変更判定LLMが付与する';
comment on column public.competitor_initiatives.themes is
    '記事ダッシュボード(api_server.py THEME_META)と同じ9テーマ体系。複数可。分類LLMが付与する';


-- ---- 月次レポート本体を3テーブル構成に作り直す ----

drop table if exists public.monthly_competitor_reports;

create table public.monthly_reports (
    report_id                uuid primary key default gen_random_uuid(),
    report_month               text not null unique,  -- 'YYYY-MM'
    period_start                  date not null,
    period_end                      date not null,

    subject                           text,
    status                              text not null default 'DRAFT'
        check (status in ('DRAFT', 'GENERATED', 'UNDER_REVIEW', 'REVISION_REQUIRED',
                           'APPROVED', 'SENT', 'CANCELLED')),

    summary_json                          jsonb not null default '{}'::jsonb,
    cross_company_trends_json                jsonb not null default '[]'::jsonb,
    html_body                                   text,
    text_body                                     text,

    model_deployment                                text,
    token_usage                                       jsonb,
    latency_ms                                          integer
        check (latency_ms is null or latency_ms >= 0),

    generated_at                                          timestamptz,
    generated_by                                             text,
    reviewed_at                                                timestamptz,
    reviewed_by                                                   text,
    approved_at                                                      timestamptz,
    approved_by                                                         text,

    sent_at                                                               timestamptz,
    send_status                                                             text
        check (send_status is null or send_status in ('success', 'error')),
    send_error_message                                                        text,
    send_mode                                                                   text
        check (send_mode is null or send_mode in ('smtp', 'preview')),
    recipients                                                                    text[],

    created_at                                                                      timestamptz not null default now(),
    updated_at                                                                        timestamptz not null default now()
);

create index monthly_reports_status_idx on public.monthly_reports(status);

create trigger monthly_reports_set_updated_at
    before update on public.monthly_reports
    for each row execute function public.set_updated_at();

alter table public.monthly_reports enable row level security;

comment on table public.monthly_reports is
    '月次競合サステナビリティメール本体。1ヶ月1行(report_month一意)。'
    'status: DRAFT→GENERATED→(UNDER_REVIEW/REVISION_REQUIRED)→APPROVED→SENT の遷移。'
    'weekly_email_reports/competitor_daily_alert_digestsと同じレビューゲートの考え方を踏襲する';


create table public.monthly_report_companies (
    monthly_report_company_id  uuid primary key default gen_random_uuid(),
    monthly_report_id             uuid not null references public.monthly_reports(report_id) on delete cascade,
    company_id                       uuid not null references public.competitor_companies(company_id) on delete cascade,

    display_order                       integer not null default 0,
    include_flag                           boolean not null default true,

    target_change_count                       integer not null default 0,
    esg_rating_update_count                      integer not null default 0,
    actual_update_count                             integer not null default 0,
    initiative_count                                   integer not null default 0,

    company_summary                                       text,
    company_database_url                                     text,

    created_at                                                  timestamptz not null default now(),
    updated_at                                                     timestamptz not null default now(),

    unique(monthly_report_id, company_id)
);

create index monthly_report_companies_report_idx on public.monthly_report_companies(monthly_report_id);

create trigger monthly_report_companies_set_updated_at
    before update on public.monthly_report_companies
    for each row execute function public.set_updated_at();

alter table public.monthly_report_companies enable row level security;

comment on table public.monthly_report_companies is
    '月次レポート1件における企業別セクション。表示順(display_order)・掲載可否(include_flag)を'
    'PMOがレポート単位で調整できる（competitor_companies.display_orderの初期値をコピーして使う）';


create table public.monthly_report_items (
    monthly_report_item_id  uuid primary key default gen_random_uuid(),
    monthly_report_id          uuid not null references public.monthly_reports(report_id) on delete cascade,
    company_id                    uuid not null references public.competitor_companies(company_id) on delete cascade,

    item_type                        text not null
        check (item_type in ('TARGET_CHANGE', 'ACTUAL_UPDATE', 'INITIATIVE', 'INITIATIVE_UPDATE',
                              'PENDING_REVIEW', 'CRAWL_NOTE')),
    change_event_id                     uuid references public.competitor_change_events(change_event_id) on delete set null,
    entity_id                              uuid,  -- INITIATIVE系はcompetitor_initiatives.initiative_idを指す（FK制約は付けない、他種別と型を共有するため）

    display_order                             integer not null default 0,
    include_flag                                 boolean not null default true,

    generated_title                                 text,
    edited_title                                       text,
    generated_summary                                     text,
    edited_summary                                           text,

    source_url                                                  text,
    database_url                                                   text,

    created_at                                                        timestamptz not null default now(),
    updated_at                                                           timestamptz not null default now()
);

create index monthly_report_items_report_idx on public.monthly_report_items(monthly_report_id, company_id);

create trigger monthly_report_items_set_updated_at
    before update on public.monthly_report_items
    for each row execute function public.set_updated_at();

alter table public.monthly_report_items enable row level security;

comment on table public.monthly_report_items is
    '月次レポート1件における掲載項目（目標変更/実績更新/取組事例等）。PMOが項目単位で'
    '掲載可否・表示順・タイトル/要約を編集できる（generated_*はLLM生成値、edited_*はPMO修正値。'
    '表示時はedited優先、無ければgenerated）';
comment on column public.monthly_report_items.entity_id is
    'item_type=INITIATIVE/INITIATIVE_UPDATEの場合はcompetitor_initiatives.initiative_idを保持する'
    '（種別によって参照先テーブルが変わるためFK制約は付けない）';
