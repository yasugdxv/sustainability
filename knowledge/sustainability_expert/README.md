# Suntory Sustainability Expert MVP Base v0.1

作成日: 2026-07-17

## 目的
既存のAzure OpenAI呼び出しを活かし、収集済み記事を「当社サステナビリティ専門家」として選定し、根拠付きコンテンツ候補を生成するための初期ベースです。

## 収録物
- `source_registry.csv`: 公開サイトの優先クロール対象26件
- `suntory_sustainability_expert_base.json`: 専門家の役割、当社文脈、重点テーマ、目標、判断ルール
- `knowledge_documents.jsonl`: Azure AI Searchへ投入できる初期知識チャンク
- `article_selection_prompt.md`: 記事選定プロンプト
- `content_generation_prompt.md`: コンテンツ生成プロンプト
- `article_assessment_schema.json`: Structured Outputs用JSON Schema
- `azure_ai_search_index_fields.json`: 検索インデックスの概念フィールド設計
- `implementation_plan.md`: MVP実装手順

## 推奨実装
1. 既存クロール記事を事象クラスタ化し、重複を除く。
2. 本パックの公式サイト文脈をAzure AI Searchへ投入する。
3. 記事ごとにテーマ推定し、関連する公式文脈をハイブリッド検索する。
4. Azure OpenAIのStructured Outputsで記事選定を実行する。
5. `publish_candidate`だけを別呼び出しでコンテンツ生成する。
6. ダッシュボードにレビュー待ちとして保存し、人の採否・修正を記録する。
7. 人の評価結果を用いてスコア閾値・プロンプトを調整する。

## 重要な設計判断
- 選定とコンテンツ生成を同じLLM呼び出しにしない。
- 公式方針・目標と、取り組み事例・ストーリーを別の権威レベルにする。
- 記事単位ではなく、同一事象のクラスタ単位で掲載判断する。
- 最初は既存コード + Azure AI Search + Azure OpenAIで実装し、Agent Service導入は必須としない。
- 公開サイト情報だけで判断できない当社影響は、断定せず確認事項として出す。

## 更新方法
- `P0`: 月次または変更検知時に再取得
- `P1`: 月次または四半期
- `P2`: 年次資料・補足資料として利用
- URLごとに本文ハッシュを保存し、変更時のみ再チャンク・再ベクトル化する。
