-- 2026-08-27: 軽量キーワードフィルタ（article_filter.py）による除外件数の集計ログ。
-- 記事本体・タイトル等は一切保存しない（除外件数と出典区分のみ）。四半期レビューで
-- 除外率の急変（語彙の劣化やソース構成変化の兆候）を確認する用途。
-- 集計粒度は実行(run)単位・出典区分(publisher_tag_id)単位。週次集計はクエリ側で
-- run_dateをまとめて行う（バッチ実行タイミングに依存しないようにするため）。

create table public.filter_exclusion_log (
    id uuid primary key default gen_random_uuid(),
    run_date date not null default current_date,
    publisher_tag_id text,          -- tag_reference: メディア・データ提供機関(SJ-11)配下のtag_id
    excluded_count integer not null,
    total_checked_count integer not null,  -- その出典区分でフィルタ判定対象になった総数（除外率の分母）
    created_at timestamptz not null default now()
);

create index filter_exclusion_log_run_date_idx on public.filter_exclusion_log(run_date);

alter table public.filter_exclusion_log enable row level security;

comment on table public.filter_exclusion_log is
    '軽量キーワードフィルタの除外件数集計ログ（記事本体は保存しない、件数のみ）';
