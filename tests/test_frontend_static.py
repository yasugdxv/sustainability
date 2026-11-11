"""画面の静的配信（frontend_static.py）のテスト。
APIを画面に置き換えないこと・SPAのフォールバック・配信元の外へ出ないことを確認する。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import frontend_static  # noqa: E402


def _make_app(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "_shell.html").write_text("<html>shell</html>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("secret", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    assert frontend_static.mount_frontend(app, tmp_path) is True
    return TestClient(app)


def test_api_is_not_replaced_by_frontend(tmp_path):
    client = _make_app(tmp_path)
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/nothing").status_code == 404  # 未定義APIは画面ではなく404


def test_spa_fallback_and_assets(tmp_path):
    client = _make_app(tmp_path)
    assert "shell" in client.get("/").text
    assert "shell" in client.get("/competitors/overview").text  # 画面のURLを直接開いても初期HTMLを返す
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/assets/missing.js").status_code == 404  # 無い.jsにHTMLを返さない


def test_cannot_escape_dist_dir(tmp_path):
    client = _make_app(tmp_path)
    res = client.get("/..%2fsecret.txt")
    assert "secret" not in res.text


def test_no_dist_means_no_mount(tmp_path, monkeypatch):
    # 実際のビルド結果(./eco-digest-spark/dist/client)があっても影響されないよう、探す基準の場所を空のフォルダへ向ける
    monkeypatch.setattr(frontend_static, "__file__", str(tmp_path / "x.py"))
    monkeypatch.delenv("WMD_FRONTEND_DIST", raising=False)
    app = FastAPI()
    assert frontend_static.mount_frontend(app, tmp_path / "none") is False
