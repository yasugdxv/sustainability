-- Weekly x Geo Intelligence Batch Inquiry Phase S2
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   external_intelligence_calls.request_type のCHECK制約に 'weekly_monitoring' を追加する。
--   Phase S1時点では 'weekly_article' / 'user_question' のみ許可していたが、
--   週次レポート生成時にGeo Intelligence側へ期間・テーマ・優先地域をまとめて問い合わせる
--   'weekly_monitoring' request_typeをPhase S2で新設したため、既存行を壊さない形で
--   制約を貼り直す。

alter table public.external_intelligence_calls
    drop constraint if exists external_intelligence_calls_request_type_check;

alter table public.external_intelligence_calls
    add constraint external_intelligence_calls_request_type_check
    check (request_type in ('weekly_article', 'user_question', 'weekly_monitoring'));
