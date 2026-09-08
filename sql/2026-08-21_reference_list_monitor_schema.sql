-- 参照リスト（制裁リスト・規則集タイトル等）の差分検知基盤
-- 対象: UN SC Consolidated List, OFAC SDN, eCFR titles（crawl_method='API'のうち、
-- 通常の記事クロールとは相性が悪い「エンティティ一覧のスナップショット」系ソース）
-- 通常記事(articles)とは別に、エンティティ単位で追加/削除/変更を検知して記録する。

-- 1. 各エンティティの最新スナップショット
create table public.reference_list_entries (
    entry_id       uuid primary key default gen_random_uuid(),
    list_source    text not null check (list_source in ('UN_SC', 'OFAC_SDN', 'ECFR_TITLES')),
    entry_key      text not null,        -- UN_SC: DATAID / OFAC_SDN: uid / ECFR_TITLES: title番号
    entry_data     jsonb not null,       -- 名前・プログラム・改正日等の主要フィールド
    content_hash   text not null,        -- 変更検知用（entry_dataのハッシュ）
    first_seen_at  timestamptz not null default now(),
    last_seen_at   timestamptz not null default now(),
    is_current     boolean not null default true,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- 同一ソース×キーの現行版は1件のみ
create unique index reference_list_entries_current_key_idx
    on public.reference_list_entries(list_source, entry_key)
    where is_current;

create index reference_list_entries_source_idx
    on public.reference_list_entries(list_source, is_current);

create trigger reference_list_entries_set_updated_at
    before update on public.reference_list_entries
    for each row execute function public.set_updated_at();

-- 2. 検知した差分イベント
create table public.reference_list_change_events (
    change_event_id uuid primary key default gen_random_uuid(),
    list_source     text not null check (list_source in ('UN_SC', 'OFAC_SDN', 'ECFR_TITLES')),
    entry_key       text not null,
    change_type     text not null check (change_type in ('added', 'removed', 'updated')),
    before_data     jsonb,
    after_data      jsonb,
    detected_at     timestamptz not null default now(),
    notified        boolean not null default false  -- 将来、週次配信等に使う際の既読管理用
);

create index reference_list_change_events_source_idx
    on public.reference_list_change_events(list_source, detected_at desc);
