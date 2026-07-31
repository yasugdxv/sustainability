-- =====================================================================
-- 競合サステナビリティモニタリング: 実企業データ投入
-- 作成日: 2026-07-27（2026-07-27_competitor_monitoring_schema.sql の後に実行する）
--
-- 内容:
--   1. competitor_companies に industry_category 列を追加（ビール/蒸留酒/清涼飲料/
--      日本・総合/ボトラー/FMCG/自社の分類。骨組み検証時にはなかった列のためALTER）
--   2. 骨組み検証用のダミー企業2社を削除（紐づくcompetitor_sourcesもON DELETE CASCADEで削除）
--   3. サントリーのindustry_categoryを'自社'に更新
--   4. 競合20社（初期クロールURL付き）を投入
-- =====================================================================

alter table public.competitor_companies
    add column if not exists industry_category text;

comment on column public.competitor_companies.industry_category is
    'ビール/蒸留酒/清涼飲料/日本・総合/ボトラー/FMCG/自社 のいずれか（対象企業一覧のカテゴリ分類）';

delete from public.competitor_companies
    where company_name in ('A飲料（ダミー）', 'Bビバレッジ（ダミー）');

update public.competitor_companies
    set industry_category = '自社'
    where company_name = 'サントリー';

insert into public.competitor_companies (company_name, company_name_en, is_own_company, industry_category) values
    ('ABインベブ',                     'AB InBev',                            false, 'ビール'),
    ('ハイネケン',                      'Heineken',                            false, 'ビール'),
    ('カールスバーグ',                   'Carlsberg Group',                     false, 'ビール'),
    ('モルソン・クアーズ',                'Molson Coors',                        false, 'ビール'),
    ('サッポロホールディングス',           'Sapporo Holdings',                    false, 'ビール'),
    ('ディアジオ',                      'Diageo',                              false, '蒸留酒'),
    ('ペルノ・リカール',                 'Pernod Ricard',                       false, '蒸留酒'),
    ('ブラウン・フォーマン',              'Brown-Forman',                        false, '蒸留酒'),
    ('バカルディ',                      'Bacardi Limited',                     false, '蒸留酒'),
    ('コカ・コーラ・カンパニー',           'The Coca-Cola Company',               false, '清涼飲料'),
    ('ペプシコ',                        'PepsiCo',                             false, '清涼飲料'),
    ('キューリグ・ドクター・ペッパー',      'Keurig Dr Pepper',                    false, '清涼飲料'),
    ('アサヒグループホールディングス',      'Asahi Group Holdings',                false, '日本・総合'),
    ('キリンホールディングス',            'Kirin Holdings',                      false, '日本・総合'),
    ('伊藤園',                          'Ito En',                              false, '日本・総合'),
    ('Coca-Cola Europacific Partners（CCEP）', 'Coca-Cola Europacific Partners', false, 'ボトラー'),
    ('ネスレ',                          'Nestlé',                              false, 'FMCG'),
    ('ダノン',                          'Danone',                              false, 'FMCG'),
    ('ユニリーバ',                       'Unilever',                            false, 'FMCG'),
    ('モンデリーズ・インターナショナル',    'Mondelez International',               false, 'FMCG');

insert into public.competitor_sources (company_id, source_url, source_type, crawl_method, lookback_days, notes)
select company_id, source_url, 'holding', 'HTML', 90, '初期クロールURL（企業一覧より投入、2026-07-27）'
from public.competitor_companies
join (values
    ('ABインベブ',                     'https://ab-inbev.com'),
    ('ハイネケン',                      'https://theheinekencompany.com'),
    ('カールスバーグ',                   'https://carlsberggroup.com'),
    ('モルソン・クアーズ',                'https://molsoncoors.com'),
    ('サッポロホールディングス',           'https://sapporoholdings.jp'),
    ('ディアジオ',                      'https://diageo.com'),
    ('ペルノ・リカール',                 'https://pernod-ricard.com'),
    ('ブラウン・フォーマン',              'https://brown-forman.com'),
    ('バカルディ',                      'https://bacardilimited.com'),
    ('コカ・コーラ・カンパニー',           'https://coca-colacompany.com'),
    ('ペプシコ',                        'https://pepsico.com'),
    ('キューリグ・ドクター・ペッパー',      'https://keurigdrpepper.com'),
    ('アサヒグループホールディングス',      'https://asahigroup-holdings.com'),
    ('キリンホールディングス',            'https://kirinholdings.com'),
    ('伊藤園',                          'https://itoen.co.jp'),
    ('Coca-Cola Europacific Partners（CCEP）', 'https://cocacolaep.com'),
    ('ネスレ',                          'https://nestle.com'),
    ('ダノン',                          'https://danone.com'),
    ('ユニリーバ',                       'https://unilever.com'),
    ('モンデリーズ・インターナショナル',    'https://mondelezinternational.com')
) as seed(company_name, source_url) using (company_name);
