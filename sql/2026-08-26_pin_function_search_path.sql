-- =====================================================================
-- Supabase Security Advisor 指摘対応: Function Search Path Mutable
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   search_pathが固定されていない関数にALTER FUNCTIONでsearch_path=''を設定する。
--   （関連プロジェクトgeopolitical_monitor_dashboardのsql/2026-08-26_pin_function_search_path.sql
--   と同じSecurity Advisor指摘カテゴリへの対応）
--
--   対象関数はいずれも本文中のテーブル参照が public.xxx の形で完全修飾されているか、
--   テーブル参照自体が無いため、search_path=''へ固定しても既存の挙動は変わらない
--   （pg_get_functiondef()で実際の定義を確認済み）。
--   関数の本体（CREATE OR REPLACE FUNCTION）は変更しない。
--
--   article_analysis_before_insert_version は元々このリポジトリのSQLマイグレーション
--   履歴に存在せず、Supabase SQL Editorで直接作成されたまま記録が漏れていたものと
--   見られる（schema drift）。今後もし他に同様の未記録関数が見つかった場合は、
--   都度 pg_get_functiondef() で内容を確認してから追記すること。
-- =====================================================================

alter function public.article_analysis_before_insert_version()
    set search_path = '';

alter function public.set_updated_at()
    set search_path = '';
