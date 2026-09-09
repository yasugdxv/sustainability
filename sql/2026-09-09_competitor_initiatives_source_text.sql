-- competitor_initiativesに検出元記事の本文全体を保存するカラムを追加。
-- 作成日: 2026-09-09
--
-- 背景: 「取組事例」画面では簡易要約(summary)と原典リンクしか見られず、
-- 記事詳細ページのような本文表示ができないという指摘への対応。
-- このカラム追加以前に登録された行はNULLのまま残るため、
-- 別途バックフィルスクリプトで既存分の本文を補完する。

alter table public.competitor_initiatives add column source_text text;

comment on column public.competitor_initiatives.source_text is
    '検出元記事の本文全体（詳細ページでの本文表示用）。2026-09-09のカラム追加'
    '以前に登録された行はNULL（別途バックフィルで補完）';
