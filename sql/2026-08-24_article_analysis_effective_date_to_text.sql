-- article_analysis.effective_date を date型からtext型へ変更
-- 記事に明記される施行日は「2027年1月から」「公布日から6か月後」等、厳密な日付として
-- パースできない自由記述であることが多く、date型では空文字列すら受け付けられない
-- （22007: invalid input syntax for type date）。他の詳細サマリー項目
-- （decision_facts/applicability/key_numbers等）と同様の自由記述text型に統一する。
-- 既存データは全件NULLのため、データ移行は不要。

alter table public.article_analysis
    alter column effective_date type text using effective_date::text;
