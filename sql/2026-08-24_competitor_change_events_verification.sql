-- 競合改訂速報: 一次開示照合結果を competitor_change_events に追加保存する
-- （通常記事側の article_analysis とは別。競合side change eventのみを対象とする）
--
-- 既存のbefore_record_id/after_record_idからcompetitor_target_records.source_url経由で
-- 旧値・新値それぞれの出典URLを追跡できるため、old_source_url/new_source_url相当の列は
-- 重複して作らない。

alter table public.competitor_change_events
    add column verification_status text
        check (verification_status is null or verification_status in
               ('VERIFIED', 'PARTIALLY_VERIFIED', 'UNVERIFIED', 'CONTRADICTED')),
    add column primary_source_url text,
    add column primary_source_title text,
    add column primary_source_domain text,
    add column primary_source_document_type text,
    add column verification_evidence text,
    add column verification_reason text,
    add column verification_method text,
    add column verification_error text,
    add column verified_at timestamptz;

create index competitor_change_events_verification_idx
    on public.competitor_change_events(verification_status);

comment on column public.competitor_change_events.verification_status is
    'NULL=未照合。競合改訂速報(competitor_daily_digest.py)はVERIFIEDのみを自動配信候補とする。'
    'WORDING_ONLY/SIMPLE_REPUBLISHは実質変更でないため照合自体を行わずUNVERIFIEDを設定する';
comment on column public.competitor_change_events.verification_method is
    'primary_source_document=登録済み公式ソースの本文を再取得し照合／'
    'web_search=公式ソース以外から検知されたためWeb検索で公式開示を探索／'
    'skipped_non_meaningful=WORDING_ONLY等のため照合スキップ／'
    'primary_source_refetch_failed・no_source_url・error=エラー系';
