"""
既存の competitor_target_records（is_current=true）・competitor_initiatives のうち
goal_category_id が未設定のレコードに、遡及的にサステナ目標カテゴリを付与する一度きりの
移行スクリプト。

対象データにはPMOが手動分類した「現行小分類」に相当する構造化フィールドが存在しないため
（260728_競合目標DB_サブタグ定義_v0.1.xlsxの「移行マッピング」シートは実データの列ではなく
PMOの手作業分類の記録のため、機械的なJOINには使えない）、既存のtitle/structured_fields/
summary/themesを入力に1件ずつLLMで判定する。

使い方:
    python backfill_competitor_goal_categories.py --limit 5 --dry-run   # まず少数件で内容確認
    python backfill_competitor_goal_categories.py --limit 5             # 少数件を実際に更新
    python backfill_competitor_goal_categories.py                       # 全件実行
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, load_config  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import competitor_classifier as classifier  # noqa: E402
import sustainability_expert_common as common  # noqa: E402


def _build_backfill_schema(goal_category_ids: list) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["goal_category_id"],
        "properties": {
            "goal_category_id": {"type": ["string", "null"], "enum": goal_category_ids + [None]},
        },
    }


def classify_existing_record(azure_client, model: str, *, title: str, themes: list,
                              structured_fields: dict = None, summary: str = None) -> str:
    """既存1レコード分の内容からgoal_category_idを判定する。
    親テーマがthemesに含まれない場合は防御的にnullへ落とす（competitor_classifier.classify_and_extract
    と同じ考え方）"""
    if classifier._GOAL_CATEGORY_PROMPT_TEXT is None:
        raise RuntimeError("先にcompetitor_classifier.load_goal_categories(client)を呼んでください")

    lines = [f"タイトル: {title or ''}", f"テーマ: {', '.join(themes or [])}"]
    if summary:
        lines.append(f"要約: {summary}")
    if structured_fields:
        filled = {k: v for k, v in structured_fields.items() if v not in (None, "")}
        if filled:
            lines.append(f"構造化項目: {filled}")
    user_prompt = "\n".join(lines)

    system_prompt = f"""あなたは競合企業のサステナビリティ目標を分類する専門家です。
既存レコードの内容から、themesのうち主目的となる1テーマ配下のサステナ目標カテゴリを1つだけ
選んでください（1レコード＝1カテゴリ、多重付与しない。該当が薄い場合はそのテーマの「その他」
を選ぶ）。themesが空、または該当が全く無い場合はgoal_category_idをnullにする。

# サステナ目標カテゴリ一覧
{classifier._GOAL_CATEGORY_PROMPT_TEXT}
"""
    schema = _build_backfill_schema(classifier._GOAL_CATEGORY_IDS)
    result = common.call_llm_structured(
        azure_client, model, system_prompt, user_prompt, schema, "GoalCategoryBackfill")
    gc_id = result["data"]["goal_category_id"]

    by_id = {r["goal_category_id"]: r for r in (classifier._GOAL_CATEGORIES or [])}
    gc = by_id.get(gc_id)
    if gc and gc["major_theme"] not in (themes or []):
        return None
    return gc_id


def _backfill_table(client, azure_client, model: str, *, table: str, select_fields: str,
                     extra_params: dict, has_summary: bool, limit, dry_run: bool) -> None:
    params = {"select": select_fields, "goal_category_id": "is.null", **extra_params}
    rows = client.select(table, params)
    if limit:
        rows = rows[:limit]

    print(f"{table}: goal_category_id未設定 {len(rows)}件を処理します（dry_run={dry_run}）")
    for i, row in enumerate(rows, 1):
        gc_id = classify_existing_record(
            azure_client, model,
            title=row.get("title"), themes=row.get("themes"),
            structured_fields=row.get("structured_fields") if not has_summary else None,
            summary=row.get("summary") if has_summary else None,
        )
        id_field = "record_id" if table == "competitor_target_records" else "initiative_id"
        print(f"  [{i}/{len(rows)}] {row[id_field]}: {row.get('title')!r} → {gc_id}")
        if not dry_run:
            client.update(table, {id_field: f"eq.{row[id_field]}"}, {"goal_category_id": gc_id})


def main(limit: int = None, dry_run: bool = False) -> None:
    config = load_config()
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if not azure_client:
        print("⚠ Azure OpenAI / OpenAI のAPIキーが未設定です。中止します。")
        return

    classifier.load_goal_categories(client)

    _backfill_table(
        client, azure_client, model, table="competitor_target_records",
        select_fields="record_id,title,themes,structured_fields",
        extra_params={"is_current": "eq.true"},
        has_summary=False, limit=limit, dry_run=dry_run,
    )
    _backfill_table(
        client, azure_client, model, table="competitor_initiatives",
        select_fields="initiative_id,title,themes,summary",
        extra_params={},
        has_summary=True, limit=limit, dry_run=dry_run,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="各テーブルにつき処理する件数の上限")
    parser.add_argument("--dry-run", action="store_true", help="DBを更新せず、判定結果を表示するだけ")
    args = parser.parse_args()
    main(limit=args.limit, dry_run=args.dry_run)
