-- =====================================================================
-- Supabase Security Advisor 指摘対応: Function Search Path Mutable（追加分2）
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   2026-08-26_pin_function_search_path.sql / 2026-08-26b_... 実行後に
--   新たに見つかった対象3件。いずれも本文中のテーブル参照が public.xxx の形で
--   完全修飾されているため、search_path=''へ固定しても既存の挙動は変わらない
--   （pg_get_functiondef()で実際の定義を確認済み）。
-- =====================================================================

alter function public.enforce_article_urls_publisher_tag_axis()
    set search_path = '';

alter function public.enforce_crawl_targets_publisher_tag_axis()
    set search_path = '';

alter function public.enforce_tag_reference_parent_axis()
    set search_path = '';
