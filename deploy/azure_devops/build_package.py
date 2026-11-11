"""Azure App Service（Python）へデプロイする成果物を組み立てる（サステナアプリ用・1サーバー構成）。

使い方（プロジェクトのルートで）:
    1. 画面を静的ビルド:  cd eco-digest-spark && WMD_STATIC_BUILD=1 npm run build
    2. python deploy/azure_devops/build_package.py          # → build/azure_package/ に出力
       python deploy/azure_devops/build_package.py --no-frontend   # 画面なし(APIだけ)

含めるもの（実行時に必要なもののみ）:
    ルート直下の *.py（api_server.py 等）、requirements_azure.txt（requirements.txt として）、
    neural_check_rules.json、knowledge/（専門家AIの知識。zipは除く）、
    App_Data/（日次クロール・分析などの定期ジョブ=WebJob）、frontend/（画面の静的ファイル）

含めないもの（秘密情報・巨大・開発用）:
    config.json / .env（秘密情報。Azureでは「環境変数」で渡す）、tests/、sql/、aws/、
    eco-digest-spark/（ソースとnode_modules）、docs/、*.md、*.zip など
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build" / "azure_package"
FRONTEND_DIST = ROOT / "eco-digest-spark" / "dist" / "client"

INCLUDE_DIRS = ["knowledge", "App_Data"]
INCLUDE_FILES = ["neural_check_rules.json"]
FORBIDDEN_NAMES = {"config.json", ".env"}          # 成果物に入ってはいけない（秘密情報）
_SKIP_PARTS = {"__pycache__", ".pytest_cache"}


def _ignore(_dir, names):
    return [n for n in names if n in _SKIP_PARTS or n.endswith((".pyc", ".zip")) or n in FORBIDDEN_NAMES]


def build(out: Path = OUT, frontend_dist: Path = FRONTEND_DIST, with_frontend: bool = True, root: Path = ROOT) -> Path:
    if with_frontend and not (frontend_dist / "_shell.html").is_file():
        sys.exit(f"画面の静的ビルドが見つかりません: {frontend_dist}\n"
                 "先に `cd eco-digest-spark && WMD_STATIC_BUILD=1 npm run build` を実行してください。")
    if out.exists():
        shutil.rmtree(out)           # 成果物用のディレクトリ（build/azure_package）だけを作り直す
    out.mkdir(parents=True)
    for p in sorted(root.glob("*.py")):
        shutil.copy2(p, out / p.name)
    # Azure(Oryx)がPythonアプリと判定するため requirements.txt の名前で置く（中身は requirements_azure.txt）
    shutil.copy2(root / "requirements_azure.txt", out / "requirements.txt")
    for name in INCLUDE_FILES:
        shutil.copy2(root / name, out / name)
    for d in INCLUDE_DIRS:
        shutil.copytree(root / d, out / d, ignore=_ignore)
    if with_frontend:
        shutil.copytree(frontend_dist, out / "frontend")
    leaked = [p for p in out.rglob("*") if p.name in FORBIDDEN_NAMES]
    if leaked:
        sys.exit(f"秘密情報のファイルが成果物に入っています: {leaked}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-frontend", action="store_true", help="画面を含めない（APIだけ）")
    args = ap.parse_args()
    out = build(with_frontend=not args.no_frontend)
    n = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"成果物を作りました: {out}（{n}ファイル）")


if __name__ == "__main__":
    main()
