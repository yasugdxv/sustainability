-- 記事の「表示用タイトル」と「詳細要約」を保存するカラムを追加し、
-- 翻訳キャッシュのnamespaceを拡張する。
-- 作成日: 2026-10-19
--
-- 背景(ユーザーFB):
--   1. 原題をそのまま出すと意味が分からない記事がある
--      → サステナの文脈を踏まえた表示用タイトル(display_title)をAI分析時に生成する
--   2. 一覧の要約が短く何の記事か分かりにくい／記事詳細が本文の翻訳だけで短い
--      → 一覧用summary_shortを拡充し、詳細用にsummary_detailを新設する
--   3. 英語UIでは日本語で生成したこれらを「日→英」翻訳する必要がある
--      → translation_cacheのnamespaceにdisplay_title/summary_detailを追加する
--
-- すべて冪等（再実行しても安全）。既存データは変更しない（既存行はNULLのまま残り、
-- 別途バックフィルで補完する。NULLの間は従来どおり原題・summary_shortで表示される）。

-- 1) article_analysis に新カラムを追加
alter table public.article_analysis
    add column if not exists display_title   text,
    add column if not exists summary_detail  text;

comment on column public.article_analysis.display_title is
    'サステナ文脈を踏まえたAI生成の表示用タイトル(日本語、30〜45字目安)。'
    'NULLの行は従来どおり原題(articles.title)を翻訳して表示する';
comment on column public.article_analysis.summary_detail is
    '記事詳細画面用のAI詳細要約(日本語、400〜600字目安)。本文に書かれた事実のみから生成する。'
    'NULLの行は詳細画面で本文のみ表示する';

-- 2) translation_cache の namespace 制約を拡張
--    （既存の制約名はPostgresの自動命名 translation_cache_namespace_check）
alter table public.translation_cache
    drop constraint if exists translation_cache_namespace_check;

alter table public.translation_cache
    add constraint translation_cache_namespace_check
    check (namespace in ('title', 'summary', 'body', 'display_title', 'summary_detail'));
