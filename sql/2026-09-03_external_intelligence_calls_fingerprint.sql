-- Sustainability Department Intelligence AI Phase B: Exact Response Cache用フィンガープリント列。
-- 実際にGeoへ送った質問文（request_type + question + country_region + themes）から計算した
-- ハッシュを保存する。計算はアプリ側（sustainability_chat_geo_service.compute_geo_request_fingerprint）
-- で行い、このテーブルには結果のみを保存する（DB側では計算しない）。
-- weekly呼び出し（weekly_article/weekly_monitoring）はこの列を使わないため常にNULLのまま。
alter table public.external_intelligence_calls
    add column if not exists request_fingerprint_hash text;

create index if not exists external_intelligence_calls_fingerprint_idx
    on public.external_intelligence_calls (request_fingerprint_hash, requested_at desc)
    where request_fingerprint_hash is not null;
