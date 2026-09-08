# MVP Implementation Plan

## 0. 先に固定するもの
- 専門家の役割と禁止事項
- 記事選定のスコア定義
- コンテンツ出力形式
- 人が評価する正解セット

## 1. Knowledge Base ingestion
- `source_registry.csv` のP0から開始する。
- HTML本文からナビゲーション、フッター、重複文を除去する。
- 見出し単位で分割し、`section_type`を付ける。
- 方針・目標・実績・リスク・事例を区別する。
- URL、取得日、本文ハッシュ、見出し階層を保存する。
- Azure AI Searchへキーワード + ベクター検索可能な形で投入する。

## 2. Article selection pipeline
```
未処理記事
  -> 機械フィルター（重複、本文不足、対象外）
  -> 事象クラスタ作成
  -> テーマ推定
  -> 当社公式文脈検索
  -> Structured Outputで選定評価
  -> publish / watch / reject
```

選定処理では、記事本文だけでなく、検索した当社公式文脈のチャンクIDを保存する。

## 3. Content generation pipeline
```
publish_candidate
  -> 一次情報確認結果を取得
  -> 公式文脈を再検索
  -> コンテンツ生成
  -> 根拠整合チェック
  -> review_requiredで保存
```

## 4. 最小DB追加
### expert_runs
- run_id
- article_cluster_id
- task_type: select / generate / validate
- model_deployment
- prompt_version
- context_chunk_ids
- input_hash
- output_json
- token_usage
- latency_ms
- status

### expert_contents
- content_id
- article_cluster_id
- title
- content_json
- status: review_required / approved / rejected / published
- reviewer_id
- reviewer_feedback
- expert_version

## 5. API例
- `POST /api/expert/select-articles`
- `POST /api/expert/generate-content/{cluster_id}`
- `GET /api/expert/contents?status=review_required`
- `POST /api/expert/contents/{content_id}/review`

## 6. 評価セット
過去4〜8週の記事から、担当者が以下を付けた50〜100事象を用意する。
- 掲載すべき / アーカイブ / 不要
- 理由
- 重要テーマ
- 当社として見るべき観点
- 良いタイトル・要約例

評価指標:
- 掲載候補のPrecision / Recall
- 重要記事の取りこぼし率
- 当社接続の妥当性
- 根拠URLの正確性
- 人手修正率
- 1コンテンツ当たりのレビュー時間

## 7. MVP完了条件
- 公式サイト文脈を根拠として検索できる。
- 同一事象を重複掲載しない。
- 選定理由とスコアが保存される。
- 生成文の事実、当社観点、確認事項が区別される。
- 全コンテンツに根拠URLがある。
- 人の採否・修正が次回評価に利用できる。
