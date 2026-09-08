-- Weekly Strategic Question機能の前提（Phase 0）: 当社サステナビリティ戦略の構造化ファクト
-- 作成日: 2026-09-01
-- 実行前提: このファイルはユーザーがSupabase側で手動実行する。
--
-- 背景: 既存のsuntory_sustainability_expert_base.json（headline_targets）は水・気候・容器の
-- 3テーマ・計7件のみで、Strategic Analysisの入力として使うには薄い。
-- knowledge/sustainability_expert/source_registry.csvに登録済みの公式ソースURL
-- （サントリー公式サステナビリティサイト、26件）のうち未取込の16件を、
-- expand_sustainability_knowledge.py で取得・構造化する先として本テーブルを新設する。
--
-- Source(source_registry.csv) → Structured Strategy Fact(本テーブル) →
-- Theme Context Summary(knowledge_documents.jsonl) の3層構造の中間層にあたる。

create table public.sustainability_strategy_facts (
    fact_id                uuid primary key default gen_random_uuid(),
    theme                    text not null
        check (theme in ('水','気候変動・GHG','容器包装','原料調達','生物多様性',
                          '人権','健康','人的資本','責任あるマーケティング')),
    strategy_element_type      text not null
        check (strategy_element_type in ('numeric_target','direction','policy_commitment','major_action')),

    strategic_direction           text,
    target_metric                    text,
    target_value                        text,
    baseline                               text,
    target_year                              integer,
    scope                                       text,
    geography                                     text,
    major_actions                                    text[] not null default '{}',
    policy_or_commitment                                text,

    source_url                                             text not null,
    source_title                                              text,
    effective_date                                               text,
    retrieved_at                                                    timestamptz not null default now(),
    created_at                                                         timestamptz not null default now(),

    -- sha256(正規化した theme+strategy_element_type+strategic_direction+target_metric+
    -- target_value+target_year+scope+source_url)。同じExcelのapplyを複数回実行しても
    -- 重複INSERTされないようにする
    fact_fingerprint                                                       text not null
);

create index sustainability_strategy_facts_theme_idx on public.sustainability_strategy_facts(theme);
create index sustainability_strategy_facts_element_type_idx
    on public.sustainability_strategy_facts(strategy_element_type);
create unique index sustainability_strategy_facts_fingerprint_idx
    on public.sustainability_strategy_facts(fact_fingerprint);

alter table public.sustainability_strategy_facts enable row level security;

comment on table public.sustainability_strategy_facts is
    '公式サステナビリティサイト（source_registry.csv記載URL）から抽出した、当社の戦略・目標・'
    '施策の構造化ファクト。knowledge_documents.jsonlの「判断軸要約」はこのテーブルを土台に'
    '生成する（生ページ本文からの直接要約にしない）。strategic_question_service.pyの'
    'strategy_relevanceスコア算出（テーマ別カバレッジ判定）にも使う。'
    'fact_fingerprintのユニーク制約により、同じ内容のFactをapplyコマンド再実行で'
    '重複INSERTしない（upsert相当の冪等性）。RLSはpolicy未定義（service-role鍵経由の'
    'バックエンド処理のみが読み書きする前提。anon SELECT policyを追加しないこと）';
comment on column public.sustainability_strategy_facts.strategy_element_type is
    'numeric_target: 数値目標（target_value/target_year等を伴う）。'
    'direction: 定性的な戦略方向性。policy_commitment: 方針・コミットメント表明。'
    'major_action: 個別の具体的施策（方針レベルの記述を伴わないもの）。'
    'カバレッジ判定(sufficient/partial/insufficient)ではmajor_actionのみの場合はpartial扱いとし、'
    'numeric_target/direction/policy_commitmentのいずれかがあればsufficientとする'
    '（数値目標の有無は必須要件にしない）';
