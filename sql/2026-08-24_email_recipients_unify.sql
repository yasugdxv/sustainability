-- 配信先管理の統一: competitor_recipients（競合速報・競合月次専用）を
-- 週次ダイジェストも含めた全メール種別共通の email_recipients に拡張する。
-- config.json の email.to_addresses（固定配列）は廃止し、DBで宛先を管理する。

alter table public.competitor_recipients rename to email_recipients;

alter table public.email_recipients
    add column notify_weekly_digest boolean not null default false;

-- テスト送信専用の受信者フラグ。true=テスト送信のみ対象、false=本番配信のみ対象
-- （本番と混ざらないよう明確に分離する。1人が両方受け取りたい場合は2行登録する）
alter table public.email_recipients
    add column is_test boolean not null default false;

comment on table public.email_recipients is
    '週次ダイジェスト・競合速報（即時アラート）・競合月次レポートの配信先を統一管理する。'
    'notify_weekly_digest/notify_immediate_alert/notify_monthly_reportで通知種別ごとに'
    'ON/OFFし、is_test=trueの行はテスト送信（--mode test）でのみ対象になる'
    '（本番配信では除外される）。';
