"""クロール先(Source)の used_by（どのアプリが使うか）の確認・変更、新しいSourceの追加。

既定は「確認のみ」で、何も書き込まない。実際に書き込むのは --apply を付けたときだけ。

使い方:
    python manage_sources.py show                                   # used_by の組み合わせ別の件数
    python manage_sources.py find --domains a.com,b.org            # 条件に合うSourceを表示（重なりの確認用）
    python manage_sources.py find --urls-file urls.txt             # URLの一覧(1行1件)で探す
    python manage_sources.py add-app erm --domains a.com           # 確認のみ: erm を used_by に足す計画を表示
    python manage_sources.py add-app erm --domains a.com --apply   # 実際に書き込む
    python manage_sources.py remove-app erm --ids T1,T2 --apply
    python manage_sources.py add-targets new_sources.json          # 確認のみ: 新しいSource(JSON配列)の検査
    python manage_sources.py add-targets new_sources.json --apply  # 実際に追加

探す条件(find / add-app / remove-app): --ids / --domains / --urls-file のどれか(複数可)。
新しいSource(add-targets)は、used_by の指定が必須。既に登録済みのURLは追加せず、
「既存に used_by を足す(add-app)」ように案内する。
"""
import argparse
import json
import sys
from pathlib import Path

from collection import source_admin as sa
from collection.consumers import select_targets
from collection.supabase_client import SupabaseClient
from config_utils import load_config

COLUMNS = "crawl_target_id,target_name,target_url,domain,publisher_name"


def _csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _read_lines(path):
    return [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def _add_selectors(p):
    p.add_argument("--ids", help="crawl_target_id をカンマ区切りで")
    p.add_argument("--domains", help="ドメインをカンマ区切りで")
    p.add_argument("--urls-file", help="URLの一覧ファイル(1行1件)")


def _find(client, args):
    targets = select_targets(client, COLUMNS)
    urls = _read_lines(args.urls_file) if getattr(args, "urls_file", None) else []
    return targets, sa.find_targets(targets, ids=_csv(args.ids), domains=_csv(args.domains), urls=urls)


def _print_not_found(nf):
    for kind, items in nf.items():
        if items:
            print(f"  見つからなかった({kind}): {len(items)}件 例: {items[:5]}")


def cmd_show(client, args):
    for k, v in sa.summarize_used_by(select_targets(client, "crawl_target_id")).items():
        print(f"  {k}: {v}件")


def cmd_find(client, args):
    _, res = _find(client, args)
    for t in res["matched"]:
        print(f"  {t['crawl_target_id']}  {sorted(t.get('used_by') or ['sustainability'])}  {t.get('target_url')}")
    print(f"一致: {len(res['matched'])}件")
    _print_not_found(res["not_found"])


def cmd_change_app(client, args, add):
    app = args.app
    _, res = _find(client, args)
    if not res["matched"] and not any(res["not_found"].values()):
        sys.exit("条件(--ids / --domains / --urls-file)を指定してください")
    plan = sa.plan_used_by_change(res["matched"], add=[app] if add else [], remove=[] if add else [app])
    print(f"対象: {len(res['matched'])}件 / 変更あり: {len(plan)}件（{'追加' if add else '削除'}: {app}）")
    for p in plan[:20]:
        print(f"  {p['crawl_target_id']}: {p['before']} -> {p['after']}")
    if len(plan) > 20:
        print(f"  ...他 {len(plan) - 20}件")
    _print_not_found(res["not_found"])
    if not args.apply:
        print("[確認のみ] 何も書き込んでいません。実行するには --apply を付けてください。")
        return
    for p in plan:
        client.update("crawl_targets", {"crawl_target_id": f"eq.{p['crawl_target_id']}"}, {"used_by": p["after"]})
    print(f"書き込み完了: {len(plan)}件")


def cmd_add_targets(client, args):
    new_rows = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(new_rows, list):
        sys.exit("JSONは、Sourceの配列にしてください")
    res = sa.validate_new_targets(new_rows, select_targets(client, "crawl_target_id,target_url"))
    print(f"追加できる: {len(res['to_insert'])}件 / 既に登録済み: {len(res['already_exists'])}件 / エラー: {len(res['errors'])}件")
    for e in res["errors"]:
        print("  エラー:", e)
    for x in res["already_exists"]:
        apps = sorted(x["new"]["used_by"])
        print(f"  登録済み: {x['new']['target_url']} -> add-app {' '.join(apps)} --ids {x['existing']['crawl_target_id']} で used_by を足してください")
    if res["errors"]:
        sys.exit("エラーがあるため、追加しません。直してから、もう一度実行してください。")
    if not args.apply:
        print("[確認のみ] 何も書き込んでいません。実行するには --apply を付けてください。")
        return
    if res["to_insert"]:
        client.insert("crawl_targets", res["to_insert"], prefer="return=minimal")
    print(f"追加完了: {len(res['to_insert'])}件")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    p = sub.add_parser("find"); _add_selectors(p)
    for name in ("add-app", "remove-app"):
        p = sub.add_parser(name); p.add_argument("app"); _add_selectors(p); p.add_argument("--apply", action="store_true")
    p = sub.add_parser("add-targets"); p.add_argument("file"); p.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    try:
        client = SupabaseClient(load_config())
        {"show": cmd_show, "find": cmd_find, "add-targets": cmd_add_targets}.get(
            args.cmd, lambda c, a: cmd_change_app(c, a, add=(args.cmd == "add-app")))(client, args)
    except sa.SourceAdminError as e:
        sys.exit(f"入力エラー: {e}")


if __name__ == "__main__":
    main()
