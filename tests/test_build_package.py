"""デプロイ用パッケージの組み立て（deploy/azure_devops/build_package.py）のテスト。
実行に必要なものが入り、秘密情報・開発用のものが入らないことを確認する。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "deploy" / "azure_devops"))
import build_package as bp  # noqa: E402


def _fake_root(tmp_path):
    root = tmp_path / "root"
    (root / "knowledge" / "x").mkdir(parents=True)
    (root / "knowledge" / "x" / "a.json").write_text("{}", encoding="utf-8")
    (root / "knowledge" / "big.zip").write_text("zip", encoding="utf-8")
    (root / "App_Data" / "jobs" / "triggered" / "job1").mkdir(parents=True)
    (root / "App_Data" / "jobs" / "triggered" / "job1" / "run.sh").write_text("#!/bin/bash", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "t.py").write_text("", encoding="utf-8")
    (root / "api_server.py").write_text("app = 1", encoding="utf-8")
    (root / "requirements_azure.txt").write_text("fastapi", encoding="utf-8")
    (root / "neural_check_rules.json").write_text("{}", encoding="utf-8")
    (root / "config.json").write_text('{"secret": 1}', encoding="utf-8")
    dist = root / "eco-digest-spark" / "dist" / "client"
    dist.mkdir(parents=True)
    (dist / "_shell.html").write_text("<html></html>", encoding="utf-8")
    return root, dist


def test_package_has_runtime_files_and_no_secrets_or_dev_files(tmp_path):
    root, dist = _fake_root(tmp_path)
    out = bp.build(out=tmp_path / "out", frontend_dist=dist, root=root)
    assert (out / "api_server.py").is_file()
    assert (out / "requirements.txt").read_text(encoding="utf-8") == "fastapi"  # requirements_azure.txt の中身
    assert (out / "neural_check_rules.json").is_file()
    assert (out / "frontend" / "_shell.html").is_file()
    assert (out / "App_Data" / "jobs" / "triggered" / "job1" / "run.sh").is_file()  # 定期ジョブ
    assert (out / "knowledge" / "x" / "a.json").is_file()
    assert not (out / "knowledge" / "big.zip").exists()
    assert not (out / "config.json").exists()      # 秘密情報
    assert not (out / "tests").exists()


def test_missing_frontend_build_stops(tmp_path):
    root, dist = _fake_root(tmp_path)
    (dist / "_shell.html").unlink()
    with pytest.raises(SystemExit):
        bp.build(out=tmp_path / "out", frontend_dist=dist, root=root)


def test_no_frontend_option_does_not_need_build(tmp_path):
    root, dist = _fake_root(tmp_path)
    (dist / "_shell.html").unlink()
    out = bp.build(out=tmp_path / "out", frontend_dist=dist, with_frontend=False, root=root)
    assert not (out / "frontend").exists()
