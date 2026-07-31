# v1 → v2 変更前後の差分一覧

## crawl_targets

| 項目 | v1 | v2 |
|---|---|---|
| タグ列（theme_major等11列） | あり（カンマ区切り文字列で直接保持） | **削除**。`crawl_target_tags` 経由で `tag_reference.tag_id` を紐付ける |
| `target_url` | 制約なし（重複可） | **`unique` 制約を追加**（重複不可） |
| `crawl_method` | 制約なし | **CHECK制約を追加**（HTML/RSS/API/ブラウザ操作/メール/手動のみ許可） |
| `lookback_days` | 制約なし | **CHECK制約を追加**（NULL または 0以上） |
| `updated_at` | `default now()` のみ（UPDATE時に自動更新されない） | **`set_updated_at` トリガーを追加**（UPDATE時に自動更新） |
| `publisher_major` | 自由入力 | 変更なし（ただしタグリファレンスの主体タグ大分類名と同一名称を使う運用ルールを追加） |

## crawl_logs

| 項目 | v1 | v2 |
|---|---|---|
| `items_detected`/`new_items`/`updated_items`/`retry_count` | デフォルトなし（必須） | **デフォルト0を追加** |
| 上記4列 | 制約なし | **CHECK (>= 0) を追加** |
| `finished_at` | 制約なし | **CHECK (finished_at >= started_at) を追加** |
| `http_status` | 制約なし | **CHECK (100〜599) を追加** |
| `crawl_target_id` 外部キー | 参照のみ（削除時の挙動未指定） | **`on delete restrict`** に変更（ログを持つクロール先は削除不可） |
| インデックス | なし | **`(crawl_target_id, started_at desc)` と `(run_result, started_at desc)` を追加** |

## tag_reference

| 項目 | v1 | v2 |
|---|---|---|
| 構造 | `tag_axis, tag_major, tag_minor, tag_meaning, tag_criteria` のフラット構造 | **`tag_level`（大分類/小分類）、`tag_code`、`parent_tag_id`、`display_order`、`status` を追加**。大分類・小分類をそれぞれ独立した行として管理し、`parent_tag_id` で親子関係を表現 |
| 一意制約 | `tag_id` の主キーのみ | **`unique(tag_axis, tag_code)`、`unique(tag_axis, tag_name, parent_tag_id)` を追加** |
| `updated_at` | 自動更新なし | **`set_updated_at` トリガーを追加** |
| 主体タグ | 10大分類（一部は代表例のみ記載、`notes`に「代表例」と明記） | **11大分類**（メディア・データ提供機関を追加）、**小分類は実在の企業名・機関名を1件ずつ個別登録**（159行） |
| 地域タグ | 6大分類（グローバル/EU/地域圏/国/州自治体/複数地域）、小分類は例示のみ | **5大分類**（複数地域は廃止、超国家・経済圏を新設）、**小分類は実在の国名・地域名を1件ずつ個別登録**（63行）。「複数地域」は単一タグとして作らず、複数の地域タグを併用する運用に変更 |
| 記事種別タグ | 4件のみ（推定・暫定と明記） | **9大分類・97小分類（計106行）を新設**。「推定」「暫定」の注記は削除 |
| テーマ/横断/マテリアリティ/情報属性 | 大分類は`tag_major`列の値のみ（独立した行なし） | 内容は維持しつつ、**大分類ごとに独立した行を追加**し、小分類から`parent_tag_id`で参照する構造に変更 |
| 総行数 | 149行 | **466行** |
| 「推定」「暫定」「代表例」「網羅的ではない」等の注記 | あり | **すべて削除**。代わりに実在するタグとして個別登録 |

## crawl_target_tags（新設）

v1には存在しなかった中間テーブル。`crawl_target_id` と `tag_id` の多対多を管理し、テーマ・横断・主体・地域・記事種別・マテリアリティ接続・情報属性のすべてのタグをここに登録する。`on delete cascade`（クロール先削除時は紐付けも削除）、`tag_id`側は`on delete restrict`（使用中のタグは削除不可）。

## RLS

v1では言及なし。v2では4テーブルすべてで `enable row level security` を実行し、ポリシーは付与しない（＝`service_role`以外は既定で読み書き不可）。閲覧が必要になった時点で個別にSELECTポリシーを追加する運用。

## データ投入

| 項目 | v1 | v2 |
|---|---|---|
| crawl_targets | データなし（構造のみ） | **元の61件RSSフィードをインポート**（`2026-07-15_crawl_targets_import.sql` / `.csv`） |
| crawl_target_tags | （テーブル自体が存在しない） | **61件それぞれに地域タグ1件＋記事種別タグ1件、計122件を紐付け**（`2026-07-15_crawl_target_tags_import.sql` / `.csv`） |

### crawl_targetsインポートに関する注記

61件はいずれも元々RSSフィードURLだったため、`endpoint_type`/`crawl_method`は全件`RSS`、`change_detection_method`は全件`新規URL`、`crawl_detail_pages`は全件`false`（RSS側で要約が取得できるため）としている。`publisher_name`/`publisher_major`/`publisher_minor`は各フィードの発信元を個別に判定して設定した（例: FDA/EFSA/英国Food Standards Agencyは`規制当局・政府`、それ以外の59件は`メディア・データ提供機関`）。

`crawl_target_tags`は61件それぞれに「地域タグ1件」（フィードの対象地域・検索条件のgl/hlパラメータ等から判定）と「記事種別タグ1件」（Google News検索フィードは`複合＞ニュースアグリゲーター`、規制当局公式フィードは`規制・法律`、それ以外の一般メディア・業界メディアは`ニュース・分析`）のみを付与しており、テーマ・主体・マテリアリティ接続・情報属性タグは付与していない。これらの61件は特定テーマに限定されない広範なニュース源であるため、テーマ等の付与は個別記事単位で行うのが適切と判断したためである。必要であれば追加で付与できる。
