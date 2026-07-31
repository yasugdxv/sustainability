-- =====================================================================
-- 競合サステナビリティモニタリング: URL種別の再設計＋候補URL自動発見
-- 作成日: 2026-07-27（2026-07-27_competitor_monitoring_schema.sql /
--         2026-07-27b_competitor_companies_real_data.sql の後に実行する）
--
-- 内容:
--   1. 骨組み検証用に投入していたcompetitor_crawl_logs/competitor_sources
--      （トップページURL、source_type='holding'）を全削除
--   2. competitor_sources.source_type のCHECK制約を、企業構造ベースの分類
--      （holding/business_unit/...）から、サステナ情報の種別ベースの分類
--      （SUSTAINABILITY_HOME/TARGETS/PROGRESS/REPORTS/NEWS/DISCLOSURE）へ貼り替え
--      （旧分類はまだどのコードからも参照されておらず置き換えても影響なし）
--   3. 競合20社の実際のサステナビリティ公式ページをSUSTAINABILITY_HOMEとして再投入
--      （CCEP/ネスレ/ユニリーバは検証時点でこの環境からBot対策により403を確認済みだが、
--      URL自体は正しいため登録する。crawl_logsに「失敗」として記録され続ける想定）
--   4. 新規テーブル competitor_source_candidates
--      （SUSTAINABILITY_HOME配下から自動発見した候補URLを、人がレビューして
--      有効化するまで本番監視対象にしないための一時保存テーブル）
-- =====================================================================

delete from public.competitor_crawl_logs;
delete from public.competitor_sources;

alter table public.competitor_sources
    drop constraint if exists competitor_sources_source_type_check;
alter table public.competitor_sources
    add constraint competitor_sources_source_type_check
    check (source_type in (
        'SUSTAINABILITY_HOME', 'TARGETS', 'PROGRESS', 'REPORTS', 'NEWS', 'DISCLOSURE'
    ));

insert into public.competitor_sources (company_id, source_url, source_type, crawl_method, lookback_days, notes)
select company_id, source_url, 'SUSTAINABILITY_HOME', 'HTML', 90,
       'サステナビリティ公式ページ（2026-07-27投入。CCEP/ネスレ/ユニリーバは検証環境からBot対策で403を確認済み）'
from public.competitor_companies
join (values
    ('ABインベブ',                     'https://www.ab-inbev.com/sustainability'),
    ('ハイネケン',                      'https://www.theheinekencompany.com/newsroom/brew-a-better-world--together-we-can/'),
    ('カールスバーグ',                   'https://www.carlsberggroup.com/sustainability/our-esg-programme/'),
    ('モルソン・クアーズ',                'https://www.molsoncoors.com/sustainability/sustainability-overview'),
    ('サッポロホールディングス',           'https://www.sapporobreweries.com/sustainability/'),
    ('ディアジオ',                      'https://www.diageo.com/en/esg/sustainability'),
    ('ペルノ・リカール',                 'https://www.pernod-ricard.com/en/sustainability-responsibility'),
    ('ブラウン・フォーマン',              'https://www.brown-forman.com/environmental-sustainability'),
    ('バカルディ',                      'https://www.bacardilimited.com/cs/'),
    ('コカ・コーラ・カンパニー',           'https://www.coca-colacompany.com/policies-and-practices/sustainability'),
    ('ペプシコ',                        'https://www.pepsico.com/sustainability'),
    ('キューリグ・ドクター・ペッパー',      'https://www.keurigdrpepper.com/our-impact/'),
    ('アサヒグループホールディングス',      'https://www.asahigroup-holdings.com/en/sustainability/'),
    ('キリンホールディングス',            'https://www.kirinholdings.com/en/sustainability/'),
    ('伊藤園',                          'https://www.itoen.co.jp/sustainability/'),
    ('Coca-Cola Europacific Partners（CCEP）', 'https://www.cocacolaep.com/sustainability/'),
    ('ネスレ',                          'https://www.nestle.com/sustainability'),
    ('ダノン',                          'https://www.danone.com/sustainability/our-approach/danone-impact-journey.html'),
    ('ユニリーバ',                       'https://www.unilever.com/sustainability/'),
    ('モンデリーズ・インターナショナル',    'https://www.mondelezinternational.com/snacking-made-right/index.html')
) as seed(company_name, source_url) using (company_name);


-- ---- competitor_source_candidates（候補URL管理） ----

create table public.competitor_source_candidates (
    candidate_id               uuid primary key default gen_random_uuid(),
    company_id                  uuid not null references public.competitor_companies(company_id) on delete cascade,
    discovered_from_source_id    uuid references public.competitor_sources(source_id) on delete set null,

    url                           text not null,
    page_title                    text,
    url_type_candidate              text not null default 'OTHER'
        check (url_type_candidate in (
            'SUSTAINABILITY_HOME', 'TARGETS', 'PROGRESS', 'REPORTS', 'NEWS', 'DISCLOSURE', 'OTHER'
        )),

    last_checked_at                 timestamptz not null default now(),
    is_crawlable                     boolean not null default true,
    is_active                         boolean not null default false,
    promoted_source_id                 uuid references public.competitor_sources(source_id) on delete set null,

    created_at                          timestamptz not null default now(),
    updated_at                            timestamptz not null default now()
);

create unique index competitor_source_candidates_company_url_idx
    on public.competitor_source_candidates(company_id, url);
create index competitor_source_candidates_active_idx
    on public.competitor_source_candidates(is_active);

create trigger competitor_source_candidates_set_updated_at
    before update on public.competitor_source_candidates
    for each row execute function public.set_updated_at();

alter table public.competitor_source_candidates enable row level security;

comment on table public.competitor_source_candidates is
    'SUSTAINABILITY_HOMEページ配下から自動発見した候補URL。is_active=falseの間は本番監視対象にせず、'
    'sustainability_expert_dashboard.py のレビュー画面で人が種別確認・有効化して初めて'
    'competitor_sources に昇格する（promoted_source_idで対応行を参照）';
comment on column public.competitor_source_candidates.url_type_candidate is
    'competitor_source_discovery.guess_url_type()によるキーワードベースの推測値。'
    '人がレビュー画面で修正してから有効化できる';
