-- =====================================================================
-- Supabase Security Advisor 指摘対応: Function Search Path Mutable（追加分）
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   2026-08-26_pin_function_search_path.sql 実行後に新たに見つかった対象。
--   articles_before_insert_version は本文中のテーブル参照が public.xxx の形で
--   完全修飾されているため、search_path=''へ固定しても既存の挙動は変わらない
--   （pg_get_functiondef()で実際の定義を確認済み）。
-- =====================================================================

alter function public.articles_before_insert_version()
    set search_path = '';
