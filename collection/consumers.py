"""クロール先(Source)を「どのアプリが使うか」の扱い（共通クロール部分）。

crawl_targets.used_by（text[]）に、使うアプリ名を入れる。クロールは全Source共通で1回、記事は1本だけ保存し、
各アプリは自分の名前を含むSourceの記事だけを使う。

- アプリを増やすときは KNOWN_APPS に名前を足すだけ（DBの列・制約の変更は不要）
- used_by 列が無い（SQL未適用）/値が空の行は、DEFAULT_APPS（サステナ）として扱う。
  → SQL適用の前後どちらでコードをデプロイしても、従来どおり動く
"""
import requests

KNOWN_APPS = ("sustainability", "erm", "geo")
DEFAULT_APPS = ("sustainability",)


def used_by(target: dict) -> set:
    """Source(crawl_targets の行)を使うアプリ名の集合。列が無い/空なら DEFAULT_APPS。"""
    value = (target or {}).get("used_by")
    if not value:
        return set(DEFAULT_APPS)
    return set(value)


def is_used_by(target: dict, app: str) -> bool:
    return app in used_by(target)


def excluded_target_ids(client, app: str) -> set:
    """このアプリでは使わない(used_by に app を含まない)Source の crawl_target_id の集合。
    articles を直接読んで集計する処理が、他アプリ専用の記事を除くために使う。
    used_by 列が無い(SQL未適用)/値が空の行は app 用として扱うので、その場合は空集合（従来どおり全件）。"""
    rows = select_targets(client, "crawl_target_id")
    return {r["crawl_target_id"] for r in rows if not is_used_by(r, app)}


def select_targets(client, columns: str, params: dict = None) -> list:
    """crawl_targets を取得する。columns に used_by を足して取得し、列が無い(SQL未適用)場合は
    used_by 抜きで取り直す（その場合、各行は DEFAULT_APPS として扱われる）。"""
    base = {**(params or {})}
    try:
        return client.select("crawl_targets", {**base, "select": f"{columns},used_by"})
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status not in (400, 404):      # 列が無いとき PostgREST は 400。それ以外の障害は隠さない
            raise
        return client.select("crawl_targets", {**base, "select": columns})
