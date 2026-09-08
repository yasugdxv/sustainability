-- 記事タイトル・要約・本文のLLM翻訳結果を永続化するキャッシュ。
-- 作成日: 2026-09-02
--
-- 背景: これまでsustainability_dashboard_core.py内のPythonの辞書（プロセスメモリ上）
-- だけにキャッシュしており、api_server.py再起動のたびに全消去されていた。
-- 再起動のたびに数千件規模の記事タイトル・要約が英語のまま表示され、
-- LLM呼び出しによる再翻訳が完了するまで（レート制限次第で数十分〜）解消しない
-- 状態になっていた。このテーブルに書き込むことで再起動をまたいで再利用する。
--
-- 本ファイルの全テーブルはRLSを有効化するがpolicyは定義しない。
-- service-role鍵経由のバックエンド処理のみが読み書きする前提（既存の他テーブルと同じ設計）。

create table public.translation_cache (
    namespace     text not null check (namespace in ('title', 'summary', 'body')),
    target_lang   text not null,
    article_id    uuid not null references public.articles(article_id) on delete cascade,
    text          text not null,
    updated_at    timestamptz not null default now(),

    primary key (namespace, target_lang, article_id)
);

alter table public.translation_cache enable row level security;

comment on table public.translation_cache is
    '記事タイトル・要約・本文の翻訳結果キャッシュ。api_server.py起動時に全件読み込んで'
    'プロセスメモリ上のキャッシュ(_short_cache/_body_cache)を温め、以後は'
    '書き込み時にこのテーブルにも反映する（読み取りは常にメモリ上の辞書経由、'
    'このテーブルへのSELECTは起動時の1回だけ）。原文と翻訳先言語が同じ場合'
    '（原文が既に日本語で target_lang=ja 等）もtextに原文をそのまま保存し、'
    '無駄なLLM再呼び出しを避ける';
