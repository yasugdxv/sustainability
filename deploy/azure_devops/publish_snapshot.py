"""Azure DevOps に送る「クリーンなスナップショット」を、作業フォルダを変えずに作る。

    python deploy/azure_devops/publish_snapshot.py -m "コミットメッセージ"
        → スナップショットのコミットをローカルに作る（送らない。内容の確認用）
    python deploy/azure_devops/publish_snapshot.py -m "..." --push <リポジトリURL> --branch <ブランチ名>
        → 作ったコミットを、指定ブランチへ送る（送る前に、必ず内容を確認すること）

仕組み（Gitの内部機能を使う）:
    ・対象は、今のブランチの「コミット済み」の内容（HEAD）。未コミットの変更や作業フォルダには触れない。
    ・Azure（DevOps）で使わないもの（aws/、.github/）はスナップショットから外す。
    ・履歴は、スナップショット同士の連なり（ローカルの参照 refs/sus-deploy/main に記録）。
      今までの開発履歴は、DevOps には出ない。
    ・秘密情報らしき文字列・config.json/.env・50MB超のファイルが見つかれば、作成せずに止まる。
      （tests/ の中のダミー値は点検の対象外）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_REF = "refs/sus-deploy/main"
EXCLUDE_PATHS = ["aws", ".github"]
FORBIDDEN_NAMES = {"config.json", ".env"}
MAX_BYTES = 50 * 1024 * 1024
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|sas)[\"' ]*[:=][\"' ]*[A-Za-z0-9+/_\-]{24,}"
    r"|sk-[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}|AccountKey=|-----BEGIN [A-Z ]*PRIVATE KEY"
)
SELF_PATH = "deploy/azure_devops/publish_snapshot.py"
SKIP_SCAN_SUFFIXES = (".png", ".jpg", ".jpeg", ".ico", ".gif", ".gz", ".zip", ".lock")


def _git(*args: str, env: dict | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, env=env, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} に失敗: {r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return r.stdout.decode("utf-8", "replace").strip()


def build_tree() -> str:
    """HEAD のツリーから、除外するものを外したツリーを、一時インデックスで作って、そのIDを返す"""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git("read-tree", "HEAD", env=env)
        for p in EXCLUDE_PATHS:
            _git("rm", "-r", "-q", "--cached", "--ignore-unmatch", p, env=env)
        return _git("write-tree", env=env)


def scan_tree(tree: str) -> list[str]:
    """問題点の一覧を返す（空なら問題なし）"""
    problems = []
    for line in _git("ls-tree", "-r", "-l", tree).splitlines():
        meta, path = line.split("\t", 1)
        size = meta.split()[3]
        size = int(size) if size.isdigit() else 0
        if Path(path).name in FORBIDDEN_NAMES:
            problems.append(f"秘密情報のファイル: {path}")
        if size > MAX_BYTES:
            problems.append(f"50MB超: {path}")
        # このスクリプト自身は、検索用の文字列(AccountKey= 等)を含むため点検しない
        if path.startswith("tests/") or path.lower().endswith(SKIP_SCAN_SUFFIXES) or path == SELF_PATH:
            continue
        text = subprocess.run(["git", "show", f"{tree}:{path}"], cwd=ROOT, capture_output=True).stdout.decode("utf-8", "ignore")
        if SECRET_PATTERN.search(text):
            problems.append(f"秘密情報らしき文字列: {path}")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--message", required=True, help="コミットメッセージ")
    ap.add_argument("--push", metavar="URL", help="送り先のリポジトリURL（指定しなければ送らない）")
    ap.add_argument("--branch", default="main", help="送り先のブランチ名")
    args = ap.parse_args()

    tree = build_tree()
    problems = scan_tree(tree)
    if problems:
        print("点検で問題が見つかったため、スナップショットを作りません:")
        for p in problems:
            print("  -", p)
        sys.exit(1)

    parent = _git("rev-parse", "--verify", "-q", DEPLOY_REF, check=False)
    cmd = ["commit-tree", tree, "-m", args.message]
    if parent:
        cmd += ["-p", parent]
    commit = _git(*cmd)
    _git("update-ref", DEPLOY_REF, commit)
    files = _git("ls-tree", "-r", "--name-only", tree).splitlines()
    print(f"スナップショットを作りました: {commit[:10]}（{len(files)}ファイル、親: {parent[:10] if parent else 'なし(初回)'}）")
    print(f"内容の確認: git ls-tree -r --name-only {commit[:10]}")

    if args.push:
        _git("push", args.push, f"{commit}:refs/heads/{args.branch}")   # 強制pushはしない
        print(f"送りました: {args.push} の {args.branch}")
    else:
        print("（--push を付けていないので、送っていません）")


if __name__ == "__main__":
    main()
