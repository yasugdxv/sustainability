alter table public.crawl_targets
    drop constraint crawl_targets_crawl_method_check;
alter table public.crawl_targets
    add constraint crawl_targets_crawl_method_check
    check (crawl_method in ('HTML', 'RSS', 'API', 'ブラウザ操作', 'Zyte', 'JSON API', 'メール', '手動', '重複廃止', '廃止'));

comment on column public.crawl_targets.crawl_method is
    '取得方式。RSS/HTML/ブラウザ操作/Zyte/JSON APIは自動クロール対象（article_crawler.py）、'
    'APIは個別スクリプト（api_article_crawler.py／reference_list_monitor.py等）で対応、'
    'JSON APIは記事一覧をJSON配列で返す専用エンドポイント（例: corporate.mcdonalds.comの'
    '/bin/getCategory）向け、'
    'メールは未実装、重複廃止＝同一内容を別の稼働中対象がカバー済みのため除外、'
    '廃止＝一過性ページ等で今後更新見込みがないため除外（いずれも自動クロール対象外）。'
    '「手動」は過去の値で現在は使用しない（移行完了後にCHECK制約から削除予定）';
