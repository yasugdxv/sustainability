-- =====================================================================
-- 重要度判定ルール格納テーブル（新規構築＋シードデータ投入）
-- 作成日: 2026-07-17
-- 対象: Supabase（Supabase SQL Editorで実行）
-- 元資料: 重要度判定.docx
--
-- 内容:
--   記事の重要度判定（7項目採点 → S/A/B/C/Dランク決定 → 補正 → 掲載区分決定）
--   のルール一式を、将来Agentが判定時に参照できる形でテーブル化する。
--   実データ（記事分析結果）は article_analysis に引き続き保存する。
--
-- 前提: public.set_updated_at() が既に作成済みであること
--   （2026-07-15_crawl_and_tag_schema_v2.sql 参照）
-- =====================================================================


-- =====================================================================
-- 1. importance_criteria（7つの評価項目）
-- =====================================================================
create table public.importance_criteria (
    criterion_id    text primary key,
    display_order   integer not null,
    name_ja         text not null,
    description     text not null,
    max_score       integer not null default 5 check (max_score > 0),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create trigger importance_criteria_set_updated_at
    before update on public.importance_criteria
    for each row execute function public.set_updated_at();

insert into public.importance_criteria (criterion_id, display_order, name_ja, description) values
    ('business_relevance', 1, 'サントリー・事業関連性', '当社事業、サス推業務、主要テーマへの関係の強さ'),
    ('change_severity',    2, '変化の重大性',           '従来の制度・目標・市場前提をどの程度変えるか'),
    ('impact_scope',       3, '影響範囲',               '影響する地域、企業、業界、バリューチェーンの広さ'),
    ('certainty_stage',    4, '確度・進展段階',         '構想、提案、正式決定、施行等の進展度'),
    ('urgency',            5, '緊急性',                 '対応・確認が必要になるまでの時間'),
    ('novelty',            6, '新規性',                 '既知情報に対する新しい事実・変更の大きさ'),
    ('decision_value',     7, '意思決定・行動価値',     '担当者や経営が何らかの確認・判断・対応に使えるか');


-- =====================================================================
-- 2. importance_score_bands（各評価項目の0〜5点の判定基準）
-- =====================================================================
create table public.importance_score_bands (
    criterion_id  text not null
        references public.importance_criteria(criterion_id) on delete cascade,
    score         integer not null check (score between 0 and 5),
    definition    text not null,
    primary key (criterion_id, score)
);

insert into public.importance_score_bands (criterion_id, score, definition) values
    ('business_relevance', 5, 'サントリーの事業、商品、操業、調達、開示、主要市場に直接影響する'),
    ('business_relevance', 4, '飲料・酒類・食品事業全体に影響し、当社にも高い確率で影響する'),
    ('business_relevance', 3, '主要テーマ、競合、バリューチェーンに関係し、担当者の確認対象となる'),
    ('business_relevance', 2, 'サステナビリティ領域として参考になるが、当社への影響は間接的'),
    ('business_relevance', 1, '周辺領域の一般情報で、業務との接続が弱い'),
    ('business_relevance', 0, '当社・サス推業務との実質的な関係がない'),

    ('change_severity', 5, '既存の事業前提・規制対応・戦略を大きく変更させる可能性がある'),
    ('change_severity', 4, '重要な義務、基準、企業方針、目標に明確な変更がある'),
    ('change_severity', 3, '運用、対象範囲、期限、数値基準等に実質的な更新がある'),
    ('change_severity', 2, '継続案件の進捗や部分的な修正'),
    ('change_severity', 1, '背景説明、再確認、軽微な更新'),
    ('change_severity', 0, '実質的な変化がない'),

    ('impact_scope', 5, 'グローバルまたは複数地域・複数事業に影響'),
    ('impact_scope', 4, 'EU、米国等の主要市場全体、または業界全体に影響'),
    ('impact_scope', 3, '主要国、複数企業、特定バリューチェーンに影響'),
    ('impact_scope', 2, '一部地域、一部企業、特定商品群に限定'),
    ('impact_scope', 1, '単一企業・単一地域の限定的事例'),
    ('impact_scope', 0, '影響範囲が不明、または実質的な影響なし'),

    ('certainty_stage', 5, '施行・発効・正式運用開始・確定した判決・実際の重大事象'),
    ('certainty_stage', 4, '正式決定、成立、最終規則、公表済みの確定方針'),
    ('certainty_stage', 3, '規制案、公開草案、正式協議、企業の公式計画'),
    ('certainty_stage', 2, '政策方針、検討開始、予告、試行段階'),
    ('certainty_stage', 1, '発言、提言、観測、未確定報道'),
    ('certainty_stage', 0, '根拠不明、噂、内容の確認不能'),

    ('urgency', 5, '即時～1か月以内に確認・対応が必要'),
    ('urgency', 4, '1～3か月以内に対応準備が必要'),
    ('urgency', 3, '3～12か月以内に対応・計画反映が必要'),
    ('urgency', 2, '1年以上先だが準備期間を考慮して早期把握が必要'),
    ('urgency', 1, '時期未定、長期的な監視対象'),
    ('urgency', 0, '対応時期との関係がない'),

    ('novelty', 5, '初出の重大情報、従来認識を覆す変更'),
    ('novelty', 4, '重要な条件、期限、数値、対象範囲の更新'),
    ('novelty', 3, '継続案件の明確な進展'),
    ('novelty', 2, '追加情報はあるが結論や方向性は変わらない'),
    ('novelty', 1, '既知情報の再報道・再整理'),
    ('novelty', 0, '完全な重複'),

    ('decision_value', 5, '経営・本部として直ちに判断、エスカレーション、対応確認が必要'),
    ('decision_value', 4, 'テーマ担当や関係部署が方針・計画・目標を確認すべき'),
    ('decision_value', 3, '今後の対応準備、ベンチマーク、リスク確認に有用'),
    ('decision_value', 2, '背景理解や将来検討に有用'),
    ('decision_value', 1, '一般的な知識として参考になる'),
    ('decision_value', 0, '読んでも具体的な判断・行動につながらない');


-- =====================================================================
-- 3. importance_rank_definitions（S/A/B/C/Dランクの点数範囲・定義）
-- =====================================================================
create table public.importance_rank_definitions (
    rank                  text primary key
        check (rank in ('S', 'A', 'B', 'C', 'D')),
    display_order         integer not null,
    name_ja               text not null,
    min_score             integer not null check (min_score >= 0),
    max_score             integer not null check (max_score >= min_score),
    definition            text not null,
    default_publication    text not null
);

insert into public.importance_rank_definitions (rank, display_order, name_ja, min_score, max_score, definition, default_publication) values
    ('S', 1, '最重要', 29, 35, '即時に共有・確認すべき重大情報',           '速報またはメール冒頭＋Notion'),
    ('A', 2, '重要',   23, 28, '本部・テーマ担当が把握すべき重要情報',     '週次メール＋Notion'),
    ('B', 3, '注目',   17, 22, '今後の進展を継続して見るべき情報',         'メール候補＋Notion'),
    ('C', 4, '参考',   10, 16, '背景理解・情報蓄積として有用',             '原則Notionのみ'),
    ('D', 5, '低関連',  0,  9, '関連性・新規性・行動価値が低い',           '原則非掲載');


-- =====================================================================
-- 4. importance_correction_rules（最低ランク・強制補正ルール）
-- =====================================================================
create table public.importance_correction_rules (
    rule_id         uuid primary key default gen_random_uuid(),
    display_order   integer not null,
    condition_text  text not null,
    correction      text not null
);

insert into public.importance_correction_rules (display_order, condition_text, correction) values
    (1,  '主要地域における規制の正式成立・施行',                 '最低A'),
    (2,  'サントリーに直接適用する新規義務・罰則',               '最低S'),
    (3,  '競合20社の重要目標・KPIの撤回、延期、大幅修正',        '最低A'),
    (4,  '競合20社の軽微な目標進捗更新',                         '原則B以下'),
    (5,  '業界に波及する重要判決・行政処分',                     '最低A'),
    (6,  '主要原料の供給、価格、産地に重大な影響',               '最低A'),
    (7,  '主要地域での取水制限・操業停止等',                     '最低A'),
    (8,  'グローバル基準の重要改定',                             '最低A'),
    (9,  '一次未確認の解釈系記事',                               '原則B以下'),
    (10, '内容が完全に重複する記事',                             'Dまたは代表記事へ統合'),
    (11, '単なるイベント告知・表彰・CSR広報',                    '原則C以下');


-- =====================================================================
-- 5. source_reliability_rules（情報信頼性による掲載制御）
--    「情報源の信頼性」は重要度の加点項目にはせず、掲載可否や確信度を決める
--    別軸とする（一次情報だから重要とは限らず、二次情報でも重要な兆候を
--    検知する場合があるため）。
-- =====================================================================
create table public.source_reliability_rules (
    state              text primary key,
    display_order      integer not null,
    definition         text not null,
    publication_rule   text not null
);

insert into public.source_reliability_rules (state, display_order, definition, publication_rule) values
    ('一次情報',     1, '法令、規制当局、企業開示、一次データ',           '通常掲載可能'),
    ('一次照合済み', 2, '二次記事を起点に一次情報を確認済み',             '一次情報を主出典として掲載'),
    ('解釈・分析',   3, 'コンサル、メディア、専門家等の分析',             '【解釈・分析】表示で掲載可能'),
    ('一次未確認',   4, '事実主張の一次情報が確認できない',               '原則メール掲載を抑え、Notion中心'),
    ('出所不明',     5, '情報源や根拠が確認できない',                     '非掲載');


-- =====================================================================
-- 6. importance_rubric_notes（判定フロー・概要・設計上の注記・具体例）
--    category:
--      overview          = 制度概要（判定目的・単位・タイミング等）
--      process_step      = 判定フローの7ステップ
--      design_note       = 補足の設計方針メモ（本文中の注記）
--      criterion_example = 評価項目ごとの判定要素・代表例
-- =====================================================================
create table public.importance_rubric_notes (
    note_id                 uuid primary key default gen_random_uuid(),
    category                text not null
        check (category in ('overview', 'process_step', 'design_note', 'criterion_example')),
    related_criterion_id    text
        references public.importance_criteria(criterion_id) on delete set null,
    display_order           integer not null,
    title                   text not null,
    content                 text not null
);

create index importance_rubric_notes_category_idx
    on public.importance_rubric_notes(category, display_order);

-- ---- overview（判定の基本仕様） ----
insert into public.importance_rubric_notes (category, display_order, title, content) values
    ('overview', 1, '判定目的',       '記事ごとの表示優先度を決定し、週次メール掲載、Notion掲載、除外を振り分ける'),
    ('overview', 2, '判定単位',       '1記事または同一事象単位'),
    ('overview', 3, '判定タイミング', '取得・重複統合・タグ付与後'),
    ('overview', 4, '判定方法',       '必須条件確認 → 評価項目採点 → 補正ルール適用 → 掲載枠調整'),
    ('overview', 5, '評価軸',         '事業関連性、変化の重大性、影響範囲、確度、緊急性、新規性、意思決定価値'),
    ('overview', 6, '出力',           '重要度ランク、総合点、判定理由、掲載区分、要確認フラグ'),
    ('overview', 7, '判定主体',       'Agentが全件判定。PMOは高重要度記事と一部サンプルを確認'),
    ('overview', 8, '判定根拠',       '各評価項目の点数と根拠記述を保存する'),
    ('overview', 9, '見直し',         '四半期ごとに判定精度、掲載件数、閲読実績を踏まえて調整');

-- ---- process_step（判定フロー7ステップ） ----
insert into public.importance_rubric_notes (category, display_order, title, content) values
    ('process_step', 1, '1. 対象外判定',       'サステナビリティとの関連性がない記事、重複、広告、内容不足等を除外'),
    ('process_step', 2, '2. 事実確認状態の判定', '一次情報、一次確認済み、解釈系、一次未確認を区分'),
    ('process_step', 3, '3. 重要度採点',       '7つの評価項目を採点'),
    ('process_step', 4, '4. 補正',             '規制施行、目標撤回、重大訴訟等について加点・最低ランク設定'),
    ('process_step', 5, '5. ランク決定',       '最重要、重要、注目、参考、除外に分類'),
    ('process_step', 6, '6. 掲載区分決定',     '速報、週次メール、Notionのみ、非掲載を決定'),
    ('process_step', 7, '7. 全体調整',         '重複、テーマ偏重、主体偏重を調整し、週15～20件に絞る');

-- ---- design_note（本文中の補足メモ） ----
insert into public.importance_rubric_notes (category, related_criterion_id, display_order, title, content) values
    ('design_note', null,               1, '情報源の信頼性は別軸',
        '「情報源の信頼性」は重要度の加点項目にはせず、掲載可否や確信度を決める別軸とする（一次情報だから重要とは限らず、二次情報でも重要な兆候を検知する場合があるため）。判定は source_reliability_rules を参照。'),
    ('design_note', 'impact_scope',      2, '単一企業事例の例外評価',
        'ただし、単一企業の記事でも、競合20社の目標撤回や先行事例であれば、別項目（関連性・意思決定価値）を高く評価する。'),
    ('design_note', 'certainty_stage',   3, '確度と情報源信頼性は別概念',
        '「確度」と「情報源の信頼性」は別。例えば、政府の公式発表でも「検討開始」であれば進展段階は2とする。'),
    ('design_note', 'novelty',           4, '新規性は差分で判定',
        '新規性は、記事単体ではなく、過去に取得済みの記事・レコードとの差分で判定する。'),
    ('design_note', null,               5, 'ランク閾値は初期案',
        'S〜Dの点数閾値（importance_rank_definitions）は初期案。過去記事に適用して、週15～20件程度になるよう調整する。'),
    ('design_note', null,               6, '補正ルールを設ける理由',
        '単純合計だけでは重大記事が落ちる可能性があるため、特定事象には最低ランク（importance_correction_rules）を設ける。');

-- ---- criterion_example（評価項目ごとの判定要素・代表例） ----
insert into public.importance_rubric_notes (category, related_criterion_id, display_order, title, content) values
    ('criterion_example', 'business_relevance', 1, 'テーマ',     '水、GHG、容器包装等の主要テーマに該当するか'),
    ('criterion_example', 'business_relevance', 2, '地理',       '主要・準主要地域に該当するか'),
    ('criterion_example', 'business_relevance', 3, '主体',       '競合20社、主要規制当局、重要イニシアチブ等か'),
    ('criterion_example', 'business_relevance', 4, '原料',       '重要9品目に関係するか'),
    ('criterion_example', 'business_relevance', 5, '業務接続',   '開示、目標管理、調達、操業、商品設計等に接続するか'),

    ('criterion_example', 'change_severity', 1, '規制',         '新法成立、適用対象拡大、義務新設、罰則強化'),
    ('criterion_example', 'change_severity', 2, '基準',         '開示項目追加、認定要件変更、評価方法改定'),
    ('criterion_example', 'change_severity', 3, '競合',         '目標撤回、期限延期、KPI大幅変更、大型投資'),
    ('criterion_example', 'change_severity', 4, '訴訟',         '重要判決、大規模罰金、業界に波及する法解釈'),
    ('criterion_example', 'change_severity', 5, '物理リスク',   '生産停止、原料供給障害、主要地域の取水制限'),

    ('criterion_example', 'urgency', 1, '優先度1: 施行日・適用開始日',   '緊急性の判定対象日として最優先'),
    ('criterion_example', 'urgency', 2, '優先度2: 対応期限・報告期限',   ''),
    ('criterion_example', 'urgency', 3, '優先度3: 意見募集期限',         ''),
    ('criterion_example', 'urgency', 4, '優先度4: 企業目標期限・移行期限', ''),
    ('criterion_example', 'urgency', 5, '優先度5: 発表日',               ''),

    ('criterion_example', 'decision_value', 1, 'エスカレーション', '重大規制、訴訟、競合目標撤回'),
    ('criterion_example', 'decision_value', 2, '担当者確認',       '対応状況、対象製品、開示影響の確認'),
    ('criterion_example', 'decision_value', 3, '計画反映',         '投資計画、調達方針、目標設定'),
    ('criterion_example', 'decision_value', 4, 'ベンチマーク',     '競合の先行施策、業界標準'),
    ('criterion_example', 'decision_value', 5, '継続監視',         '規制案、評価手法変更、NGOキャンペーン');


-- =====================================================================
-- 7. article_analysis.importance_level を S/A/B/C/D の5段階に変更
--    （現時点で article_analysis は0件のため、既存データへの影響なし）
--    制約名が既知でない場合に備え、importance_level列の既存CHECK制約を
--    pg_constraintから動的に特定して削除する
-- =====================================================================
do $$
declare
    con record;
begin
    for con in
        select conname
        from pg_constraint
        where conrelid = 'public.article_analysis'::regclass
          and contype = 'c'
          and pg_get_constraintdef(oid) ilike '%importance_level%'
    loop
        execute format('alter table public.article_analysis drop constraint %I', con.conname);
    end loop;
end $$;

alter table public.article_analysis
    add constraint article_analysis_importance_level_check
        check (importance_level is null or importance_level in ('S', 'A', 'B', 'C', 'D'));

-- 資料の「出力」欄（重要度ランク、総合点、判定理由、掲載区分、要確認フラグ）に
-- 合わせて、採点根拠を残せる列を追加する
alter table public.article_analysis
    add column if not exists importance_total_score integer
        check (importance_total_score is null or importance_total_score between 0 and 35),
    add column if not exists importance_scores jsonb,
    add column if not exists publication_category text,
    add column if not exists needs_review boolean not null default false;

comment on column public.article_analysis.importance_level is
    'importance_rank_definitions.rank（S/A/B/C/D）を参照';
comment on column public.article_analysis.importance_total_score is
    '7項目（各0-5点）の合計点。importance_rank_definitionsの点数範囲と対応';
comment on column public.article_analysis.importance_scores is
    '評価項目ごとの点数を保持するJSON。例: {"business_relevance": 5, "change_severity": 4, ...}';
comment on column public.article_analysis.publication_category is
    '掲載区分（速報／週次メール／Notionのみ／非掲載）。importance_rank_definitions.default_publicationを起点に決定';
comment on column public.article_analysis.needs_review is
    'PMOによる確認が必要な要確認フラグ（高重要度記事・サンプルチェック対象等）';


-- =====================================================================
-- 8. RLS 有効化（他テーブルと統一。ポリシーは付与しない＝service_role専用）
-- =====================================================================
alter table public.importance_criteria enable row level security;
alter table public.importance_score_bands enable row level security;
alter table public.importance_rank_definitions enable row level security;
alter table public.importance_correction_rules enable row level security;
alter table public.source_reliability_rules enable row level security;
alter table public.importance_rubric_notes enable row level security;
