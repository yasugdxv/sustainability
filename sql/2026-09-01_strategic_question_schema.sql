-- Weekly Strategic Question / Organizational Decision Knowledge: 新規テーブル一式
-- 作成日: 2026-09-01
-- 実行前提: このファイルはユーザーがSupabase側で手動実行する。
-- 前提: sql/2026-09-01c_sustainability_strategy_facts.sql 適用済みであること（Phase 0）。
--
-- 設計方針（複数回のレビュー反映版）:
--   - 回答者識別はメールアドレスをURLに含めない。配信ごとにopaqueトークンを発行し、
--     ハッシュ化してDBに保存する（response_token_hash）。生トークンはメール本文にのみ存在する。
--   - トークン・recipient_keyの導出にはサーバー秘密鍵(STRATEGIC_QUESTION_HMAC_SECRET)を
--     使う（question_id自体を鍵にしない。question_idは秘密情報ではないため）。
--   - response tokenは乱数ではなくdelivery_idからHMAC-SHA256で決定論的に導出する
--     （送信直前にプロセスが落ちてpendingのまま孤児化したdeliveryでも再送できるようにする）。
--   - 配信対象のスナップショットを sustainability_strategic_question_deliveries に保存し、
--     配信数の分母を確定させる（回答率の正しい計算に必須）。
--   - decision_dimensionは自由文(_label)と固定enum(_key)の2本立てにする。重複回避・
--     将来の縦断分析はキーで行う。
--   - 質問のclose（時間経過によるquestion_status遷移）とDecision Insight生成（LLM呼び出し）
--     を分離する。insight_statusで管理し、LLM失敗時も再試行できるようにする。
--   - sustainability_decision_insightsに「先週の結果として既に掲載したか」を示す
--     included_in_report_id/included_atを持たせ、二重掲載を防ぐ。
--   - Decision Insightの自動supersedeは実装しない（MVP。同一テーマ×対立軸キーでも文脈が
--     異なる場合の誤消去を避ける）。is_current/superseded_byは将来拡張用に残すのみ。
--   - 本ファイルの全テーブルはRLSを有効化するがpolicyは定義しない。これは既存の
--     filter_keyword_tag_map等と同じ「service-role鍵経由のバックエンド処理のみが
--     読み書きし、ブラウザ/anon roleから直接Supabaseを読ませない」ことが前提の設計。
--     この前提を崩して「動かないからanon SELECT policyを追加する」対応をすると、
--     回答者の個人情報相当（recipient_email等）が意図せず公開されるため、絶対に行わないこと。

create table public.sustainability_strategic_questions (
    question_id            uuid primary key default gen_random_uuid(),

    period_start             date not null,
    period_end                 date not null,

    theme                        text not null
        check (theme in ('水','気候変動・GHG','容器包装','原料調達','生物多様性',
                          '人権','健康','人的資本','責任あるマーケティング')),

    -- 対立軸: 固定enum(_key)を正本とし、_labelはLLMが生成した自由文の見出し（表示用）
    decision_dimension_key       text not null
        check (decision_dimension_key in (
            'ambition_vs_achievability', 'short_term_vs_long_term', 'global_vs_local',
            'compliance_vs_leadership', 'cost_vs_sustainability_impact',
            'target_vs_structural_transformation', 'certification_vs_direct_intervention',
            'risk_mitigation_vs_opportunity', 'internal_action_vs_rule_making',
            'speed_vs_certainty', 'other'
        )),
    decision_dimension_label        text not null,

    title                             text not null,
    question_text                       text not null,

    analysis_json                          jsonb not null default '{}'::jsonb,
    candidates_json                           jsonb not null default '[]'::jsonb,
    ranking_score                                numeric,
    ranking_reasons                                 jsonb not null default '[]'::jsonb,

    evidence_article_cluster_ids               uuid[] not null default '{}',
    evidence_change_event_ids                     uuid[] not null default '{}',
    evidence_initiative_ids                          uuid[] not null default '{}',
    evidence_strategy_fact_ids                          uuid[] not null default '{}',

    model_deployment                                       text,
    prompt_version                                            text,
    token_usage                                                  jsonb,
    latency_ms                                                      integer
        check (latency_ms is null or latency_ms >= 0),

    status                                                            text not null
        check (status in ('success', 'error')),
    error_message                                                        text,

    question_status                                                        text not null default 'draft'
        check (question_status in ('draft', 'embedded', 'open', 'closed')),
    opens_at                                                                  timestamptz,
    closes_at                                                                    timestamptz,
    response_window_days                                                           integer not null default 5
        check (response_window_days > 0),

    -- close(質問の受付終了)とInsight生成(LLM)は別ライフサイクル。
    -- question_statusはcloses_at到来で機械的にclosedへ遷移し、その成否に関わらず確定する。
    -- insight_statusはその後のLLM処理の成否を独立して管理し、errorなら再試行可能にする。
    insight_status                                                                  text not null default 'pending'
        check (insight_status in ('pending', 'success', 'error')),
    insight_error_message                                                              text,

    embedded_in_report_id                                                                uuid
        references public.weekly_email_reports(report_id) on delete set null,

    created_at                                                                              timestamptz not null default now()
);

create unique index sustainability_strategic_questions_period_idx
    on public.sustainability_strategic_questions(period_start);
create index sustainability_strategic_questions_question_status_idx
    on public.sustainability_strategic_questions(question_status);
create index sustainability_strategic_questions_insight_status_idx
    on public.sustainability_strategic_questions(insight_status) where question_status = 'closed';
create index sustainability_strategic_questions_dimension_key_idx
    on public.sustainability_strategic_questions(theme, decision_dimension_key);

alter table public.sustainability_strategic_questions enable row level security;

comment on table public.sustainability_strategic_questions is
    '週1問の戦略トレードオフ質問。承認は週次ダイジェスト(weekly_email_reports)の承認に'
    'バンドルされる。question_status(draft→embedded→open→closed)は時間経過で機械的に'
    '確定し、insight_status(pending→success/error)はLLMによるDecision Insight生成の'
    '成否を独立して管理する（question_statusと結合しない）';
comment on column public.sustainability_strategic_questions.decision_dimension_key is
    '対立軸の固定分類。重複回避チェック・将来の縦断分析（テーマ×対立軸×時間での'
    '判断傾向の蓄積）はこのキーで行う。既存の型に当てはまらない場合はotherを許容する';


create table public.sustainability_strategic_question_options (
    option_id      uuid primary key default gen_random_uuid(),
    question_id     uuid not null
        references public.sustainability_strategic_questions(question_id) on delete cascade,

    option_code       text not null,
    display_order        integer not null default 0,

    label                   text not null,
    description               text,
    is_status_quo               boolean not null default false,

    created_at                    timestamptz not null default now(),

    unique (question_id, option_code)
);

create index sustainability_strategic_question_options_question_idx
    on public.sustainability_strategic_question_options(question_id);

alter table public.sustainability_strategic_question_options enable row level security;

comment on table public.sustainability_strategic_question_options is
    '質問1件につき2〜4件の選択肢。option_codeはメールのリンクにそのまま載る短い識別子（A/B/C等）';


-- 配信スナップショット: 「誰に送ったか」を確定し、回答率の分母を正しく計算できるようにする。
-- 回答者識別はメールアドレスそのものではなく response_token_hash / recipient_key で行う。
create table public.sustainability_strategic_question_deliveries (
    delivery_id             uuid primary key default gen_random_uuid(),
    question_id               uuid not null
        references public.sustainability_strategic_questions(question_id) on delete cascade,

    -- HMAC-SHA256(key=STRATEGIC_QUESTION_HMAC_SECRET, msg=f"recipient:{question_id}:{lower(email)}")。
    -- 鍵はサーバー秘密鍵（question_id自体を鍵にしない。question_idは秘密情報ではなく、
    -- メールアドレス候補と組み合わせれば第三者が再計算できてしまうため）。
    -- messageにquestion_idを含めることで質問(=週)ごとに値が変わり、週をまたいだ相関は
    -- 秘密鍵を知らない限り不可能
    recipient_key               text not null,
    -- 送信期間中の運用（再送・トラブルシュート）のためだけに一時保持する。
    -- question_status='closed'への遷移時にNULLへ更新する（close_due_questions内）
    recipient_email                text,

    -- sha256(raw_token)。raw_tokenは乱数ではなく
    -- HMAC-SHA256(key=STRATEGIC_QUESTION_HMAC_SECRET, msg=f"response:{delivery_id}")で
    -- 決定論的に導出する。delivery_id自体はDBに残り続けるため、送信直前にプロセスが落ちて
    -- send_status='pending'のまま孤児化したdeliveryでも、生トークンを再計算でき再送できる
    response_token_hash               text not null unique,

    send_status                          text not null default 'pending'
        check (send_status in ('pending', 'success', 'error')),
    send_error_message                      text,
    sent_at                                    timestamptz,
    opened_response_at                            timestamptz,
    responded_at                                     timestamptz,

    created_at                                          timestamptz not null default now(),

    unique (question_id, recipient_key)
);

create index sustainability_strategic_question_deliveries_question_idx
    on public.sustainability_strategic_question_deliveries(question_id);

alter table public.sustainability_strategic_question_deliveries enable row level security;

comment on table public.sustainability_strategic_question_deliveries is
    '質問1件を配信した対象者のスナップショット。response_token_hashで回答時の'
    'トークン照合を行う（生トークンはメール本文にのみ存在し、DBにはハッシュのみ保存）。'
    '回答率 = count(responded_at is not null) / count(send_status=''success'')。'
    'unique(question_id,recipient_key)により、activate_on_digest_approval()の再実行が'
    '冪等になる（同一recipientへ重複してdeliveryを発行しない）';
comment on column public.sustainability_strategic_question_deliveries.recipient_email is
    '送信期間中(question_status=open)のみ保持する運用データ。closed遷移時にNULL化する';


create table public.sustainability_strategic_question_responses (
    response_id           uuid primary key default gen_random_uuid(),
    question_id             uuid not null
        references public.sustainability_strategic_questions(question_id) on delete cascade,
    option_id                 uuid not null
        references public.sustainability_strategic_question_options(option_id) on delete cascade,
    delivery_id                 uuid not null
        references public.sustainability_strategic_question_deliveries(delivery_id) on delete cascade,

    -- deliveries.recipient_keyの複製（deliveriesが将来クリーンアップされても
    -- 回答自体の集計・分析が破綻しないようにする）
    respondent_key                 text not null,
    comment                            text,

    submitted_at                         timestamptz not null default now(),
    updated_at                               timestamptz not null default now()
);

create unique index sustainability_strategic_question_responses_dedup_idx
    on public.sustainability_strategic_question_responses(question_id, respondent_key);
create index sustainability_strategic_question_responses_question_idx
    on public.sustainability_strategic_question_responses(question_id);

create trigger sustainability_strategic_question_responses_set_updated_at
    before update on public.sustainability_strategic_question_responses
    for each row execute function public.set_updated_at();

alter table public.sustainability_strategic_question_responses enable row level security;

comment on table public.sustainability_strategic_question_responses is
    '社員からの回答。メールアドレスは保持しない（respondent_keyはdeliveries.recipient_keyと'
    '同じ不可逆キー、個人プロファイル化をしない設計思想に合わせる）。'
    '同一質問+同一respondent_keyでの再送信は上書き（回答変更）として扱う。'
    'GETのみでは記録されない（確認ページ表示→POST送信の2段階）';


create table public.sustainability_decision_insights (
    insight_id             uuid primary key default gen_random_uuid(),
    question_id              uuid not null
        references public.sustainability_strategic_questions(question_id) on delete cascade,

    decision_dimension_key      text not null,
    decision_dimension_label       text not null,
    theme                             text not null
        check (theme in ('水','気候変動・GHG','容器包装','原料調達','生物多様性',
                          '人権','健康','人的資本','責任あるマーケティング')),

    observed_tendency                   text not null,
    distribution_json                      jsonb not null default '{}'::jsonb,
    response_count                           integer not null default 0
        check (response_count >= 0),
    delivery_count                              integer not null default 0
        check (delivery_count >= 0),
    reasoning_summary                              text,
    -- 原文コメントの引用ではなく、理由づけパターンを意味レベルで一般化した文の配列
    -- （個人特定情報が残らない形にする）
    representative_reasoning                          jsonb not null default '[]'::jsonb,
    confidence                                           numeric
        check (confidence is null or (confidence >= 0 and confidence <= 1)),

    guardrail_label                                         text not null
        default '社内の回答者から観測された傾向であり、会社の正式方針ではありません',

    observed_at                                                timestamptz not null default now(),

    -- MVPでは自動supersedeしない（is_current常にtrue、superseded_byは常にNULL）。
    -- 同一テーマ×対立軸キーでも背景の文脈が異なる場合があり、機械的な自動supersedeは
    -- 誤って有効な過去シグナルを消しかねないため。カラム自体は将来の
    -- 文脈単位でのsupersede実装のために残す
    is_current                                                    boolean not null default true,
    superseded_by                                                    uuid
        references public.sustainability_decision_insights(insight_id) on delete set null,

    -- 「先週の結果」として週次メールに掲載済みかどうか。二重掲載防止に必須。
    included_in_report_id                                                  uuid
        references public.weekly_email_reports(report_id) on delete set null,
    included_at                                                               timestamptz,

    model_deployment                                                              text,
    prompt_version                                                                   text,
    token_usage                                                                         jsonb,
    latency_ms                                                                             integer
        check (latency_ms is null or latency_ms >= 0),

    created_at                                                                                timestamptz not null default now()
);

create index sustainability_decision_insights_dimension_idx
    on public.sustainability_decision_insights(theme, decision_dimension_key);
create index sustainability_decision_insights_current_idx
    on public.sustainability_decision_insights(theme, is_current) where is_current;
create index sustainability_decision_insights_observed_idx
    on public.sustainability_decision_insights(observed_at desc);
create index sustainability_decision_insights_unincluded_idx
    on public.sustainability_decision_insights(included_in_report_id) where included_in_report_id is null;

alter table public.sustainability_decision_insights enable row level security;

comment on table public.sustainability_decision_insights is
    'クローズした戦略質問1件につき1行、LLMで蒸留した「組織内の判断傾向」。'
    'is_current/superseded_byはMVPでは未使用（常にis_current=true）。将来、文脈単位での'
    'supersede判定を実装する際に使う想定でカラムのみ用意している。'
    'included_in_report_id/included_atで「先週の結果」への掲載済み管理を行い、'
    '同じ結果が複数週にまたがって再掲載されるのを防ぐ。'
    'response_count/delivery_count が config.strategic_question の'
    'min_responses_for_publish/min_response_rate_for_publish のAND条件を満たさない行は'
    'チャット参照・週次メール掲載のどちらからも除外する（strategic_question_service.py参照）。'
    'チャットからの参照時はguardrail_labelを必ず併記し、正式方針として提示してはならない';
