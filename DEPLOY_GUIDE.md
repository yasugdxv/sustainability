# サステナビリティ・インテリジェンス ダッシュボード — デプロイ・運用ガイド

> 対象: 部内共有用
> 作成: 2026-06-25
> 改訂: 2026-07-31（旧`run.py`/静的HTML版を廃止し、API+Reactの現行構成に合わせて全面更新）

---

## 1. アプリ概要

競合サステナビリティモニタリング・記事収集/AI分析ダッシュボード。3つの独立したコンポーネントで構成される。

| コンポーネント | 実体 | 役割 |
|---|---|---|
| APIバックエンド | `api_server.py`（FastAPI） | 記事・競合データ・チャット等のREST API |
| フロントエンド | `eco-digest-spark/`（React / TanStack Start） | エンドユーザー向けダッシュボードUI |
| PMOレビューツール | `sustainability_expert_dashboard.py`（Streamlit） | 週次/月次レポートの承認・競合アラートの内部レビュー |

データ収集（`article_crawler.py`・`competitor_crawler.py`等）とLLM分析（`article_analyzer.py`等）はこれらとは別に、
バッチ処理として定期実行する（cron等。常駐サーバーではない）。

旧版（`run.py`が静的HTML `dashboard.html` を`http.server`で配信する「世界地図UI版」）は2026-07頃に廃止し、
`_removed_20260723/`へ退避済み。現在は使用しない。

---

## 2. システム構成

```
ユーザー（ブラウザ）
    │
    ▼
eco-digest-spark（React、Vite/TanStack Start dev server）
    │  fetch("http://127.0.0.1:8000/api/...")
    ▼
api_server.py（FastAPI / uvicorn、ポート8000）
    │
    ├── Supabase ─────────────── 記事・競合データ・タグ・フィルタキーワード等
    ├── Azure OpenAI API ────── 検索意図解析・チャット応答・翻訳
    └── (バッチ処理群は別途、同じSupabaseに直接読み書き)
        article_crawler.py / article_analyzer.py / competitor_crawler.py 等

PMO内部レビュー: sustainability_expert_dashboard.py（Streamlit、上記とは別ポートで起動）
```

> **注記**: 実際のホスティング先（Azure App Service for Containers / Container Apps / 他）は
> このリポジトリ内から確認できていない。この節は実際のホスティング先が確認でき次第、追記すること。

---

## 3. ローカル起動方法（現状の動作確認済みパス）

### 3-1. APIバックエンド

```powershell
.venv\Scripts\activate  # 共有venv（python/直下）を利用
cd projects\world_monitor_dashboard
python api_server.py
```

`http://127.0.0.1:8000` で起動する（`config.json`が無い場合はAzure OpenAI/Supabase連携が無効になり、一部APIが機能しない）。

### 3-2. フロントエンド

```powershell
cd projects\world_monitor_dashboard\eco-digest-spark
npm run dev
```

Vite dev serverが起動し、表示されたURL（例: `http://localhost:3000`）にブラウザでアクセスする。
`src/lib/api.ts`の`API_BASE`が`http://127.0.0.1:8000`に固定されているため、3-1のAPIサーバーを先に起動しておくこと。

### 3-3. PMOレビューツール（Streamlit）

```powershell
cd projects\world_monitor_dashboard
streamlit run sustainability_expert_dashboard.py
```

---

## 4. コンテナデプロイ（APIバックエンドのみ）

`Dockerfile`は**APIバックエンド(`api_server.py`)専用**。フロントエンド・Streamlitツールは含まない。

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements_azure.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV API_PORT=8000
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "docker_entrypoint.py"]
```

起動の流れ:
```
1. docker_entrypoint.py が起動
2. config.json が無ければ環境変数（5節）から自動生成（config_utils.build_config_from_env）
3. api_server.py の FastAPI app を uvicorn で起動（host=0.0.0.0, port=$API_PORT）
```

### ⚠️ フロントエンドの本番配信は未検証

`eco-digest-spark`は`npm run build`でNitro(TanStack Start)のSSRサーバーバンドル(`.output/server/index.mjs`)を
生成するが、2026-07-31時点で`npm run preview`経由の起動確認では500エラーが発生し、本番配信可能な状態には
なっていない（原因未調査）。現状フロントエンドを動かすには、デプロイ先でも`npm run dev`相当（開発サーバー）を
稼働させる運用とするか、別途Nitro本番サーバーの動作検証を行うこと。Vercel/Cloudflare等のTanStack Start対応
ホスティングへ切り出す方が早い可能性もある。

---

## 5. 環境変数一覧

`docker_entrypoint.py` / `config_utils.build_config_from_env()` が参照する環境変数:

| 変数名 | 必須 | 説明 |
|---|---|---|
| `API_PORT` | - | APIサーバーのポート（Dockerfileで`8000`を設定済み） |
| `AZURE_OPENAI_API_KEY` | ✅ | Azure OpenAI APIキー |
| `AZURE_OPENAI_ENDPOINT` | ✅ | Azure OpenAI エンドポイント URL |
| `AZURE_OPENAI_DEPLOYMENT` | - | デプロイ名（例: `Global-AzureOpenAI-DEV-gpt-4o`。未設定時デフォルトあり） |
| `AZURE_OPENAI_API_VERSION` | - | APIバージョン（未設定時デフォルトあり） |
| `SUPABASE_URL` | ✅ | Supabase プロジェクトURL |
| `SUPABASE_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | ✅ | Supabase APIキー（service_role推奨） |
| `OPENAI_API_KEY` | - | Azure OpenAI未設定時のフォールバック |
| `SSL_VERIFY` | 推奨 | `false`に設定（社内プロキシのSSL対応） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 推奨 | 社内プロキシ URL |
| `SUSTAINABILITY_EXPERT_ENABLED` | - | サスティナビリティ専門家MVP機能の有効化（`true`/`false`） |

---

## 6. よくあるトラブル

| 症状 | 原因 | 対処 |
|---|---|---|
| フロントエンドがAPIに繋がらない | `api_server.py`が起動していない/ポート不一致 | `python api_server.py`を先に起動、`API_BASE`（api.ts）とポートを確認 |
| AI要約・検索が動かない | APIキー未設定 or 環境変数ミス | `config.json`またはコンテナ環境変数を確認 |
| SSL エラー | プロキシ経由のSSL検証失敗 | `SSL_VERIFY=false`が設定されているか確認 |
| `npm run dev`がポート競合 | 他プロセスが同ポート使用中 | Viteが自動で別ポートを提示するのでそちらを使う |

---

## 7. 今後の拡張予定（オプション）

- フロントエンド(Nitro本番サーバー)の500エラー原因調査、または本番配信先の選定
- 実際のホスティング先・CI/CDパイプラインの確認とこのガイドへの反映
- `API_BASE`（eco-digest-spark/src/lib/api.ts）のビルド時環境変数化（フロントエンドとバックエンドを別ホストに配置する場合に必要）
- Microsoft Entra ID 認証の追加（IT 管理者によるアプリ登録が必要）
