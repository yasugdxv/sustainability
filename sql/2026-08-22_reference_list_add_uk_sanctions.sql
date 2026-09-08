-- 参照リスト差分検知基盤に UK Sanctions List を追加
-- （既存: UN_SC / OFAC_SDN / ECFR_TITLES のCHECK制約に UK_SANCTIONS を加える）

alter table public.reference_list_entries
    drop constraint reference_list_entries_list_source_check;
alter table public.reference_list_entries
    add constraint reference_list_entries_list_source_check
    check (list_source in ('UN_SC', 'OFAC_SDN', 'ECFR_TITLES', 'UK_SANCTIONS'));

alter table public.reference_list_change_events
    drop constraint reference_list_change_events_list_source_check;
alter table public.reference_list_change_events
    add constraint reference_list_change_events_list_source_check
    check (list_source in ('UN_SC', 'OFAC_SDN', 'ECFR_TITLES', 'UK_SANCTIONS'));
