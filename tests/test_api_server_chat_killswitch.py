# -*- coding: utf-8 -*-
"""/api/chat 緊急Kill Switch (CHAT_ENABLED) の回帰テスト。

2026-10-06のセキュリティインシデント対応で導入。CHAT_ENABLEDは環境変数のみで判定し、
config.json(chat_api.enabled等)は一切参照しない設計のため、config側の値に依存しない
ことも明示的に確認する。各テストで環境変数・monkeypatchは自動的に元へ復元される
(pytestのmonkeypatchフィクスチャの性質上、テスト関数終了時に必ず巻き戻る)。
"""
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import sustainability_expert_common as common  # noqa: E402
import api_server  # noqa: E402


# ─── is_chat_enabled() 単体テスト ──────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    (None, False),       # 未設定
    ("", False),         # 空文字
    ("false", False),
    ("0", False),
    ("no", False),
    ("FALSE", False),
    ("yes please", False),   # 不正値(紛らわしい文字列)
    ("tru", False),          # 不正値(タイポ)
    ("true", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("YES", True),
])
def test_is_chat_enabled_env_var_values(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CHAT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CHAT_ENABLED", value)
    assert common.is_chat_enabled() == expected


def test_is_chat_enabled_ignores_config_json_even_when_true(monkeypatch):
    """config.jsonにchat_api.enabled=trueがあっても、環境変数未設定ならFalse。
    is_chat_enabled()は引数を取らずconfigを一切参照しない設計そのものを確認する。"""
    monkeypatch.delenv("CHAT_ENABLED", raising=False)
    # is_chat_enabled() はconfigを受け取らない。もし将来誤って引数を受け取る
    # シグネチャに変更された場合はこの呼び出し自体がTypeErrorで失敗し検知できる。
    assert common.is_chat_enabled() is False


# ─── /api/chat エンドポイントの振る舞い ─────────────────────────────
@pytest.fixture
def client_with_mocks(monkeypatch):
    """_get_articles / _knowledge_store.search / _azure_client を呼び出し検知用の
    モックに差し替える。CHAT_ENABLED=Falseのとき、これらが一切呼ばれないことを
    確認するため。テスト終了後は元のオブジェクトに自動的に戻る(monkeypatch管理)。"""
    mock_get_articles = MagicMock(side_effect=AssertionError(
        "_get_articles()が呼ばれた: Kill SwitchがOFFの時は到達してはいけない"))
    monkeypatch.setattr(api_server, "_get_articles", mock_get_articles)

    mock_knowledge_store = MagicMock()
    mock_knowledge_store.search.side_effect = AssertionError(
        "knowledge_store.search()が呼ばれた: Kill SwitchがOFFの時は到達してはいけない")
    monkeypatch.setattr(api_server, "_knowledge_store", mock_knowledge_store)

    mock_azure_client = MagicMock()
    mock_azure_client.chat.completions.create.side_effect = AssertionError(
        "Azure/OpenAIクライアントが呼ばれた: Kill SwitchがOFFの時は到達してはいけない")
    monkeypatch.setattr(api_server, "_azure_client", mock_azure_client)

    return TestClient(api_server.app), mock_get_articles, mock_knowledge_store, mock_azure_client


def test_chat_disabled_returns_503_without_side_effects(monkeypatch, client_with_mocks):
    monkeypatch.delenv("CHAT_ENABLED", raising=False)
    client, mock_get_articles, mock_knowledge_store, mock_azure_client = client_with_mocks

    resp = client.post("/api/chat", json={"message": "テスト", "history": [], "themes": [], "lang": "ja"})

    assert resp.status_code == 503
    body = resp.json()
    assert body == {"detail": "Chat機能は一時的に利用できません。"}

    # 内部情報が含まれていないことの確認(例外名・設定キー名・スタックトレース等)
    body_text = resp.text
    for leak_indicator in ["Traceback", "config.json", "AZURE_OPENAI", "api_key",
                           "Exception", "File \"", "line "]:
        assert leak_indicator not in body_text, f"503応答に内部情報の疑いがある文字列が含まれる: {leak_indicator!r}"

    mock_get_articles.assert_not_called()
    mock_knowledge_store.search.assert_not_called()
    mock_azure_client.chat.completions.create.assert_not_called()


def test_chat_disabled_with_false_value_still_blocks(monkeypatch, client_with_mocks):
    monkeypatch.setenv("CHAT_ENABLED", "false")
    client, mock_get_articles, mock_knowledge_store, mock_azure_client = client_with_mocks

    resp = client.post("/api/chat", json={"message": "テスト", "history": [], "themes": [], "lang": "ja"})

    assert resp.status_code == 503
    mock_get_articles.assert_not_called()
    mock_knowledge_store.search.assert_not_called()
    mock_azure_client.chat.completions.create.assert_not_called()


def test_chat_enabled_passes_killswitch_gate(monkeypatch):
    """CHAT_ENABLED=trueの場合、新ゲートは通過すること(その先のcommon.is_enabled等、
    既存ロジックの振る舞いは本テストの対象外。ゲート自体の開閉のみ確認する)。"""
    monkeypatch.setenv("CHAT_ENABLED", "true")
    # 次のゲート(common.is_enabled)で早期returnさせることで、
    # 新ゲートを通過したことだけを切り分けて確認する
    monkeypatch.setattr(common, "is_enabled", lambda config: False)
    client = TestClient(api_server.app)

    resp = client.post("/api/chat", json={"message": "テスト", "history": [], "themes": [], "lang": "ja"})

    # is_enabled()==Falseによる400が返れば、新しいKill Switch自体は通過したことになる
    assert resp.status_code == 400
    assert "サステナAI" in resp.json()["detail"]


# ─── 非回帰: 他エンドポイントはCHAT_ENABLEDの値に影響されない ────────────
@pytest.mark.parametrize("chat_enabled_value", [None, "false", "true"])
def test_other_endpoints_unaffected_by_chat_enabled(monkeypatch, chat_enabled_value):
    if chat_enabled_value is None:
        monkeypatch.delenv("CHAT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CHAT_ENABLED", chat_enabled_value)
    client = TestClient(api_server.app)

    resp = client.get("/api/health")
    assert resp.status_code == 200
