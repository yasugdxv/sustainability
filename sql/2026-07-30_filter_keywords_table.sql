-- フィルタールール キーワードのテーブル化（PMOフィードバック(6)対応）
-- これまでarticle_filter.py内にPython定数として直書きしていた軽量フィルタの
-- キーワードを、PMO・DX本部が四半期レビュー（ソース棚卸しと同サイクル）で
-- 共同保守できるよう別テーブルに切り出す。
--
-- tag_referenceは意図的に流用しない（フィルタールール_たたき台_20260720.md 3.1節の通り、
-- tag_referenceはLLMタグ付け用でフィルタ用キーワードとは粒度・目的が異なるため）。
--
-- 実行前提: このファイルはユーザーがSupabase側で手動実行する。

create table public.filter_keywords (
    keyword_id      uuid primary key default gen_random_uuid(),
    keyword_text    text not null,
    keyword_group   text not null,
    tier            text not null
        check (tier in ('厳密語', '一般語')),
    language        text not null
        check (language in ('ja', 'en')),
    status          text not null default '有効'
        check (status in ('有効', '無効')),
    notes           text,

    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),

    unique (keyword_text, keyword_group, tier)
);

create index filter_keywords_status_idx on public.filter_keywords(status);
create index filter_keywords_group_idx on public.filter_keywords(keyword_group);

create trigger filter_keywords_set_updated_at
    before update on public.filter_keywords
    for each row execute function public.set_updated_at();

alter table public.filter_keywords enable row level security;
