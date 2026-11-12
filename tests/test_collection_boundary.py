"""共通クロール部分の境界を守るテスト。

共通クロール部分(collection_boundary.COMMON_MODULES)が、サステナ独自のモジュールを
読み込んでいないことを、ソースコードを解析して確認する。違反すると失敗する。
"""
import ast
from pathlib import Path

import collection_boundary as cb

ROOT = Path(__file__).resolve().parents[1]
ALL_MODULES = {p.stem for p in ROOT.glob("*.py")}


def _imported_local_modules(path: Path) -> set:
    """ファイル内のすべての import(関数の中も含む)から、ルート直下の自作モジュール名を集める。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names & ALL_MODULES


def test_common_modules_exist():
    missing = [m for m in cb.COMMON_MODULES if not (ROOT / f"{m}.py").is_file()]
    assert not missing, f"一覧にあるが存在しないモジュール: {missing}"


def test_common_modules_do_not_import_sustainability_specific_modules():
    common = set(cb.COMMON_MODULES)
    violations = {}
    for m in cb.COMMON_MODULES:
        bad = _imported_local_modules(ROOT / f"{m}.py") - common - {m}
        if bad:
            violations[m] = sorted(bad)
    assert not violations, (
        "共通クロール部分が、サステナ独自のモジュールを読み込んでいます"
        f"(切り出せなくなります): {violations}"
    )


def test_supabase_client_is_republished_for_backward_compatibility():
    import article_crawler
    import supabase_client
    assert article_crawler.SupabaseClient is supabase_client.SupabaseClient


def test_select_in_chunks_is_republished_for_backward_compatibility():
    import supabase_client
    import sustainability_expert_common as common
    assert common._select_in_chunks is supabase_client._select_in_chunks
