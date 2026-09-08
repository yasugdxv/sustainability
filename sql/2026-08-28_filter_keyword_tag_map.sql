-- filter_keywordsとtag_referenceの多対多対応表（PMOレビュー対応: フィルタ語彙の
-- カバレッジ正本化）
--
-- 背景: article_filter.pyの軽量キーワードフィルタ(filter_keywords)はtag_referenceと
-- 完全に独立して人手管理されており、2026年7月に「ジェンダー」タグ（tag_referenceには
-- あるがfilter_keywordsには無い）という捕捉漏れが実際に発生した。
-- このテーブルは「どのキーワードがどのタグの捕捉に対応するか」の対応関係を持ち、
-- check_filter_keyword_coverage.pyがtag_reference.filter_coverage_policy='required'の
-- タグに対して有効なキーワードが紐付いているかを機械的に検証できるようにする。
--
-- 1つのキーワードが複数タグの捕捉に寄与しうるため、単一tag_id列ではなく多対多構造にする
-- （例: 「ジェンダー」キーワードはTH-08-05[ジェンダー]とTH-08-04[DEI]の両方に寄与しうる）。
--
-- RLSとDB権限の前提: 本テーブルはRLSを有効化するが、本リポジトリの既存バッチ
-- （article_crawler.SupabaseClient）はconfig.jsonのsupabase.key（service_role鍵）を
-- 使用しており、service_roleはRLSを常にバイパスする（external_intelligence_calls等、
-- 既存のRLS有効化済みテーブルも明示的なSELECT policyを追加せず正常に読み書きできている
-- のと同じ前提）。したがって本テーブルにも追加のSELECT policyは設けていない。
--
-- 実行前提: このファイルはユーザーがSupabase側で手動実行する。

create table public.filter_keyword_tag_map (
    id          uuid primary key default gen_random_uuid(),
    keyword_id  uuid not null references public.filter_keywords(keyword_id) on delete cascade,
    tag_id      text not null references public.tag_reference(tag_id),
    created_at  timestamptz not null default now(),

    unique (keyword_id, tag_id)
);

create index filter_keyword_tag_map_tag_id_idx on public.filter_keyword_tag_map(tag_id);

alter table public.filter_keyword_tag_map enable row level security;
