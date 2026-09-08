-- Supabase Security Advisor 指摘対応
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   vector 拡張機能が public スキーマに入っているという警告への対応。
--   Supabase推奨の extensions スキーマへ移動する。
--   本リポジトリのSQL管理下には vector型のカラム・インデックスへの
--   言及が無く（ベクター検索は現状Azure AI Search側で行っている構成）、
--   依存オブジェクトは無いと見られるため、単純な移動のみで対応する。
--   Supabaseのデフォルトsearch_pathには extensions が含まれるため、
--   移動後も既存のクエリへの影響は無い想定。

create schema if not exists extensions;
alter extension vector set schema extensions;
