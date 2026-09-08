-- competitor_target_records / competitor_initiatives に goal_categories への参照を追加。
-- 1レコード＝1目標カテゴリ（主目的で判定、多重付与しない）という運用原則のため、
-- 既存のthemes（配列、複数可）とは異なりnullable単一値のFKとする。
--
-- 実行前提: このファイルはユーザーがSupabase側で手動実行する。
-- 事前提: sql/2026-08-02_goal_categories.sql を実行済みであること。

alter table public.competitor_target_records
    add column if not exists goal_category_id text references public.goal_categories(goal_category_id);

alter table public.competitor_initiatives
    add column if not exists goal_category_id text references public.goal_categories(goal_category_id);

comment on column public.competitor_target_records.goal_category_id is
    'サステナ目標カテゴリ（goal_categories）。1レコード＝1カテゴリ、主目的で判定し多重付与しない。'
    '分類LLM(competitor_classifier.py)が付与する。既存themesとは独立した別軸';
comment on column public.competitor_initiatives.goal_category_id is
    'サステナ目標カテゴリ（goal_categories）。1レコード＝1カテゴリ、主目的で判定し多重付与しない。'
    '分類LLM(competitor_classifier.py)が付与する。既存themesとは独立した別軸';

create index competitor_target_records_goal_category_idx
    on public.competitor_target_records(goal_category_id);
create index competitor_initiatives_goal_category_idx
    on public.competitor_initiatives(goal_category_id);
