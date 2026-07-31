-- =====================================================================
-- 競合サステナビリティモニタリング: 日次アラートダイジェスト
-- 作成日: 2026-07-27（2026-07-27〜2026-07-27cの後に実行する）
--
-- 内容:
--   変更イベント1件ごとの即時個別配信(competitor_alerts)に代えて、
--   1日1回・その日確定した変更イベントをまとめて1通のメールにし、PMOが
--   レビューしてから送信する（weekly_email_reports/monthly_competitor_reportsと
--   同じレビューゲート構成）。ただし全イベントが確信度高くレビュー不要の日は
--   従来通り自動配信する。
--
--   競合monitoring既存のcompetitor_alertsテーブルは変更・削除しない
--   （過去データは履歴として残す。今後この日次ダイジェストに置き換わり、
--   新規行は作られなくなる）。
-- =====================================================================

create table public.competitor_daily_alert_digests (
    digest_id            uuid primary key default gen_random_uuid(),
    digest_date            date not null unique,
    change_event_ids         uuid[] not null default '{}',

    subject                    text,
    html_body                    text,
    auto_send_eligible              boolean not null default false,

    review_status                     text not null default 'review_required'
        check (review_status in ('review_required', 'approved', 'rejected', 'sent')),
    reviewer_id                          text,
    reviewer_feedback                      jsonb,
    reviewed_at                              timestamptz,

    sent_at                                    timestamptz,
    send_status                                  text
        check (send_status is null or send_status in ('success', 'error')),
    send_error_message                             text,
    send_mode                                        text
        check (send_mode is null or send_mode in ('smtp', 'preview')),
    recipients                                         text[],

    created_at                                           timestamptz not null default now(),
    updated_at                                             timestamptz not null default now()
);

create index competitor_daily_alert_digests_review_status_idx
    on public.competitor_daily_alert_digests(review_status);

create trigger competitor_daily_alert_digests_set_updated_at
    before update on public.competitor_daily_alert_digests
    for each row execute function public.set_updated_at();

alter table public.competitor_daily_alert_digests enable row level security;

comment on table public.competitor_daily_alert_digests is
    '競合サステナ変更イベントの日次ダイジェスト。1日1行(digest_date一意)。'
    'auto_send_eligible=trueの日はレビュー無しで自動配信、それ以外はPMOレビュー後に送信する。'
    '旧来の変更イベント1件ごとの個別アラート(competitor_alerts)を置き換える';
comment on column public.competitor_daily_alert_digests.auto_send_eligible is
    'その日ダイジェストに含めた全変更イベントがreview_required=falseかつ'
    'confidenceがcompetitor_monitoring.auto_send_confidence_threshold以上だったか';
