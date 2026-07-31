-- =====================================================================
-- [旧版・非推奨] このファイルはさらに sql/build/01_create_schema.sql
-- （+ 02_tag_reference_seed.sql、crawl_management_table_data/配下のimport）
-- に置き換えられた。特に本ファイルの publisher_major/publisher_minor 列は、
-- 実際に稼働中のコード（article_crawler.py, article_filter.py）や実データが
-- 前提とする publisher_tag_id 列と非互換のため、このファイルで新規構築すると
-- 現行コードが動かない。新規実行には sql/build/ 配下を使用すること。
-- このファイルは変更履歴として残置している。
-- =====================================================================
-- サステナビリティ・インテリジェンス基盤 クロール管理テーブル v2
-- 作成日: 2026-07-15
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- v1（2026-07-15_crawl_and_tag_schema.sql）からの変更点:
--   * crawl_targets からタグ列（theme_major等11列）を削除し、
--     crawl_target_tags 経由で tag_reference と多対多に紐付ける方式へ変更
--   * tag_reference に tag_level / tag_code / parent_tag_id / display_order /
--     status を追加し、大分類・小分類をそれぞれ独立した行として管理
--   * crawl_target_tags（新設）
--   * updated_at 自動更新トリガー
--   * crawl_method・lookback_days等へのCHECK制約追加
--   * crawl_logsへのデフォルト値・CHECK制約・インデックス追加
--   * 全テーブルでRLSを有効化
--
-- このファイルは2つの実行パスを含む。どちらか一方のみを実行すること。
--   [A] 新規構築（v1を一度も実行していない場合）
--   [B] 移行（v1のcreate table文を既にSupabaseで実行済みの場合。
--       crawl_targets/crawl_logsの既存行は保持したままスキーマのみ変更する）
-- =====================================================================


-- =====================================================================
-- 共通: updated_at 自動更新トリガー関数
-- =====================================================================
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;


-- #######################################################################
-- [A] 新規構築（v1を実行していない場合はこちらのみ実行し、[B]は実行しない）
-- #######################################################################

-- ---- A-1. crawl_targets（タグ列なし版） ----
create table public.crawl_targets (
    crawl_target_id          text primary key,
    target_name              text not null,
    target_url               text not null unique,
    domain                   text not null,
    publisher_name           text not null,
    publisher_major          text not null,
    publisher_minor          text,

    endpoint_type              text not null,
    crawl_method                text not null
        check (crawl_method in ('HTML', 'RSS', 'API', 'ブラウザ操作', 'メール', '手動')),
    change_detection_method    text not null,
    crawl_detail_pages         boolean not null,
    include_paths              text,
    exclude_paths              text,
    search_conditions          text,
    keyword_filters            text,
    javascript_required        boolean,
    authentication_required    boolean,
    pagination_method          text,
    lookback_days              integer
        check (lookback_days is null or lookback_days >= 0),
    fallback_url               text,
    crawl_notes                text,

    created_at                timestamptz not null default now(),
    updated_at                timestamptz not null default now(),
    notes                     text
);

create trigger crawl_targets_set_updated_at
    before update on public.crawl_targets
    for each row execute function public.set_updated_at();

-- ---- A-2. crawl_logs ----
create table public.crawl_logs (
    crawl_log_id      text primary key,
    crawl_target_id   text not null
        references public.crawl_targets(crawl_target_id) on delete restrict,
    started_at        timestamptz not null,
    finished_at       timestamptz
        constraint crawl_logs_finished_after_started
        check (finished_at is null or finished_at >= started_at),
    run_result        text not null
        check (run_result in ('成功', '一部成功', '失敗', '更新なし')),
    process_stage     text,
    http_status       integer
        check (http_status is null or http_status between 100 and 599),
    final_url         text,
    items_detected    integer not null default 0 check (items_detected >= 0),
    new_items         integer not null default 0 check (new_items >= 0),
    updated_items     integer not null default 0 check (updated_items >= 0),
    error_category    text,
    error_message     text,
    retry_count       integer not null default 0 check (retry_count >= 0),
    crawler_version   text,
    notes             text
);

create index crawl_logs_target_started_idx
    on public.crawl_logs(crawl_target_id, started_at desc);

create index crawl_logs_result_started_idx
    on public.crawl_logs(run_result, started_at desc);

-- ---- A-3. tag_reference ----
create table public.tag_reference (
    tag_id          text primary key,
    tag_axis        text not null
        check (tag_axis in ('テーマ', '横断', '主体', '地域', '記事種別', 'マテリアリティ接続', '情報属性')),
    tag_level       text not null
        check (tag_level in ('大分類', '小分類')),
    tag_code        text not null,
    tag_name        text not null,
    parent_tag_id   text references public.tag_reference(tag_id) on delete restrict,
    tag_meaning     text not null,
    tag_criteria    text not null,
    display_order   integer,
    status          text not null default '有効'
        check (status in ('有効', '無効')),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    notes           text,
    unique (tag_axis, tag_code),
    unique (tag_axis, tag_name, parent_tag_id)
);

create trigger tag_reference_set_updated_at
    before update on public.tag_reference
    for each row execute function public.set_updated_at();

-- ---- A-4. crawl_target_tags（中間テーブル） ----
create table public.crawl_target_tags (
    crawl_target_id text not null
        references public.crawl_targets(crawl_target_id) on delete cascade,
    tag_id text not null
        references public.tag_reference(tag_id) on delete restrict,
    created_at timestamptz not null default now(),
    primary key (crawl_target_id, tag_id)
);

create index crawl_target_tags_tag_id_idx
    on public.crawl_target_tags(tag_id);

-- ---- A-5. RLS 有効化（ポリシーは付与しない＝service_role以外は既定で読み書き不可） ----
alter table public.crawl_targets enable row level security;
alter table public.crawl_logs enable row level security;
alter table public.tag_reference enable row level security;
alter table public.crawl_target_tags enable row level security;
-- 閲覧を許可したいロールが出てきたら、対象ロールにSELECTポリシーを個別追加する。
-- 例: create policy "anon can read tag_reference" on public.tag_reference for select to anon using (true);


-- #######################################################################
-- [B] 移行（v1のcreate table文を既にSupabaseで実行済みの場合はこちらのみ実行）
--     crawl_targets / crawl_logs の既存行は削除せずスキーマのみ変更する。
--     tag_reference は列構成そのものを再設計するため、DROPして作り直す
--     （tag_referenceにはv1のシードデータしか入っておらず、実データではないため）。
-- #######################################################################

-- ---- B-1. crawl_targets: タグ列の削除、target_urlのunique化、CHECK追加 ----
alter table public.crawl_targets
    drop column if exists theme_major,
    drop column if exists theme_minor,
    drop column if exists cross_tags,
    drop column if exists subject_major,
    drop column if exists subject_minor,
    drop column if exists region_major,
    drop column if exists region_minor,
    drop column if exists article_type_major,
    drop column if exists article_type_minor,
    drop column if exists materiality_tags,
    drop column if exists information_attribute;

-- target_url の重複が既にある場合はこのALTERが失敗する。
-- 失敗した場合は "select target_url, count(*) from crawl_targets group by target_url having count(*) > 1;"
-- で重複を特定し、先に解消してから再実行すること。
alter table public.crawl_targets
    add constraint crawl_targets_target_url_key unique (target_url);

alter table public.crawl_targets
    add constraint crawl_targets_crawl_method_check
        check (crawl_method in ('HTML', 'RSS', 'API', 'ブラウザ操作', 'メール', '手動'));

alter table public.crawl_targets
    add constraint crawl_targets_lookback_days_check
        check (lookback_days is null or lookback_days >= 0);

drop trigger if exists crawl_targets_set_updated_at on public.crawl_targets;
create trigger crawl_targets_set_updated_at
    before update on public.crawl_targets
    for each row execute function public.set_updated_at();

-- ---- B-2. crawl_logs: デフォルト値・CHECK・インデックス追加 ----
alter table public.crawl_logs
    alter column items_detected set default 0,
    alter column new_items set default 0,
    alter column updated_items set default 0,
    alter column retry_count set default 0;

alter table public.crawl_logs
    add constraint crawl_logs_items_detected_check check (items_detected >= 0),
    add constraint crawl_logs_new_items_check check (new_items >= 0),
    add constraint crawl_logs_updated_items_check check (updated_items >= 0),
    add constraint crawl_logs_retry_count_check check (retry_count >= 0),
    add constraint crawl_logs_finished_after_started
        check (finished_at is null or finished_at >= started_at),
    add constraint crawl_logs_http_status_check
        check (http_status is null or http_status between 100 and 599);

-- crawl_target_id の外部キーを on delete restrict に張り替え
alter table public.crawl_logs drop constraint if exists crawl_logs_crawl_target_id_fkey;
alter table public.crawl_logs
    add constraint crawl_logs_crawl_target_id_fkey
        foreign key (crawl_target_id) references public.crawl_targets(crawl_target_id)
        on delete restrict;

create index if not exists crawl_logs_target_started_idx
    on public.crawl_logs(crawl_target_id, started_at desc);
create index if not exists crawl_logs_result_started_idx
    on public.crawl_logs(run_result, started_at desc);

-- ---- B-3. tag_reference: 作り直し（v1のシードデータのみのため） ----
drop table if exists public.tag_reference cascade;
create table public.tag_reference (
    tag_id          text primary key,
    tag_axis        text not null
        check (tag_axis in ('テーマ', '横断', '主体', '地域', '記事種別', 'マテリアリティ接続', '情報属性')),
    tag_level       text not null
        check (tag_level in ('大分類', '小分類')),
    tag_code        text not null,
    tag_name        text not null,
    parent_tag_id   text references public.tag_reference(tag_id) on delete restrict,
    tag_meaning     text not null,
    tag_criteria    text not null,
    display_order   integer,
    status          text not null default '有効'
        check (status in ('有効', '無効')),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    notes           text,
    unique (tag_axis, tag_code),
    unique (tag_axis, tag_name, parent_tag_id)
);

create trigger tag_reference_set_updated_at
    before update on public.tag_reference
    for each row execute function public.set_updated_at();

-- ---- B-4. crawl_target_tags（新設） ----
create table if not exists public.crawl_target_tags (
    crawl_target_id text not null
        references public.crawl_targets(crawl_target_id) on delete cascade,
    tag_id text not null
        references public.tag_reference(tag_id) on delete restrict,
    created_at timestamptz not null default now(),
    primary key (crawl_target_id, tag_id)
);

create index if not exists crawl_target_tags_tag_id_idx
    on public.crawl_target_tags(tag_id);

-- ---- B-5. RLS 有効化 ----
alter table public.crawl_targets enable row level security;
alter table public.crawl_logs enable row level security;
alter table public.tag_reference enable row level security;
alter table public.crawl_target_tags enable row level security;


-- =====================================================================
-- 次の手順（[A]または[B]の実行後、共通）:
--   1. 2026-07-15_tag_reference_seed_v2.sql を実行（tag_reference本体へ466件投入）
--   2. 2026-07-15_crawl_targets_import.sql を実行（crawl_targets本体へ61件投入）
--   3. 2026-07-15_crawl_target_tags_import.sql を実行（crawl_target_tags本体へ122件投入）
-- =====================================================================
