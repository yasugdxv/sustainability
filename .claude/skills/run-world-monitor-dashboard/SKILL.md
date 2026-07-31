---
name: run-world-monitor-dashboard
description: run, start, launch, screenshot, test the world_monitor_dashboard — サステナビリティ・インテリジェンス ダッシュボード（APIバックエンド api_server.py + Reactフロントエンド eco-digest-spark）。Use when asked to run, start, verify, or take a screenshot of this app.
---

# world_monitor_dashboard — Run Skill

競合サステナビリティモニタリング・記事収集/AI分析ダッシュボード。2つの独立したプロセスで構成される。

| コンポーネント | 実体 | ポート |
|---|---|---|
| APIバックエンド | `api_server.py`（FastAPI/uvicorn） | 8000 |
| フロントエンド | `eco-digest-spark/`（React、Vite/TanStack Start dev server） | Viteが自動選択（通常3000系） |

旧版（`run.py`が静的HTML `dashboard.html` を配信する「世界地図UI版」）は2026-07に廃止済み、
`_removed_20260723/`へ退避済み。**現在は使用しない**（このスキルの旧版が`run.py`を前提にしていたら誤り）。

PMOレビュー用のStreamlitアプリ(`sustainability_expert_dashboard.py`)は別途 `streamlit run` で起動する
（このスキルの対象外。ユーザーから明示的に依頼された場合のみ起動する）。

## 前提条件

```
# 仮想環境は c:\Users\269811\python\.venv （projects/の親、全プロジェクト共有）
.venv\Scripts\python.exe -c "import fastapi, uvicorn, requests"

# フロントエンドの依存関係（初回のみ）
cd projects\world_monitor_dashboard\eco-digest-spark
..\.tools\node-v22.14.0-win-x64\npm.cmd install
```

`.tools/node-v22.14.0-win-x64/` はプロキシ制約下でのNode.js再ダウンロードを避けるための同梱ランタイム。
システムにNode/npmが別途入っていればそちらを使っても良い。

## ビルド / セットアップ

ビルド不要（開発モードで起動する）。`projects/world_monitor_dashboard/config.json` が無いとAzure OpenAI/Supabase連携が無効になり、一部APIが機能しない（`config.json.example`参照）。

## 起動 — エージェント向けパス（推奨）

### スモークテスト（APIのみ、起動確認）

```powershell
cd c:\Users\269811\python\projects\world_monitor_dashboard
..\.venv\Scripts\python.exe .claude\skills\run-world-monitor-dashboard\smoke.py
```

内部で`api_server.py`をポート8000相当（`API_PORT`環境変数）で起動し、`/api/categories`への応答を健全性確認とする。

期待出力：
```
[smoke] API OK: http://127.0.0.1:8000/api/categories
[smoke] PASS
[smoke] Process terminated
```

### 手動での起動確認（APIバックエンド + フロントエンド）

```powershell
# 1. APIバックエンドをバックグラウンドで起動
cd c:\Users\269811\python\projects\world_monitor_dashboard
Start-Process -FilePath "..\.venv\Scripts\python.exe" -ArgumentList "api_server.py" `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden

# 2. 起動待機
Start-Sleep 5

# 3. ヘルスチェック
curl http://127.0.0.1:8000/api/categories
# → JSONが返れば正常

# 4. フロントエンド起動（別ターミナル/別プロセス）
cd eco-digest-spark
..\.tools\node-v22.14.0-win-x64\npm.cmd run dev
# → 表示されたURL（例: http://localhost:3000）にブラウザでアクセス
```

## ポート競合が発生した場合

```powershell
# APIのポートを変えて起動
$env:API_PORT = "8001"
.venv\Scripts\python.exe api_server.py
```

`eco-digest-spark/src/lib/api.ts`の`API_BASE`は`http://127.0.0.1:8000`に固定されているため、
APIのポートを変える場合はこのファイルも合わせて変更する必要がある。

## Gotchas

- **APIキーなしでも一部動作**: `config.json`が無くてもAPIサーバー自体は起動するが、AI検索・チャット・翻訳機能はAzure OpenAI未設定のため動かない。
- **フロントエンドはAPIサーバーが先**: `eco-digest-spark`はビルド時ではなく実行時に`127.0.0.1:8000`へfetchするため、APIサーバーを先に起動しておくこと。
- **フロントエンドの本番ビルド配信は未検証**: `npm run build`→`npm run preview`は2026-07-31時点で500エラーになることを確認済み（原因未調査）。スクリーンショット等の確認は`npm run dev`（開発サーバー）で行うこと。
- **SSL/プロキシ**: 社内プロキシ環境のため`config.json`の`ssl.verify: false`・`proxy`セクションが必要になる場合がある。

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `dependencies OK`が出ない | `.venv/Scripts/pip install fastapi uvicorn requests`（社内プロキシ指定） |
| `/api/categories`が応答しない | ポート競合の可能性。`netstat -ano \| findstr :8000`で確認 |
| フロントエンドが真っ白/APIエラー | APIサーバー未起動、またはポート不一致。`api.ts`の`API_BASE`を確認 |
| `npm run dev`が動かない | `eco-digest-spark`で`npm install`を先に実行したか確認 |
