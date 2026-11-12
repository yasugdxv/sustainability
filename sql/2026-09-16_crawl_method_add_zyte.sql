-- crawl_targets.crawl_method に 'Zyte' を追加する
--
-- 背景: ニューラル週次メールとの整合検証（2026-09-15〜16）で、要対応(手動)116件のうち
-- 54件がBot対策(403/401/406等)によるものと判明。scripts/zyte_poc/でZyte APIの
-- 有効性を検証した結果、58件中49件（84%）が復旧可能と確認できたため、
-- article_crawler.py本体にZyte APIをcrawl_method='Zyte'として組み込んだ
-- （list_html_candidates/extract_articleがuse_zyte引数でZyte経由の取得に対応）。

alter table public.crawl_targets
    drop constraint crawl_targets_crawl_method_check;
alter table public.crawl_targets
    add constraint crawl_targets_crawl_method_check
    check (crawl_method in ('HTML', 'RSS', 'API', 'ブラウザ操作', 'Zyte', 'メール', '手動', '重複廃止', '廃止'));

comment on column public.crawl_targets.crawl_method is
    '取得方式。RSS/HTML/ブラウザ操作/Zyteは自動クロール対象（article_crawler.py）、'
    'APIは個別スクリプト（api_article_crawler.py／reference_list_monitor.py等）で対応、'
    'メールは未実装、重複廃止＝同一内容を別の稼働中対象がカバー済みのため除外、'
    '廃止＝一過性ページ等で今後更新見込みがないため除外（いずれも自動クロール対象外）。'
    '「手動」は過去の値で現在は使用しない（移行完了後にCHECK制約から削除予定）';
