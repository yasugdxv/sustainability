-- サスティナビリティ記事ダッシュボード（React版）の「いいね」「読んだ」件数を
-- 保存するためのテーブルと、カウントを安全に増減させるための関数。
--
-- 適用方法: Supabaseダッシュボード > SQL Editor で本ファイルの内容を実行してください。
-- （既にテーブルまで作成済みの場合も、関数定義だけ再実行すれば問題ありません）

create table if not exists public.article_engagement (
    article_id   uuid primary key references public.articles(article_id) on delete cascade,
    likes_count  integer not null default 0,
    reads_count  integer not null default 0,
    updated_at   timestamptz not null default now()
);

comment on table public.article_engagement is
    'React版ダッシュボードの「いいね」「読んだ」件数（ブラウザ単位のローカル状態と組み合わせて使用）';

-- 戻り値の列名(article_id等)がテーブルの列名と同名だと、PL/pgSQL内で
-- 「column reference "article_id" is ambiguous」になることがあるため、
-- returns table(...) は使わず、テーブルの行型をそのまま返す形にする。
drop function if exists public.increment_article_engagement(uuid, integer, integer);

-- likes_count / reads_count を原子的に増減させる関数。
-- 行が無ければ作成し、あれば更新する（upsert）。
create function public.increment_article_engagement(
    p_article_id uuid,
    p_likes_delta integer default 0,
    p_reads_delta integer default 0
) returns setof public.article_engagement
language plpgsql
as $$
begin
    return query
    insert into public.article_engagement as ae (article_id, likes_count, reads_count)
    values (p_article_id, greatest(p_likes_delta, 0), greatest(p_reads_delta, 0))
    on conflict (article_id) do update
        set likes_count = greatest(ae.likes_count + p_likes_delta, 0),
            reads_count = greatest(ae.reads_count + p_reads_delta, 0),
            updated_at = now()
    returning ae.*;
end;
$$;
