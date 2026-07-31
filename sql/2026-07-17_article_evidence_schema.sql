-- =====================================================================
-- 記事分析パイプライン用の追加スキーマ（エビデンス保存 + 出典判定の整合）
-- 作成日: 2026-07-17
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   1. article_evidence（新設） — LLMのWeb検索ツールが返した構造化引用
--      （annotations/url_citation）のみを保存する。本文中の自由記述URLは
--      モデルが実在しないURLを生成することがあるため保存しない。
--   2. article_analysis.primary_source_status のCHECK制約を、
--      source_reliability_rules.state の5値に統一する
--      （現状 article_analysis は0件のため既存データへの影響なし）
-- =====================================================================


-- =====================================================================
-- 1. article_evidence
-- =====================================================================
create table public.article_evidence (
    evidence_id       uuid primary key default gen_random_uuid(),
    article_id        uuid not null
        references public.articles(article_id) on delete cascade,

    evidence_url      text not null,
    title             text,
    search_query      text,
    relevance_note    text,

    created_at        timestamptz not null default now()
);

create index article_evidence_article_id_idx on public.article_evidence(article_id);

alter table public.article_evidence enable row level security;


-- =====================================================================
-- 2. article_analysis.primary_source_status を source_reliability_rules と揃える
-- =====================================================================
do $$
declare
    con record;
begin
    for con in
        select conname
        from pg_constraint
        where conrelid = 'public.article_analysis'::regclass
          and contype = 'c'
          and pg_get_constraintdef(oid) ilike '%primary_source_status%'
    loop
        execute format('alter table public.article_analysis drop constraint %I', con.conname);
    end loop;
end $$;

alter table public.article_analysis
    add constraint article_analysis_primary_source_status_check
        check (primary_source_status is null or primary_source_status in
            ('一次情報', '一次照合済み', '解釈・分析', '一次未確認', '出所不明'));

comment on column public.article_analysis.primary_source_status is
    'source_reliability_rules.state を参照。Web検索で確認した根拠は article_evidence に保存する';
