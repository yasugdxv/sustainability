"""
参照リスト（制裁リスト・規則集タイトル等）の差分検知モジュール

通常の記事クロール（articles）とは相性が悪い「エンティティ一覧のスナップショット」系
ソースを対象に、前回取得分との差分（追加・削除・変更）を検知し
reference_list_entries / reference_list_change_events に記録する。

対象ソース:
    UN_SC        UN SC Consolidated List（国連制裁対象者・法人）
    OFAC_SDN     OFAC Sanctions List Service（米国財務省SDNリスト）
    ECFR_TITLES  eCFR titles（米国連邦規則集の各タイトルの最終改正日）

使い方:
    python reference_list_monitor.py          # 全ソースを処理
    python reference_list_monitor.py UN_SC    # 指定ソースのみ処理
"""
import hashlib
import json
import sys
from datetime import datetime, timezone

import requests
from lxml import etree

from article_crawler import SupabaseClient
from config_utils import load_config, make_proxies

REQUEST_TIMEOUT = 60
LIST_SOURCES = ("UN_SC", "OFAC_SDN", "ECFR_TITLES", "UK_SANCTIONS")


def _hash_data(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _text(el, tag: str, namespaces: dict = None) -> str | None:
    child = el.find(tag, namespaces=namespaces)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


# ─── ソース別フェッチ・パース ───────────────────────────────────────
def fetch_un_sc_entries(proxies: dict, verify: bool) -> list:
    """UN SC Consolidated List（INDIVIDUALS + ENTITIES）を取得し、
    [{"entry_key": DATAID, "entry_data": {...}}, ...] を返す"""
    resp = requests.get("https://scsanctions.un.org/resources/xml/en/consolidated.xml",
                         proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = etree.fromstring(resp.content)

    entries = []
    for section, kind in (("INDIVIDUALS", "INDIVIDUAL"), ("ENTITIES", "ENTITY")):
        container = root.find(section)
        if container is None:
            continue
        for el in container.findall(kind):
            data_id = _text(el, "DATAID")
            if not data_id:
                continue
            name_parts = [_text(el, tag) for tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME")]
            entries.append({
                "entry_key": data_id,
                "entry_data": {
                    "kind": kind,
                    "name": " ".join(p for p in name_parts if p),
                    "un_list_type": _text(el, "UN_LIST_TYPE"),
                    "reference_number": _text(el, "REFERENCE_NUMBER"),
                    "listed_on": _text(el, "LISTED_ON"),
                    "last_day_updated": _text(el, "LAST_DAY_UPDATED/VALUE"),
                    "version_num": _text(el, "VERSIONNUM"),
                },
            })
    return entries


def fetch_ofac_sdn_entries(proxies: dict, verify: bool) -> list:
    """OFAC SDN List（sdnEntry）を取得し、[{"entry_key": uid, "entry_data": {...}}, ...] を返す"""
    resp = requests.get(
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML",
        proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = etree.fromstring(resp.content)
    ns = {"sdn": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    prefix = "sdn:" if ns else ""

    entries = []
    for el in root.findall(f".//{prefix}sdnEntry", namespaces=ns or None):
        uid = _text(el, f"{prefix}uid", ns or None)
        if not uid:
            continue
        programs = [p.text for p in el.findall(f"{prefix}programList/{prefix}program", namespaces=ns or None)
                    if p.text]
        entries.append({
            "entry_key": uid,
            "entry_data": {
                "last_name": _text(el, f"{prefix}lastName", ns or None),
                "sdn_type": _text(el, f"{prefix}sdnType", ns or None),
                "programs": programs,
            },
        })
    return entries


def fetch_ecfr_titles(proxies: dict, verify: bool) -> list:
    """eCFR titles（連邦規則集の各タイトルの最終改正日）を取得し、
    [{"entry_key": title番号, "entry_data": {...}}, ...] を返す"""
    resp = requests.get("https://www.ecfr.gov/api/versioner/v1/titles.json",
                         proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    entries = []
    for t in data.get("titles", []):
        if t.get("reserved"):
            continue
        entries.append({
            "entry_key": str(t["number"]),
            "entry_data": {
                "name": t.get("name"),
                "latest_amended_on": t.get("latest_amended_on"),
                "latest_issue_date": t.get("latest_issue_date"),
            },
        })
    return entries


def fetch_uk_sanctions_list_entries(proxies: dict, verify: bool) -> list:
    """UK Sanctions List（Designation）を取得し、
    [{"entry_key": UniqueID, "entry_data": {...}}, ...] を返す"""
    resp = requests.get("https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
                         proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = etree.fromstring(resp.content)

    entries = []
    for el in root.findall("Designation"):
        unique_id = _text(el, "UniqueID")
        if not unique_id:
            continue
        names = [_text(n, "Name6") for n in el.findall("Names/Name")]
        entries.append({
            "entry_key": unique_id,
            "entry_data": {
                "name": next((n for n in names if n), None),
                "individual_entity_ship": _text(el, "IndividualEntityShip"),
                "regime_name": _text(el, "RegimeName"),
                "sanctions_imposed": _text(el, "SanctionsImposed"),
                "date_designated": _text(el, "DateDesignated"),
                "last_updated": _text(el, "LastUpdated"),
                "un_reference_number": _text(el, "UNReferenceNumber"),
            },
        })
    return entries


FETCHERS = {
    "UN_SC": fetch_un_sc_entries,
    "OFAC_SDN": fetch_ofac_sdn_entries,
    "ECFR_TITLES": fetch_ecfr_titles,
    "UK_SANCTIONS": fetch_uk_sanctions_list_entries,
}


# ─── 差分検知・保存（3ソース共通） ────────────────────────────────────
BATCH_SIZE = 500  # 1リクエストあたりの件数（OFAC_SDNが2万件近くあるため、1件ずつのリクエストは避ける）


def _chunks(seq: list, size: int = BATCH_SIZE):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _bulk_set_is_current_false(client: SupabaseClient, entry_ids: list):
    for chunk in _chunks(entry_ids):
        client.update("reference_list_entries", {"entry_id": f"in.({','.join(chunk)})"}, {"is_current": False})


def detect_and_save_changes(client: SupabaseClient, list_source: str, new_entries: list) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    existing = client.select("reference_list_entries", {
        "select": "entry_id,entry_key,entry_data,content_hash,first_seen_at",
        "list_source": f"eq.{list_source}", "is_current": "eq.true",
    })
    existing_by_key = {e["entry_key"]: e for e in existing}
    new_by_key = {e["entry_key"]: e for e in new_entries}

    new_rows, change_events = [], []
    updated_old_ids, removed_old_ids, unchanged_ids = [], [], []
    added = updated = removed = unchanged = 0

    for key, new_entry in new_by_key.items():
        new_hash = _hash_data(new_entry["entry_data"])
        old = existing_by_key.get(key)

        if old is None:
            new_rows.append({
                "list_source": list_source, "entry_key": key,
                "entry_data": new_entry["entry_data"], "content_hash": new_hash,
                "first_seen_at": now_iso, "last_seen_at": now_iso, "is_current": True,
            })
            change_events.append({
                "list_source": list_source, "entry_key": key, "change_type": "added",
                "before_data": None, "after_data": new_entry["entry_data"],
            })
            added += 1
            continue

        if old["content_hash"] == new_hash:
            unchanged_ids.append(old["entry_id"])
            unchanged += 1
            continue

        updated_old_ids.append(old["entry_id"])
        new_rows.append({
            "list_source": list_source, "entry_key": key,
            "entry_data": new_entry["entry_data"], "content_hash": new_hash,
            "first_seen_at": old.get("first_seen_at") or now_iso, "last_seen_at": now_iso, "is_current": True,
        })
        change_events.append({
            "list_source": list_source, "entry_key": key, "change_type": "updated",
            "before_data": old["entry_data"], "after_data": new_entry["entry_data"],
        })
        updated += 1

    for key, old in existing_by_key.items():
        if key in new_by_key:
            continue
        removed_old_ids.append(old["entry_id"])
        change_events.append({
            "list_source": list_source, "entry_key": key, "change_type": "removed",
            "before_data": old["entry_data"], "after_data": None,
        })
        removed += 1

    # 旧レコードをis_current=falseにするのは、新レコードのinsertより先に行う必要がある
    # （list_source, entry_key）に対するUNIQUE partial index（is_current=true）に違反するため
    _bulk_set_is_current_false(client, updated_old_ids + removed_old_ids)

    for chunk in _chunks(unchanged_ids):
        client.update("reference_list_entries", {"entry_id": f"in.({','.join(chunk)})"}, {"last_seen_at": now_iso})

    for chunk in _chunks(new_rows):
        client.insert("reference_list_entries", chunk, prefer="return=minimal")

    for chunk in _chunks(change_events):
        client.insert("reference_list_change_events", chunk, prefer="return=minimal")

    return {"added": added, "updated": updated, "removed": removed, "unchanged": unchanged,
            "total": len(new_entries)}


def main(sources: tuple = LIST_SOURCES):
    config = load_config()
    client = SupabaseClient(config)
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)

    for source in sources:
        print(f"[{source}] 取得中...", flush=True)
        try:
            entries = FETCHERS[source](proxies, verify)
        except Exception as e:
            print(f"[{source}] 取得失敗: {type(e).__name__}: {e}")
            continue
        summary = detect_and_save_changes(client, source, entries)
        print(f"[{source}] 総数{summary['total']}件 "
              f"（新規{summary['added']} 変更{summary['updated']} "
              f"削除{summary['removed']} 変更なし{summary['unchanged']}）")


if __name__ == "__main__":
    args = tuple(a.upper() for a in sys.argv[1:]) or LIST_SOURCES
    unknown = [a for a in args if a not in LIST_SOURCES]
    if unknown:
        print(f"未知のソース指定: {unknown}（対応: {LIST_SOURCES}）")
        sys.exit(1)
    main(args)
