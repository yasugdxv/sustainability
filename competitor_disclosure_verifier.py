"""
競合サステナビリティモニタリング: 一次開示照合の補助ライブラリ

2026-08-24時点の設計: 「変更後レコードの取得元本文が変更内容を裏付けるか」という
メインの照合ロジックは competitor_change_detector.judge_change() に統合済み
（クロール時に取得済みの本文をその場で使い、再取得しない）。

このモジュールに残っているのは以下の補助部品のみ:
  - VERIFICATION_STATUSES / NON_MEANINGFUL_CHANGE_TYPES: 判定ステータスの定義
    （competitor_change_detector.py, competitor_daily_digest.py から参照）
  - fetch_known_domains: 企業の登録済み公式ドメイン一覧の取得
  - judge_via_search: 変更元が公式ドメインと認識できない場合のWeb検索照合
    （現行の競合クロールはcompetitor_sources＝公式ドメインのみを対象にしているため
    実運用では基本的に発生しないが、将来メディア等の二次情報から変更検知を行う
    ルートが追加された場合に備えて残している）

旧版（2026-08-24未明時点）にあった「本日分の未照合イベントをバッチで再取得・照合する」
仕組み（verify_todays_events/list_unverified_events等）は削除した。後日ページを
再取得して照合すると、その間にページがさらに更新されているケースがあり、
「実際には変わっていないのに再取得時点の内容と食い違ってCONTRADICTEDになる」
誤判定を生んでいたため（実データ検証で確認済み）。
"""
from urllib.parse import urlparse

from article_crawler import SupabaseClient  # noqa: E402

VERIFICATION_STATUSES = ("VERIFIED", "PARTIALLY_VERIFIED", "UNVERIFIED", "CONTRADICTED")
# 表現の言い換え・単純再掲載のみは実質変更でないため、常にUNVERIFIED扱いとする
NON_MEANINGFUL_CHANGE_TYPES = {"WORDING_ONLY", "SIMPLE_REPUBLISH"}


def fetch_known_domains(client: SupabaseClient, company_id: str) -> set:
    """その企業の登録済みcompetitor_sources全件のドメイン集合を返す
    （現行の競合クロールは全てこの一覧のみを対象にしているため、実質すべてが公式ドメイン）"""
    sources = client.select("competitor_sources", {"select": "source_url", "company_id": f"eq.{company_id}"})
    return {urlparse(s["source_url"]).netloc.lower() for s in sources if s.get("source_url")}


# ─── 公式ドメインと認識できない場合のWeb検索照合（将来の二次情報検知ルート向け） ──
SEARCH_VERIFY_SYSTEM_PROMPT = """あなたは企業サステナビリティ情報の一次情報照合専門AIです。
競合企業について検知された変更内容が、当該企業自身の公式開示（Sustainability Report,
ESG Report, Integrated Report, Annual Report, ESG Data Book, 公式サステナビリティ
Webページ, 公式IRページ, 公式ニュースリリース等）で確認できるかを、Web検索で確認して
ください。

厳守事項:
- 必ずWeb検索ツールを1回以上呼び出すこと
- 確認の根拠にできるのは、当該企業自身が公式に発表した情報のみ。以下は根拠にしないこと:
  ニュースメディアの報道、コンサル・NGOレポート、アナリスト記事、他社サイト、
  検索結果のスニペットのみでの判断
- 優先的に確認すべき当該企業の公式ドメイン（分かっている範囲）: {known_domains}
- 上記ドメイン以外で見つかった情報は、それが真に当該企業の公式発表と確認できない限り、
  一次開示とは扱わないこと

判定基準:
- VERIFIED: 当該企業の公式開示が変更内容を直接裏付ける
- PARTIALLY_VERIFIED: 変更自体は確認できるが、対象範囲・数値等の一部が食い違う
- UNVERIFIED: 当該企業の公式開示を発見できない、または根拠不十分
- CONTRADICTED: 当該企業の公式開示が変更内容と明確に矛盾する

出力は次の2部構成にすること:
1. まず1〜3文の日本語の説明文で、検索で確認できた内容と参照した情報源について
   自然に言及すること（情報源のタイトル・発表機関名等に触れること）
2. 次に改行し、コードブロックでJSON1個のみを出力すること:
```json
{{"verification_status": "VERIFIED", "verification_reason": "...",
  "verification_evidence": "...", "primary_source_title": "...",
  "primary_source_document_type": "..."}}
```

# 対象企業
{company_name}

# 検知された変更内容
{change_summary}
"""


def judge_via_search(azure_client, model: str, company_name: str, change_summary: str,
                      known_domains: set) -> dict:
    import sustainability_expert_common as common

    prompt = SEARCH_VERIFY_SYSTEM_PROMPT.format(
        known_domains="、".join(sorted(known_domains)) if known_domains else "（登録済み公式ドメインなし）",
        company_name=company_name, change_summary=change_summary or "(不明)",
    )
    resp = azure_client.responses.create(
        model=model, instructions=prompt,
        input="上記の変更内容を検索で確認してください。",
        tools=[{"type": "web_search_preview"}], tool_choice="required",
    )
    data = common.extract_json_object(resp.output_text)
    evidence = common.extract_url_citations(resp)
    evidence_domains = {urlparse(e["url"]).netloc.lower() for e in evidence if e.get("url")}

    # LLMの自己申告だけに頼らず、根拠URLのドメインを機械的に確認する。
    # 公式ドメインと一致しない場合はVERIFIED/PARTIALLY_VERIFIEDの申告があっても格下げする
    is_official_evidence = bool(evidence_domains & known_domains) if known_domains else False
    if data.get("verification_status") in ("VERIFIED", "PARTIALLY_VERIFIED") and not is_official_evidence:
        data["verification_status"] = "UNVERIFIED"
        data["verification_reason"] = (
            (data.get("verification_reason") or "")
            + " ［自動格下げ: 根拠URLが登録済み公式ドメインと一致しないため一次開示として採用しなかった］"
        ).strip()

    if evidence:
        data["primary_source_url"] = evidence[0]["url"]
        data["primary_source_domain"] = urlparse(evidence[0]["url"]).netloc.lower()
    return data
