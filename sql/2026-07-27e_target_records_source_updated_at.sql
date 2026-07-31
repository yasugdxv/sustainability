-- =====================================================================
-- 競合サステナビリティモニタリング: 目標データへの情報源更新日時の追加
-- 作成日: 2026-07-27（2026-07-27〜2026-07-27dの後に実行する）
--
-- 内容:
--   competitor_target_records に source_updated_at を追加する。
--   既存の extracted_at / created_at は「当社がクロールした時刻」であり、
--   競合サイト自身が最終更新した日時とは別物。article_crawler.extract_article()が
--   ページのmetaタグ等から取得する更新日時(updated_at)を、目標データにも
--   保存できるようにする（取得できない場合はNULLのまま）。
-- =====================================================================

alter table public.competitor_target_records
    add column if not exists source_updated_at timestamptz;

comment on column public.competitor_target_records.source_updated_at is
    '競合サイト自身がこのページを最終更新したとみられる日時（ページのmetaタグ等から取得。'
    '取得できない場合はNULL）。extracted_at/created_at（当社がクロールした時刻）とは別物';
