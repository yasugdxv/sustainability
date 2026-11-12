import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from docker_entrypoint import _sync_config_file  # noqa: E402


def test_non_empty_env_config_overwrites_existing_file(tmp_path):
    """2026-10-08の不具合そのものの回帰テスト: 環境変数由来のconfigが非空なら、
    既存のconfig.json(古い/不完全な内容)があっても常に上書きされること"""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"zyte": {"api_key": ""}}), encoding="utf-8")

    message = _sync_config_file({"zyte": {"api_key": "new-key-from-env"}}, config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == {"zyte": {"api_key": "new-key-from-env"}}
    assert "上書き" in message


def test_non_empty_env_config_writes_when_no_existing_file(tmp_path):
    config_path = tmp_path / "config.json"

    message = _sync_config_file({"azure": {"api_key": "x"}}, config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == {"azure": {"api_key": "x"}}
    assert "生成しました" in message


def test_empty_env_config_without_existing_file_writes_empty_config(tmp_path):
    """従来通りの挙動: 環境変数が無く、既存ファイルも無い場合は空の設定を書き込む"""
    config_path = tmp_path / "config.json"

    message = _sync_config_file({}, config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == {}
    assert "空の設定" in message


def test_empty_env_config_preserves_existing_manual_config(tmp_path):
    """ローカル開発保護の回帰テスト: 環境変数が無い場合、手動で用意した既存の
    config.jsonは一切書き換えられないこと"""
    config_path = tmp_path / "config.json"
    manual_config = {"azure": {"api_key": "manually-entered-local-dev-key"}}
    config_path.write_text(json.dumps(manual_config), encoding="utf-8")

    message = _sync_config_file({}, config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == manual_config
    assert "既存の config.json を使用します" in message
