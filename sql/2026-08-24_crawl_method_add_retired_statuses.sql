-- crawl_targets.crawl_method に「重複廃止」「廃止」を追加する
-- 「手動」は本来「人が定期的に手作業で確認する」という意味のはずだったが、実態調査の結果、
-- 22件全てが (a) 同一内容を別の稼働中クロール対象が既にカバーしている重複、または
-- (b) 一過性イベント終了により今後更新が見込めない廃止ページ、のいずれかであり、
-- 実際に人手で確認する運用は一件も存在しなかった。誤解を招く「手動」のまま残すのではなく、
-- 理由を明示する値に置き換える。

-- 手順1: 既存22件が「手動」のままなので、まずは新旧両方の値を許可する制約に緩める
alter table public.crawl_targets
    drop constraint crawl_targets_crawl_method_check;
alter table public.crawl_targets
    add constraint crawl_targets_crawl_method_check
    check (crawl_method in ('HTML', 'RSS', 'API', 'ブラウザ操作', 'メール', '手動', '重複廃止', '廃止'));

-- 手順2: このSQL実行後、.scratch_fix_manual_targets.py を実行して
-- 22件を「重複廃止」「廃止」へ更新する（このSQLの中では行わない）

-- 手順3: 22件の更新完了・手動が0件になったことを確認した後、
-- 「手動」を許可リストから外す（このSQLの中では行わない。後日別途実行）
-- alter table public.crawl_targets
--     drop constraint crawl_targets_crawl_method_check;
-- alter table public.crawl_targets
--     add constraint crawl_targets_crawl_method_check
--     check (crawl_method in ('HTML', 'RSS', 'API', 'ブラウザ操作', 'メール', '重複廃止', '廃止'));

comment on column public.crawl_targets.crawl_method is
    '取得方式。RSS/HTML/ブラウザ操作は自動クロール対象（article_crawler.py）、APIは個別スクリプト'
    '（api_article_crawler.py／reference_list_monitor.py等）で対応、メールは未実装、'
    '重複廃止＝同一内容を別の稼働中対象がカバー済みのため除外、廃止＝一過性ページ等で今後更新'
    '見込みがないため除外（いずれも自動クロール対象外）。'
    '「手動」は過去の値で現在は使用しない（移行完了後にCHECK制約から削除予定）';
