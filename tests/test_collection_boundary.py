"""共通クロール部分の境界を守るテスト。

collection/ の中のモジュール(collection_boundary.COMMON_MODULES)が、サステナ独自のモジュールや
ルート直下の入口を読み込んでいないことを、ソースコードを解析して確認する。違反すると失敗する。
"""
import ast
import subprocess
import sys
from pathlib import Path

import collection_boundary as cb

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "collection"
ROOT_MODULES = {p.stem for p in ROOT.glob("*.py")}


def _imported_roots(path: Path) -> set:
    """ファイル内のすべての import(関数の中も含む)の、先頭のモジュール名を集める。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_common_modules_live_in_collection_package():
    missing = [m for m in cb.COMMON_MODULES if not (PKG / f"{m}.py").is_file()]
    assert not missing, f"一覧にあるが collection/ に存在しないモジュール: {missing}"
    on_disk = {p.stem for p in PKG.glob("*.py")} - {"__init__"}
    assert not (on_disk - set(cb.COMMON_MODULES)), \
        f"collection/ にあるが一覧に無いモジュール: {sorted(on_disk - set(cb.COMMON_MODULES))}"


def test_common_modules_do_not_import_sustainability_specific_modules():
    allowed = set(cb.HOST_PROVIDED) | {"collection"}
    violations = {}
    for m in cb.COMMON_MODULES:
        # ルート直下のモジュール名と一致する import だけを見る（標準ライブラリ・外部パッケージは対象外）
        bad = (_imported_roots(PKG / f"{m}.py") & ROOT_MODULES) - allowed
        if bad:
            violations[m] = sorted(bad)
    assert not violations, (
        "共通クロール部分が、サステナ独自のモジュール/ルートの入口を読み込んでいます"
        f"(切り出せなくなります。collection 内は `from collection.xxx import` で書く): {violations}"
    )


def test_root_entry_points_return_the_same_module_object():
    """ルートの入口は、実体と同じモジュールを返す（テストの差し替え・非公開関数の参照が従来どおり動く）。"""
    import importlib
    for m in cb.COMMON_MODULES:
        assert importlib.import_module(m) is importlib.import_module(f"collection.{m}")


def test_republished_names_for_backward_compatibility():
    import article_crawler
    import supabase_client
    import sustainability_expert_common as common
    assert article_crawler.SupabaseClient is supabase_client.SupabaseClient
    assert common._select_in_chunks is supabase_client._select_in_chunks


def test_cache_dir_stays_at_app_root():
    import article_crawler
    import config_utils
    assert article_crawler.CACHE_DIR == config_utils.BASE / "cache"


def test_direct_execution_of_root_entry_points_still_works():
    """`python xxx.py` が実体まで届くか。副作用のない引数(不正な値/--help)で、実体の __main__ に入ることを確認する。"""
    cases = [
        (["article_crawler.py", "abc"], "invalid literal"),                 # int("abc") で止まる = 実体の __main__ に到達
        (["crawl_health_check.py", "--help"], "lookback-days"),            # argparse の説明
        (["reference_list_monitor.py", "NO_SUCH_SOURCE"], "未知のソース指定"),   # 不明なソース名は何もせず終了
        (["api_article_crawler.py", "no_such_source"], "未知のソース指定"),
    ]
    for argv, expect in cases:
        r = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        out = r.stdout + r.stderr
        assert "ModuleNotFoundError" not in out and "ImportError" not in out, (argv, out[-500:])
        assert expect in out, (argv, out[-500:])
