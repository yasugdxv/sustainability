-- 外部Intelligence Service連携基盤 Phase S1
-- 作成日: 2026-08-26
-- 対象: Supabase（Supabase SQL Editorで実行）
--
-- 内容:
--   外部Intelligence Service（現状はGeo Intelligence／geopolitical_monitor_dashboard
--   アプリのAPIのみ）への問い合わせ監査ログテーブルを新設する。
--
--   crawl_logs/expert_runs等の既存監査ログテーブルは全て「自社パイプラインの1実行」を
--   記録するドメイン専用テーブル（1ドメイン1テーブル）だが、本テーブルは
--   「外部の別アプリへのアウトバウンドAPI呼び出し」というインフラ横断の関心事のため、
--   意図的に汎用名で新設する（将来SCM/ERM等の別サービスが増えてもtarget_serviceを
--   増やすだけで対応できるようにするため）。
--
--   status（本テーブルのstatus列）は「Sustainability側からみた通信状態」
--   （success/unavailable/failed/invalid_response/disabled）であり、
--   Geo Intelligence自身が返す業務ステータス（completed/partial/failed。
--   geopolitical_monitor_dashboard側の実装確認により、Geo側のパイプラインが
--   業務的に失敗した場合でも200+構造化エラーで返ることを確認済み）とは
--   意図的に分離し、remote_status列に別途保持する。
--   同様にconfidence（Geo実装確認: "high"/"medium"/"low"の文字列。0〜1の数値ではない）
--   もGeo側の実仕様に合わせてtext型とする。
--
--   remote_status/confidenceにはCHECK制約を設けない。Geo側が将来値を追加・変更しても
--   監査ログの書き込み自体は失敗させたくないため（statusとknowledge_sufficiencyは
--   Sustainability側で完全に制御する値、またはGeo側の型がEnumとして固定されている
--   値のためCHECK制約を維持する）。

create table public.external_intelligence_calls (
    id                    uuid primary key default gen_random_uuid(),

    target_service        text not null,          -- 例: 'geo_intelligence'（CHECK制約なし。将来別サービス追加時も列変更不要）
    request_type          text not null
        check (request_type in ('weekly_article', 'user_question')),

    source_type           text,                     -- 例: 'article'。Phase S1では常にNULL
    source_id             text,                     -- 例: articles.article_id を文字列化。FK制約なし

    local_request_id      uuid not null,            -- Sustainability側で生成しGeoへ送ったrequest_id
    remote_request_id     text,                     -- Geoレスポンスがエコーバックしたrequest_id（不一致は本文中でinvalid_response扱いになるため、ここに記録される時点で一致している）

    request_payload       jsonb,
    response_payload      jsonb,                    -- 失敗時(unavailable/timeout等)はNULLになり得る

    status                text not null
        check (status in ('success', 'unavailable', 'failed', 'invalid_response', 'disabled')),
    remote_status         text,                     -- Geo自身の業務ステータス（completed/partial/failed）。CHECK制約なし（上記コメント参照）
    knowledge_sufficiency text
        check (knowledge_sufficiency is null or knowledge_sufficiency in ('sufficient', 'partial', 'insufficient')),
    confidence            text,                     -- "high"/"medium"/"low" 等。CHECK制約なし（上記コメント参照）

    requested_at          timestamptz not null default now(),
    completed_at          timestamptz
        check (completed_at is null or completed_at >= requested_at),
    latency_ms            integer
        check (latency_ms is null or latency_ms >= 0),

    http_status_code      integer
        check (http_status_code is null or http_status_code between 100 and 599),
    error_type            text
        check (error_type is null or error_type in
            ('timeout', 'connection_error', 'http_error', 'schema_validation', 'not_configured', 'unknown')),
    error_message         text,

    created_at            timestamptz not null default now()
);

create index external_intelligence_calls_service_idx
    on public.external_intelligence_calls(target_service, request_type);
create index external_intelligence_calls_requested_at_idx
    on public.external_intelligence_calls(requested_at desc);
create unique index external_intelligence_calls_local_request_id_idx
    on public.external_intelligence_calls(local_request_id);

comment on table public.external_intelligence_calls is
    '外部Intelligence Service（現状はGeo Intelligenceのみ）への問い合わせ監査ログ。
     1回のquery_geo_intelligence()呼び出し（内部リトライ込み）につき1行。
     APIキー/Authorization/Token等の機微情報はrequest_payload/response_payloadに
     絶対に保存しない（geo_intelligence_service.py側でマスク済みの値のみ書き込む）';
comment on column public.external_intelligence_calls.status is
    'Sustainability側からみた通信状態。Geo自身の業務ステータスはremote_status列を参照';
comment on column public.external_intelligence_calls.remote_status is
    'Geo Intelligence自身が返す業務ステータス（completed/partial/failed）。
     Sustainability側のstatus列（通信状態）とは別物。Geoの業務失敗時もHTTP 200 +
     構造化レスポンスで返るため、その場合はstatus=success, remote_status=failedとなる';
comment on column public.external_intelligence_calls.source_type is
    '将来の呼び出し元追跡用（例: article）。Phase S1では未使用（常にNULL）';
comment on column public.external_intelligence_calls.remote_request_id is
    'Geoレスポンスがエコーバックしたrequest_id。local_request_idと不一致の場合は
     status=invalid_responseとして扱われるため、このカラムに値がある行は一致済み';

alter table public.external_intelligence_calls enable row level security;
