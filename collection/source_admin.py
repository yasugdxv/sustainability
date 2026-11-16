"""クロール先(Source)の used_by を、安全に追加・変更するための検査と計画（共通クロール部分）。

書き込みはしない。「何をどう変えるか」の計画(plan)を作り、実際の書き込みは呼び出し側(manage_sources.py)が
--apply のときだけ行う。ここでは次を保証する:
  - アプリ名は KNOWN_APPS のものだけ（タイプミスで、どのアプリにも使われないSourceができるのを防ぐ）
  - used_by が空にならない
  - 新しいSourceは used_by の指定が必須（指定を忘れて、サステナの分析対象になるのを防ぐ）
  - 既に登録済みのURLを、重複して登録しない（既存Sourceに used_by を足す形にする）
"""
from urllib.parse import urlparse

from collection import consumers

# crawl_targets で NOT NULL の列（実DBの全行で値がある列）。空文字・未指定は不可
REQUIRED_NEW_TARGET_KEYS = ("crawl_target_id", "target_name", "target_url", "domain", "publisher_name", "publisher_tag_id",
                            "endpoint_type", "crawl_method", "change_detection_method", "used_by")
# NOT NULL だが False / 0 が正しい値になりうる列（未指定(None)だけを不可にする）
REQUIRED_NOT_NULL_KEYS = ("crawl_detail_pages", "authentication_required", "lookback_days")
# crawl_targets_crawl_method_check と同じ値（sql/2026-09-16b_crawl_method_add_json_api.sql）。
# 新しいSourceは稼働させる前提なので、廃止系（重複廃止・廃止）は含めない
VALID_CRAWL_METHODS = ("HTML", "RSS", "API", "ブラウザ操作", "Zyte", "JSON API", "メール", "手動")


class SourceAdminError(ValueError):
    """入力が不正なとき（利用者に直してもらう内容）。"""


def normalize_apps(apps) -> list:
    """アプリ名の一覧を検査して、重複を除いた(順序は保つ)リストを返す。未知の名前はエラー。"""
    if isinstance(apps, str):
        apps = [apps]
    out = []
    for a in apps or []:
        name = str(a)
        if name != name.strip() or name != name.lower():
            raise SourceAdminError(f"アプリ名は小文字で、前後に空白なしで指定してください: {name!r}")
        if name not in consumers.KNOWN_APPS:
            raise SourceAdminError(f"未知のアプリ名: {name!r}（既知: {', '.join(consumers.KNOWN_APPS)}）")
        if name not in out:
            out.append(name)
    return out


def normalize_url_key(url: str) -> str:
    """URLの比較用キー（スキーム・www・末尾スラッシュ・#以降を無視）。"""
    p = urlparse((url or "").strip())
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def find_targets(targets: list, *, ids=(), domains=(), urls=()) -> dict:
    """条件に合うSourceを探す。戻り値: {"matched": [行...], "not_found": {"ids": [...], "domains": [...], "urls": [...]}}。
    domains は、domain 列が一致（www 無視）するもの全て。urls は、正規化したURLが一致するもの。"""
    by_id = {t["crawl_target_id"]: t for t in targets}
    by_domain, by_url = {}, {}
    for t in targets:
        by_domain.setdefault((t.get("domain") or "").lower().removeprefix("www."), []).append(t)
        by_url.setdefault(normalize_url_key(t.get("target_url")), []).append(t)
    matched, seen = [], set()
    missing = {"ids": [], "domains": [], "urls": []}

    def take(rows):
        for r in rows:
            if r["crawl_target_id"] not in seen:
                seen.add(r["crawl_target_id"])
                matched.append(r)

    for i in ids:
        (take([by_id[i]]) if i in by_id else missing["ids"].append(i))
    for d in domains:
        key = d.strip().lower().removeprefix("www.")
        (take(by_domain[key]) if key in by_domain else missing["domains"].append(d))
    for u in urls:
        key = normalize_url_key(u)
        (take(by_url[key]) if key in by_url else missing["urls"].append(u))
    return {"matched": matched, "not_found": missing}


def plan_used_by_change(targets: list, *, add=(), remove=()) -> list:
    """targets（crawl_targets の行）の used_by を、add を足し remove を外した結果の計画を返す。
    戻り値: 変更があるものだけの [{"crawl_target_id", "before", "after"}]。空になる変更はエラー。"""
    add, remove = normalize_apps(add), normalize_apps(remove)
    if not add and not remove:
        raise SourceAdminError("追加するアプリか、外すアプリを指定してください")
    if set(add) & set(remove):
        raise SourceAdminError("同じアプリを、追加と削除の両方に指定しています")
    plan = []
    for t in targets:
        before = sorted(consumers.used_by(t))
        after = [a for a in before if a not in remove] + [a for a in add if a not in before]
        if not after:
            raise SourceAdminError(
                f"used_by が空になります（Sourceはどのアプリも使わないことになる）: {t['crawl_target_id']}。"
                "別のアプリを先に追加するか、Source自体を無効化してください")
        if sorted(after) != before:
            plan.append({"crawl_target_id": t["crawl_target_id"], "before": before, "after": sorted(after)})
    return plan


def validate_new_targets(new_rows: list, existing_targets: list, valid_publisher_tags=None) -> dict:
    """新しいSourceの追加の検査。戻り値: {"to_insert": [...], "already_exists": [{"new": 行, "existing": 行}], "errors": [...]}。
    既存のURL(正規化して一致)は挿入せず already_exists に分ける（その場合は used_by の追加で対応する）。
    valid_publisher_tags を渡すと、publisher_tag_id がその集合に実在するかも検査する（tag_reference の tag_id）。"""
    existing_by_url = {normalize_url_key(t.get("target_url")): t for t in existing_targets}
    existing_ids = {t["crawl_target_id"] for t in existing_targets}
    out = {"to_insert": [], "already_exists": [], "errors": []}
    seen_ids, seen_urls = set(), set()
    for i, row in enumerate(new_rows, 1):
        label = row.get("crawl_target_id") or f"{i}行目"
        missing = ([k for k in REQUIRED_NEW_TARGET_KEYS if not row.get(k)]
                   + [k for k in REQUIRED_NOT_NULL_KEYS if row.get(k) is None])
        if missing:
            out["errors"].append(f"{label}: 必須項目がありません: {', '.join(missing)}"
                                 + ("（used_by は必須です。忘れるとサステナの分析対象になります）" if "used_by" in missing else ""))
            continue
        try:
            row = {**row, "used_by": normalize_apps(row["used_by"])}
        except SourceAdminError as e:
            out["errors"].append(f"{label}: {e}")
            continue
        if row["crawl_method"] not in VALID_CRAWL_METHODS:
            out["errors"].append(f"{label}: crawl_method が不正です: {row['crawl_method']!r}")
            continue
        if valid_publisher_tags is not None and row["publisher_tag_id"] not in valid_publisher_tags:
            out["errors"].append(f"{label}: publisher_tag_id が tag_reference に存在しません: {row['publisher_tag_id']!r}")
            continue
        key = normalize_url_key(row["target_url"])
        if key in existing_by_url:
            out["already_exists"].append({"new": row, "existing": existing_by_url[key]})
        elif row["crawl_target_id"] in existing_ids or row["crawl_target_id"] in seen_ids:
            out["errors"].append(f"{label}: crawl_target_id が既に使われています")
        elif key in seen_urls:
            out["errors"].append(f"{label}: 入力の中でURLが重複しています: {row['target_url']}")
        else:
            seen_ids.add(row["crawl_target_id"])
            seen_urls.add(key)
            out["to_insert"].append(row)
    return out


def summarize_used_by(targets: list) -> dict:
    """used_by の組み合わせ別の件数。例: {"sustainability": 600, "erm+sustainability": 3}。"""
    counts = {}
    for t in targets:
        key = "+".join(sorted(consumers.used_by(t)))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
