-- フィルタールール 案A対応: article_analysis.analysis_status に 'フィルタ除外' を追加
-- 背景: 現状の実装(article_crawler.py)は「フィルタ非通過記事はarticlesへのINSERT自体を
-- スキップ」する案B。PMOフィードバックにより「保存はしたうえでLLM分析前にスキップする」
-- 案A（記事本体は残し、article_analysisにフィルタ除外である旨だけ記録）へ変更するため、
-- CHECK制約にこの値を追加する。
--
-- 実行前提: このファイルはユーザーがSupabase側で手動実行する
-- （このプロジェクトの運用ルールとして、スキーマ変更(DDL)はユーザー自身が実行する）。

alter table public.article_analysis
    drop constraint article_analysis_analysis_status_check;

alter table public.article_analysis
    add constraint article_analysis_analysis_status_check
    check (analysis_status in ('処理済', '要確認', '承認済', '差戻し', 'フィルタ除外'));
