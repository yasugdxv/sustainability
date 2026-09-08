-- Supabase Security Advisor 指摘対応
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   reference_list_entries / reference_list_change_events
--   （2026-08-21_reference_list_monitor_schema.sql で新設）に
--   enable row level security が抜けていたため追加する。
--   他のテーブル（crawl_targets, crawl_logs 等）は新設時に必ず
--   付けている定型だが、このファイルだけ抜けていたのが原因。
--   ポリシーは追加しない（service_role が全アクセスする既存運用のまま）。

alter table public.reference_list_entries enable row level security;
alter table public.reference_list_change_events enable row level security;
