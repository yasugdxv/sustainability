"""画面（eco-digest-spark の静的ビルド）を FastAPI から配信する。

Azure App Service（Python）では、1つのアプリが /api（FastAPI）と画面の両方を同じサイトで配信する。
Nodeのサーバーは使わない。画面は `WMD_STATIC_BUILD=1 npm run build` で作る静的ファイル（SPA）。
（地政学アプリの geo_frontend_static.py と同じ方式）

  - 配信元: 環境変数 WMD_FRONTEND_DIST、無ければ ./frontend、./eco-digest-spark/dist/client の順に探す。
    _shell.html（SPAの初期HTML）が見つからなければ何もしない（API専用として動く。ローカル開発は従来どおり）。
  - /api/... は決して画面に置き換えない（存在しないAPIは404のJSON）。/docs・/openapi.json も妨げない
    （これらは先に登録済みのため、本モジュールの全パス受けより優先される。呼び出しは、全ルート登録の後）。
  - 画面のURL（/competitors など）を直接開いても動くよう、存在しないパスは _shell.html を返す（SPAのフォールバック）。
    ただし、拡張子つきのファイル（.js / .css / .png など）が無いときは404（HTMLを返さない）。
  - 配信できるのは配信元ディレクトリの中のファイルだけ（.. などで外へは出られない）。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

SHELL_NAME = "_shell.html"
_NO_CACHE = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}


def find_dist(explicit: str | os.PathLike | None = None) -> Path | None:
    base = Path(__file__).parent
    candidates = [explicit, os.environ.get("WMD_FRONTEND_DIST"), base / "frontend", base / "eco-digest-spark" / "dist" / "client"]
    for c in candidates:
        if c and (Path(c) / SHELL_NAME).is_file():
            return Path(c).resolve()
    return None


def mount_frontend(app: FastAPI, dist_dir: str | os.PathLike | None = None) -> bool:
    """画面の配信を有効にする。配信元が見つからなければ False（何も登録しない）。"""
    dist = find_dist(dist_dir)
    if dist is None:
        return False
    shell = dist / SHELL_NAME

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")      # APIの未定義パスは、画面ではなく404
        if path:
            target = (dist / path).resolve()
            try:
                target.relative_to(dist)                                     # 配信元の外へは出さない
            except ValueError:
                raise HTTPException(status_code=404, detail="Not Found")
            if target.is_file():
                headers = _IMMUTABLE if path.startswith("assets/") else _NO_CACHE
                return FileResponse(target, headers=headers)
            if Path(path).suffix:                                            # 無いファイルは404（.jsの代わりにHTMLを返さない）
                raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(shell, media_type="text/html", headers=_NO_CACHE)

    return True
