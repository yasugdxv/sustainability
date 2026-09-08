-- 地理ティアの機械可読化（PMO 2026-08-18フィードバック 1章）
-- tag_reference（地域軸 RG-xx）にティア属性列を追加し、PMO指定の割当表を反映する。
-- 判定エンジン側（評価項目1・補正ルール2件・下位5軸監視閾値(i)）が、この列を参照して
-- 「地理＝主要・準主要地域に該当するか」を機械的に判定できるようにする。

-- 1. カラム追加
alter table public.tag_reference
    add column if not exists geo_tier text
    check (geo_tier is null or geo_tier in ('最上位', '主要', '準主要', 'その他'));

comment on column public.tag_reference.geo_tier is
    'PMO指定の地理ティア（RG軸のみ使用）。最上位=グローバル・EU／主要=主要5か国／準主要=準主要6か国／その他=それ以外';

-- 2. PMO指定の割当表を反映
update public.tag_reference set geo_tier = '最上位'
    where tag_id in ('RG-01', 'RG-02-01');

update public.tag_reference set geo_tier = '主要'
    where tag_id in ('RG-04-01', 'RG-04-02', 'RG-04-03', 'RG-04-04', 'RG-04-05');

update public.tag_reference set geo_tier = '準主要'
    where tag_id in ('RG-04-06', 'RG-04-07', 'RG-04-08', 'RG-04-09', 'RG-04-10', 'RG-04-11');

update public.tag_reference set geo_tier = 'その他'
    where tag_axis = '地域' and geo_tier is null;

-- 3. 反映結果の確認用クエリ（実行後、件数が想定通りか目視確認する）
-- select geo_tier, count(*) from public.tag_reference where tag_axis = '地域' group by geo_tier order by geo_tier;
-- 想定: 最上位=2件、主要=5件、準主要=6件、その他=52件（地域タグ合計65件）
