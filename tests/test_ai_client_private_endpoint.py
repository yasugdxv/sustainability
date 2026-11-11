"""ai_client.make_openai_client: 社内IP(Private Endpoint)宛てのAzure接続は、プロキシを通さないこと。"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_client  # noqa: E402

CONFIG = {
    "proxy": {"enabled": True, "http_proxy": "http://proxy.example:9400"},
    "ssl": {"verify": False},
    "azure": {"endpoint": "https://foundry.example.services.ai.azure.com/openai/v1/", "api_key": "dummy",
              "deployment": "dep-x", "api_version": "2025-04-01-preview"},
}


def _has_proxy(client) -> bool:
    return any(m is not None for m in client._client._mounts.values())  # httpxのプロキシ設定の有無


def test_private_host_bypasses_proxy(monkeypatch):
    monkeypatch.setattr(ai_client.socket, "gethostbyname", lambda h: "10.143.84.213")
    client, model = ai_client.make_openai_client(CONFIG)
    assert model == "dep-x"
    assert not _has_proxy(client)
    assert str(client.base_url).startswith("https://foundry.example.services.ai.azure.com/openai/")  # パス付きでも丸められる


def test_public_host_keeps_proxy(monkeypatch):
    monkeypatch.setattr(ai_client.socket, "gethostbyname", lambda h: "20.1.2.3")
    client, _ = ai_client.make_openai_client(CONFIG)
    assert _has_proxy(client)


def test_unresolvable_host_keeps_proxy(monkeypatch):
    def boom(h):
        raise OSError("no dns")
    monkeypatch.setattr(ai_client.socket, "gethostbyname", boom)
    client, _ = ai_client.make_openai_client(CONFIG)
    assert _has_proxy(client)
