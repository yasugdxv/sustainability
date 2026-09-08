-- tag_reference に filter_coverage_policy 列を追加（PMOレビュー対応: フィルタ語彙の
-- カバレッジ正本化）
--
-- 3状態で管理する理由: boolean(default false)にすると、新規タグ追加時に設定を
-- 忘れた場合「カバレッジ不要」と自動判定されてしまい、「タグは存在するがfilter設定が
-- ない」という2026年7月の事故が「タグは存在するがcoverage要否が未設定」という形で
-- そのまま再発する。そのためtextで required/exempt/unreviewed の3状態を持たせ、
-- 新規タグのdefaultは「要判断」を意味するunreviewedとする（fail-openにしない）。
--
-- check_filter_keyword_coverage.py は本列だけをスコープの正本として参照する
-- （sustainability_article_selector.py側のPython定数[theme_major_ids等]は参照しない）。
--
-- 実行前提: このファイルはユーザーがSupabase側で手動実行する。
-- 2026-08-28_filter_keyword_tag_map.sql の後に実行すること。

alter table public.tag_reference
    add column filter_coverage_policy text not null default 'unreviewed'
    check (filter_coverage_policy in ('required', 'exempt', 'unreviewed'));

-- 初期値投入: テーマ大分類9件 + 横断CR-01(情報開示)・CR-02(ESG評価・サステナブル
-- ファイナンス) + TH-08-05(ジェンダー、小分類)を明示的にrequiredにする。
-- TH-08-05を個別にrequiredとするのが本修正の核心（「親テーマ[人的資本]に
-- キーワードがあれば十分」という暗黙の想定を許さないため）。
-- それ以外の既存タグは当面unreviewedのままとし、required/exemptへの棚卸しは
-- PMOの継続的な判断に委ねる。
update public.tag_reference
set filter_coverage_policy = 'required'
where tag_id in (
    'TH-01', 'TH-02', 'TH-03', 'TH-04', 'TH-05', 'TH-06', 'TH-07', 'TH-08', 'TH-09',
    'CR-01', 'CR-02',
    'TH-08-05'
);
