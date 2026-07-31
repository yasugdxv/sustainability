"""
競合サステナビリティモニタリング: メール表示用の共通ヘルパー

日次ダイジェスト(competitor_daily_digest.py)・月次メール(monthly_competitor_report.py)の
両方が使うHTMLエスケープ・日本語ラベル変換をここにまとめる（両モジュール間の循環importを避けるため）。
"""

RECORD_TYPE_LABELS = {
    "TARGET": "目標", "KPI": "KPI指標", "ACTUAL": "実績", "ESG_RATING": "ESG評価",
}

CHANGE_TYPE_LABELS = {
    "NEW_TARGET": "新規設定", "SUCCESSOR_TARGET": "後継目標への切替", "SEPARATE_TARGET": "別目標を新設",
    "TARGET_WITHDRAWN": "撤回", "REMOVED_FROM_PAGE": "ページから削除", "SCOPE_EXPANDED": "対象範囲の拡大",
    "SCOPE_NARROWED": "対象範囲の縮小", "SUBSTANTIVE_CHANGE": "内容を実質的に変更",
    "WORDING_ONLY": "表現の変更のみ（実質変更なし）", "SIMPLE_REPUBLISH": "同一内容の再掲載",
}

DIRECTION_LABELS = {"STRENGTHENED": "強化", "WEAKENED": "後退", "NEUTRAL": "中立"}

# competitor_classifier.STRUCTURED_FIELDS_SCHEMAのキー → 日本語ラベル（月次メールの変更前/変更後表示用）
FIELD_LABELS = {
    "target_value": "目標値", "numeric_value": "数値", "unit": "単位", "reduction_rate": "削減率",
    "base_year": "基準年", "target_year": "目標年", "interim_target_year": "中間目標年",
    "target_company": "対象会社", "target_region": "対象地域", "target_site": "対象拠点",
    "target_product": "対象商品", "target_material": "対象原材料", "target_packaging": "対象包装材",
    "scope": "Scope区分", "boundary": "算定範囲", "kpi_definition": "KPI定義",
    "achievement_status": "達成状況", "actual_fiscal_year": "実績年度", "actual_value": "実績値",
    "esg_score": "ESG評価スコア", "esg_rank": "ESG評価ランク", "selection_status": "選定状況",
}


def esc(text) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def event_badge(e: dict) -> str:
    """record_type/change_type/directionの生の列挙値を、読み手に伝わる日本語タグにする"""
    parts = [RECORD_TYPE_LABELS.get(e.get("record_type"), e.get("record_type") or ""),
             CHANGE_TYPE_LABELS.get(e.get("change_type"), e.get("change_type") or "")]
    if e.get("direction"):
        parts.append(DIRECTION_LABELS.get(e["direction"], e["direction"]))
    return "｜".join(p for p in parts if p)


def format_before_after(changed_fields: list) -> tuple:
    """changed_fields([{field, before, after}, ...])から、日本語ラベル付きの
    「変更前」「変更後」テキスト（改行区切り）を組み立てる。空なら("", "")を返す"""
    if not changed_fields:
        return "", ""
    before_lines = []
    after_lines = []
    for f in changed_fields:
        label = FIELD_LABELS.get(f.get("field"), f.get("field"))
        before_lines.append(f"{label}: {f.get('before') if f.get('before') not in (None, '') else '（記載なし）'}")
        after_lines.append(f"{label}: {f.get('after') if f.get('after') not in (None, '') else '（記載なし）'}")
    return "\n".join(before_lines), "\n".join(after_lines)


def format_fields(fields: dict) -> str:
    """structured_fields(dict)から、日本語ラベル付きの読みやすいテキスト（改行区切り）を組み立てる。
    比較対象が無い新規目標(NEW_TARGET)の内容をそのまま提示する用途"""
    if not fields:
        return ""
    lines = []
    for key, label in FIELD_LABELS.items():
        value = fields.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    return "\n".join(lines)
