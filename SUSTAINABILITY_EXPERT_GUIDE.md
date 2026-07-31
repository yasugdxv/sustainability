# 当社サスティナビリティ専門家 MVP ガイド

作成日: 2026-07-17
更新日: 2026-07-22（週次メールパイプラインとの統合。`sustainability_content_generator.py`は
退役し`_archive/`へ移動、記事ごとの詳細コンテンツ生成は週次メールのテーマ別ダイジェスト
（テーマトピックス）に統合された。詳細は「6. 週次メールパイプラインとの統合（2026-07-22）」参照）

## 1. 機能概要

収集済み記事(article_crawler.py / article_analyzer.pyで取得済みのもの)を、当社サスティ
ナビリティの公式方針・7つの重点テーマ・中長期目標との関連性で評価し、以下の3区分に分類する。

- `publish_candidate`（掲載候補）
- `watch_or_archive`（継続監視）
- `not_selected`（非掲載）

`publish_candidate`となった事象クラスタだけを対象に、サスティナビリティ担当者向けの
コンテンツ候補（何が起きたか／なぜ当社が見るべきか／当社として見るべき観点／確認事項／
根拠・出典／不確実性）を生成する。生成物は自動公開せず、常に`review_required`として
保存し、人による承認・却下・修正・フィードバック記録を経て初めて次工程へ進む。

チャット機能・地政学専門家AIとの連携・複数専門家Agent化は本MVPの対象外。

## 2. アーキテクチャ

既存の設計（Azure OpenAI共通クライアント・Supabase直接アクセス・CLIバッチ実行・
config.jsonによる設定管理）をそのまま踏襲し、新しいフレームワークは追加していない。

```
knowledge/sustainability_expert/          … 専門家の知識ベース(仕様一式)
  ├─ suntory_sustainability_expert_base.json  … 役割・当社文脈・判断ルール
  ├─ article_selection_prompt.md              … 記事選定プロンプト
  ├─ content_generation_prompt.md             … コンテンツ生成プロンプト
  ├─ article_assessment_schema.json           … 記事選定のJSON Schema
  ├─ knowledge_documents.jsonl                … 知識ベース初期データ
  └─ source_registry.csv                      … 知識ベース更新元サイト一覧（参考資料）

sustainability_expert_common.py     … 共通処理（機能フラグ、クラスタ解決アダプター、
                                       LLM構造化出力ヘルパー、expert_runsログ保存）
sustainability_knowledge_store.py   … 知識ベース検索・投入（ローカル簡易実装 / Azure AI Search実装）
sustainability_article_selector.py  … 記事選定パイプライン（CLI）＋list_weekly_picks()
                                       （週次配信件数の機械的な絞り込み）
sustainability_content_generator.py … [退役・_archive/へ移動] 旧コンテンツ生成パイプライン。
                                       週次メールのテーマ別ダイジェストに統合された
weekly_email_report.py              … 週次メール生成パイプライン（CLI、build/2フェーズ）
sustainability_expert_api.py        … APIハンドラー用ラッパー（run.pyから呼ばれる）
sustainability_expert_dashboard.py  … レビュー画面（Streamlit。週次メールレビュー／
                                       個別記事レビュー[旧・参照専用]の2画面）
sql/2026-07-17_sustainability_expert_schema.sql … 新規テーブル定義
sql/2026-07-22_weekly_email_review_gate_schema.sql … 週次メールのレビュー承認ゲート追加
```

### 処理フロー（2026-07-22統合後）

```
articles(is_current=true, 未処理)
  → 事象クラスタ解決（article_urls.duplicate_of_article_url_id を辿るアダプター。
     専用クラスタリング基盤は新設せず、article_urls.article_url_id を
     クラスタの代表ID(article_cluster_id)としてそのまま利用する）
  → 知識ベース検索（当社公式コンテキストを5〜10件）
  → 記事選定LLM呼び出し（sustainability_article_selector.py、--top-n-per-themeで
     テーマ大分類ごと上位N件＋マテリアリティ接続タグワイルドカードに母集団を絞り込み）
      → publish_candidate / watch_or_archive / not_selected（LLMの個別判定。変更なし）
  → list_weekly_picks()で今週の配信件数を機械的に絞り込む（decisionがnot_selected以外を
     total_score降順に並べ、上位15〜20件＝config.jsonのweekly_digest.target_min/target_max
     をチューニング対象として切り詰める。importance_rank_definitionsと同じ考え方）
  → 週次メールドラフト生成（weekly_email_report.py build。記事ごとの見出し・要点書き換え＋
     テーマ別ダイジェスト＝テーマトピックスを生成。record_generator.pyの詳細生成はここに統合）
  → review_required として weekly_email_reports に保存（送信しない）
  → sustainability_expert_dashboard.py の「週次メールレビュー」画面で承認/却下/修正
  → 承認 → 送信（weekly_email_report.py send、または承認ボタンから直接）
```

旧: 記事ごとに`sustainability_content_generator.py`が詳細コンテンツ（何が起きたか／なぜ
当社が見るべきか／推奨アクション等）を個別生成し`expert_contents`に保存していたが、
2026-07-22に週次メールのテーマ別ダイジェストへ統合し、退役した（`_archive/`へ移動）。
過去に生成済みの`expert_contents`データは、ダッシュボードの「個別記事レビュー（旧・参照専用）」
画面から引き続き参照できる（新規データは生成されない）。

### 既存システムとの接続点

- Azure OpenAI呼び出し: `run.make_openai_client(config)` を再利用（新しいクライアントは作らない）
- DBアクセス: `article_crawler.SupabaseClient` を再利用（PostgREST経由、service_role key）
- API層: `run.py` の `DashboardHandler`（既存の自前HTTPサーバー）に5エンドポイントを追加
- レビュー画面: 既存の`run.py`＋`dashboard.html`（globe.gl採用の巨大な地政学リスク監視画面）
  には組み込まず、独立した`sustainability_expert_dashboard.py`（Streamlit）として追加した。
  理由: `run.py`のHTML生成部分は単一の巨大関数で改変リスクが高く、CLAUDE.mdの
  「Streamlitでプロトタイプ作成」方針にも沿うため。既存ダッシュボードには一切変更を加えていない。

## 3. セットアップ方法

1. `config.json.example` を参照し、実際の`config.json`に `sustainability_expert` ブロックを
   追記する（`config.json`は読み取り保護されているため、この追記は手動で行うこと）。
2. `sql/2026-07-17_sustainability_expert_schema.sql` をSupabase SQL Editorで実行する
   （既存テーブルへの破壊的変更は無し。新規テーブル2つを追加するのみ）。
3. 依存ライブラリを確認する（`requirements_azure.txt`参照。`trafilatura` `lxml` `python-docx`
   `pypdf` `python-dateutil` `playwright` `jsonschema` が必要。今回の開発環境には全て
   インストール済みだったため追加インストールは不要だった）。
4. 知識ベースを投入する（4章参照）。

## 4. 必要な環境変数 / config.json設定

| 項目 | 説明 |
|---|---|
| `sustainability_expert.enabled`（またはconfig.json未設定時は環境変数 `SUSTAINABILITY_EXPERT_ENABLED=true`） | 機能フラグ。falseまたは未設定の場合、選定・生成パイプラインは何もせず終了する |
| `sustainability_expert.expert_version` | 専門家バージョン文字列。省略時は`suntory_sustainability_expert_base.json`の`metadata.version`を使う |
| `sustainability_expert.knowledge_search.mode` | `local`（既定、開発用） または `azure_ai_search`（本番） |
| `sustainability_expert.knowledge_search.azure_ai_search.endpoint` / `.api_key` / `.index_name` | Azure AI Search利用時のみ必要 |
| `sustainability_expert.knowledge_search.embedding_deployment` | ベクトル検索を使う場合のみ。Azure OpenAIのembeddingデプロイ名 |

既存の `api_keys.azure_openai` / `supabase` / `proxy` / `ssl` はそのまま再利用する（追加不要）。

### config.jsonが無いサーバー環境向け（環境変数フォールバック）

`article_crawler.load_config()`（本機能や`article_analyzer.py`が共通で使う設定読込）は、
`config.json`が存在しない場合、`app.py`と同じ流儀で環境変数から設定を組み立てる。
本番サーバー移行時、`config.json`を配置せず環境変数だけで動かせるようにするため。

| 環境変数 | 説明 |
|---|---|
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` / `AZURE_OPENAI_API_VERSION` | `app.py`と共通のAzure OpenAI設定（`DEPLOY_GUIDE.md`参照） |
| `OPENAI_API_KEY` | Azure OpenAI未設定時のOpenAIフォールバック |
| `SUPABASE_URL` | SupabaseプロジェクトURL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service_role key（クロール・分析・専門家パイプライン全てに必要） |
| `HTTPS_PROXY` / `HTTP_PROXY` / `SSL_VERIFY` | `app.py`と共通のプロキシ・SSL設定 |
| `SUSTAINABILITY_EXPERT_ENABLED` | 本機能の有効化フラグ（前述の通り） |

`AZURE_OPENAI_API_KEY`と`SUPABASE_URL`のどちらも未設定の場合は、従来通り空設定（`{}`）を返す
（ローカルで`config.json`を使う開発フローに影響しない）。

## 5. DBマイグレーション方法

既存の運用と同じく、SQLファイルをSupabase SQL Editorで手動実行する。

```
sql/2026-07-17_sustainability_expert_schema.sql
```

新規テーブル: `expert_runs`（選定/生成/検証それぞれのLLM呼び出しログ）、
`expert_contents`（レビュー待ちコンテンツ候補）。既存テーブルへの変更は無い。

## 6. 知識投入方法

```bash
python sustainability_knowledge_store.py ingest
python sustainability_knowledge_store.py ingest --mode azure_ai_search   # 本番向け
python sustainability_knowledge_store.py search "水ストレス"              # 動作確認
```

`knowledge/sustainability_expert/knowledge_documents.jsonl` を初期データとして読み込む。
同じ`content_hash`の文書は再投入せず、変更された文書だけ更新する。

## 7. 記事選定の実行方法

```bash
python sustainability_article_selector.py                        # 未処理クラスタを全件処理
python sustainability_article_selector.py 5                       # 先頭5クラスタだけ（テスト用）
python sustainability_article_selector.py --since-days 7          # 直近7日分のみ
python sustainability_article_selector.py --article-ids <id1>,<id2>  # 記事ID指定
```

APIからも実行できる: `POST /api/expert/select-articles`
（body例: `{"since_days": 7, "limit": 20}` または `{"article_ids": ["..."]}`。
1回のHTTPリクエストで処理するクラスタ数は既定で20件まで。大量のバックフィルはCLIを使うこと）

## 8. 週次メール生成方法（2026-07-22統合。旧コンテンツ生成はここに統合された）

```bash
python weekly_email_report.py build [--since-days 7]         # ドラフト生成→review_requiredで保存（送信しない）
python weekly_email_report.py send --report-id <id> [--reviewer-id <id>]  # 承認済みドラフトを送信
                                                               # （主経路はダッシュボードの承認ボタン）
```

旧`sustainability_content_generator.py`（記事ごとの詳細コンテンツ生成）は`_archive/`へ移動し、
`POST /api/expert/generate-content/{cluster_id}`は未使用（呼ばれた場合はエラーレスポンスを返すのみ）。

## 9. レビュー方法

```bash
streamlit run projects/world_monitor_dashboard/sustainability_expert_dashboard.py
```

サイドバーで「週次メールレビュー」（週次ドラフトの概況・テーマ別ダイジェスト＝テーマトピックス・
記事一覧・メールプレビューを確認し、承認して送信/却下/修正のみ保存ができる）と
「個別記事レビュー（旧・参照専用、新規データは増えない）」を切り替えられる。

APIからも操作できる:
- `GET /api/weekly-reports?status=review_required`
- `GET /api/weekly-reports/{report_id}`
- `POST /api/weekly-reports/{report_id}/review`
  （body例: `{"status": "approved", "reviewer_id": "...", "reviewer_feedback": {...}}`）
- 旧: `GET /api/expert/contents?status=review_required` / `GET /api/expert/contents/{content_id}` /
  `POST /api/expert/contents/{content_id}/review`（過去データの参照・修正用に残置）

## 10. テスト方法

```bash
pip install --proxy http://jejp1prfp001.sgn.suntory.co.jp:9400 pytest   # 初回のみ
python -m pytest tests/ -v
```

`tests/test_sustainability_knowledge_store.py`（知識文書読込・検索・重複投入防止）、
`tests/test_sustainability_article_selector.py`（クラスタ解決・重複記事の統合・
publish_candidate判定・重複LLM実行防止・list_weekly_picksの週次件数絞り込み）、
`tests/test_weekly_email_report.py`（記事組み立て・見出し要点書き換え・週次シンセシス・
ドラフト保存/修正/却下/承認送信の状態遷移）を追加・更新した。
Azure OpenAI / Supabaseへは接続せず、`tests/_fakes.py`のフェイク実装でモックしている。
（旧`tests/test_sustainability_content_generator.py`は`_archive/`へ移動済み）

## 11. Azure上で必要な手動設定

- Azure AI Searchを本番利用する場合: `azure_ai_search_index_fields.json`を参考に
  インデックス`suntory-sustainability-expert-v1`を作成する（本パックには
  `AzureAISearchKnowledgeStore.ensure_index_exists()`によるベストエフォートの
  自動作成処理もあるが、権限やAPIバージョンの都合で失敗する場合はポータルから手動作成すること）
- ベクトル検索を使う場合: Azure OpenAIにembeddingモデル（例: text-embedding-3-small）の
  デプロイを追加し、`embedding_deployment`に設定する
- Azure OpenAIの構造化出力(Structured Outputs)を有効に使う場合:
  `api_version`を`2024-08-01-preview`以降、モデルをstructured outputs対応バージョンに
  更新することを推奨する（未対応でも手動JSON抽出+Schema検証にフォールバックするため
  MVPの動作自体には影響しない）

## 12. 現時点の制約

- 事象クラスタは専用のクラスタリング基盤ではなく、既存の`article_urls.duplicate_of_article_url_id`
  を再利用したアダプターである。現状この値を設定する重複判定処理自体は別途必要（本MVPでは
  既存の重複判定結果を「利用できる」ようにしただけで、重複判定ロジック自体は対象外）
- `source_registry.csv`（当社公式サイト26件）を使った知識ベースの自動再取得・再チャンク化は
  未実装。現状は`knowledge_documents.jsonl`の手動更新が前提
- コンテンツ生成のJSON Schemaは仕様パックに同梱されていなかったため、
  `content_generation_framework.required_sections`を基に`sustainability_expert_common.py`内で
  独自定義した
- Azure AI Search実装は用意したが、実際のAzureリソースに対する動作確認は行っていない
  （ローカル簡易実装でのみ動作確認済み）
- 承認後の公開処理は接続していない（既存の公開フローが本リポジトリに存在しないため、
  ステータスは`approved`までとしている）

## 13. 将来の拡張ポイント

- チャット機能: `sustainability_expert_api.py`に対話エンドポイントを追加し、
  `knowledge_store.search()`をRAGの検索部として再利用できる
- 地政学専門家AIとの連携: `expert_runs.task_type`に新しい値を追加し、
  複数専門家の判断を`article_cluster_id`単位で突き合わせる形で拡張できる
- 複数専門家Agent化: 現状は単一専門家・単一プロンプトのMVPだが、
  `suntory_sustainability_expert_base.json`と同形式の専門家定義を追加し、
  `expert_version`で並行運用する拡張が考えられる
