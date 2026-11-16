-- crawl_targets に「どのアプリがこのクロール先(Source)を使うか」を示す used_by 列を追加する。
-- 作成日: 2026-11-16
--
-- 背景:
--   外部情報収集(クロール)を、サステナ・ERM・geo などで共通化する。クロールは全体で1回、記事は1本だけ保存し、
--   各アプリは自分の用途のSourceの記事だけを使う。Sourceは重なる(複数アプリで使う)ものが多いため、
--   単一の値ではなく「アプリ名の配列」で持つ。アプリが増えても、列は増やさず値を足すだけにする。
--
-- 値の例: {sustainability} / {sustainability,erm} / {erm,geo}
--   既知のアプリ名は collection/consumers.py の KNOWN_APPS が正本（CHECK制約にしない＝新アプリ追加でSQL変更が不要）。
--
-- 既存の全Sourceは {sustainability} とする（現状はサステナだけが使っているため。挙動は変わらない）。
-- 冪等（再実行しても安全）。既存データの意味は変えない。
-- ※ 適用順序: このSQLを適用してから、used_by を参照するコードをデプロイしても、先にデプロイしても動く
--   （コード側は列が無い場合に従来どおり動く）。

alter table public.crawl_targets
    add column if not exists used_by text[] not null default '{sustainability}';

-- 空配列は「どのアプリも使わない」になり、意図しない取りこぼしの元になるため禁止する
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'crawl_targets_used_by_not_empty'
          and conrelid = 'public.crawl_targets'::regclass
    ) then
        alter table public.crawl_targets
            add constraint crawl_targets_used_by_not_empty check (cardinality(used_by) >= 1);
    end if;
end $$;

create index if not exists crawl_targets_used_by_idx on public.crawl_targets using gin (used_by);

comment on column public.crawl_targets.used_by is
    'このクロール先を使うアプリ名の配列(例: {sustainability,erm})。クロール自体は全Source共通で1回だけ行い、'
    '各アプリは自分の名前を含むSourceの記事だけを使う。既知のアプリ名は collection/consumers.py を参照';
