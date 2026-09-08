-- Secondary Redundancy Classification / Representative Source Priority
-- 作成日: 2026-09-02
--
-- 背景: 同一イベントについて一次情報と二次情報が両方存在する場合、単なる要約・言い換えに
-- 過ぎない二次記事が一次情報より高く評価・重複掲載されることを防ぐ。既存の7項目重要度
-- スコア（importance_scores/importance_total_score）は最終値のまま維持し、既存の全読み取り
-- コード（api_server.py, sustainability_dashboard_core.fetch_articles_with_tags,
-- weekly_email_report.py等）は無改修で動く。
--
-- 設計方針:
--   - importance_scores_rawに補正前のLLM自己申告そのものを保持し、後から理由を追えるようにする。
--     補正が無い記事はimportance_scoresと同一内容になる。
--   - Representative Source Priorityは数値の優先度スコア表ではなく、週次選定からの除外・
--     UI表示の3区分に必要十分な4値enum(representative_role)で表現する。
--   - secondary_redundancy_classification/matched_primary_article_id/redundancy_reasonは
--     「二次・解釈系記事 かつ 一次情報候補が見つかった場合」のみ非NULLになる
--     （出所不明や候補が無い記事はNULLのまま、representative_roleは既定値'representative'）。

alter table public.article_analysis
    add column importance_scores_raw jsonb,
    add column secondary_redundancy_classification text
        check (secondary_redundancy_classification in
               ('redundant_summary', 'value_added_reporting', 'value_added_analysis')),
    add column matched_primary_article_id uuid
        references public.articles(article_id) on delete set null,
    add column redundancy_reason text,
    add column representative_role text not null default 'representative'
        check (representative_role in
               ('representative', 'supporting_analysis', 'additional_reporting', 'suppressed_duplicate'));

create index article_analysis_matched_primary_idx on public.article_analysis(matched_primary_article_id);

comment on column public.article_analysis.importance_scores_raw is
    '7項目スコアのLLM自己申告そのまま（機械補正前）。secondary_redundancy_classification='
    'redundant_summaryの場合のみimportance_scoresと異なる値になる（novelty/decision_valueを'
    '各-1、0未満にはしない）';
comment on column public.article_analysis.secondary_redundancy_classification is
    '同一イベントの一次情報が見つかった二次・解釈系記事についてのみ判定する。'
    'redundant_summary=一次情報の要約・言い換えに留まる（機械補正の対象）、'
    'value_added_reporting=独自取材・追加データ等、value_added_analysis=独自の分析・示唆。'
    '候補が見つからない/一次情報自体/出所不明の記事はNULLのまま';
comment on column public.article_analysis.matched_primary_article_id is
    'タグ重複・公開日近接のヒューリスティックで候補抽出し、LLMが同一イベントと判定した'
    '一次情報記事のarticles.article_id。正式なクラスタリング基盤ではないため、'
    'タグが大きくずれる・報道時期が離れている等の理由で一次情報が見つからない場合がある';
comment on column public.article_analysis.representative_role is
    '週次選定・将来のUI表示で使う代表ソース区分。suppressed_duplicateは週次選定候補から'
    '除外する（sustainability_article_selector.py参照）。Importance Scoreには一切加算しない';
