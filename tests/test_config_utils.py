import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config_utils as cu  # noqa: E402

_ENV_KEYS = (
    "AZURE_OPENAI_API_KEY", "SUPABASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "ZYTE_API_KEY", "TINYFISH_API_KEY",
)


def _clear_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_tinyfish_api_key_read_from_env(monkeypatch):
    """2026-10-09の回帰テスト: TINYFISH_API_KEYがconfig["api_keys"]["tinyfish"]へ
    正しく反映されること(ZYTE_API_KEYと同じ抜けが再発していないことの確認)"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "dummy-azure-key")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy-tinyfish-key")

    config = cu.build_config_from_env()

    assert config["api_keys"]["tinyfish"] == "dummy-tinyfish-key"


def test_tinyfish_api_key_defaults_to_empty_string_when_unset(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "dummy-azure-key")

    config = cu.build_config_from_env()

    assert config["api_keys"]["tinyfish"] == ""


def test_build_config_from_env_populates_all_api_keys(monkeypatch):
    """既存のopenai/anthropic/zyte、新規tinyfishが同じ並びで一貫して読まれること"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "dummy-azure-key")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-key")
    monkeypatch.setenv("ZYTE_API_KEY", "dummy-zyte-key")
    monkeypatch.setenv("TINYFISH_API_KEY", "dummy-tinyfish-key")

    config = cu.build_config_from_env()

    assert config["api_keys"]["openai"] == "dummy-openai-key"
    assert config["api_keys"]["anthropic"] == "dummy-anthropic-key"
    assert config["api_keys"]["tinyfish"] == "dummy-tinyfish-key"
    assert config["zyte"]["api_key"] == "dummy-zyte-key"


def test_build_config_from_env_returns_empty_dict_when_no_relevant_env_vars(monkeypatch):
    """ローカル開発保護の既存挙動の非回帰: 関連する環境変数が一切無ければ空のdictを返すこと
    (docker_entrypoint._sync_config_file()がこれを見て既存config.jsonを上書きしない)"""
    _clear_env(monkeypatch)

    config = cu.build_config_from_env()

    assert config == {}
