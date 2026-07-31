"""
競合サステナビリティモニタリング: 情報取得〜変更検知〜アラート判定の一気通貫パイプライン

competitor_sources（企業ごとの公式情報源）を巡回し、各URLの本文を取得する。
本文抽出そのものは article_crawler.py の extract_article()（trafilatura/PDF/docx対応込み）を
再利用する（このモジュールでは本文の永続化は行わない。実行結果のみ competitor_crawl_logs に
1情報源1行で記録する）。

取得に成功した本文はその場で competitor_classifier.classify_and_extract() に渡して分類・構造化し、
続けて competitor_change_detector.process_extracted_record() で過去DBとの差分判定・保存を行う
（Azure OpenAI / OpenAI のAPIキーが未設定の場合は分類以降をスキップし、取得・ログ記録のみ行う）。
変更イベントは1件ごとに即時配信せず、全情報源の処理が終わった後に
competitor_daily_digest.build_digest() を1回だけ呼び、その日確定した変更イベントを
まとめた日次ダイジェストを作成する（PMOレビュー、または確信度が高ければ自動配信）。

使い方:
    python competitor_crawler.py          # 全competitor_sourcesを処理
    python competitor_crawler.py 5        # 先頭5件だけ試す（動作確認用）
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from article_crawler import SupabaseClient, extract_article, load_config  # noqa: E402
from config_utils import make_proxies  # noqa: E402
from ai_client import make_openai_client  # noqa: E402
import competitor_classifier as classifier  # noqa: E402
import competitor_change_detector as change_detector  # noqa: E402
import competitor_source_discovery as discovery  # noqa: E402
import competitor_daily_digest as daily_digest  # noqa: E402

REQUEST_VERIFY_DEFAULT = True


def list_sources(client: SupabaseClient) -> list:
    """competitor_sourcesを企業情報つきで取得する"""
    sources = client.select("competitor_sources", {"select": "*"})
    companies = {c["company_id"]: c for c in client.select("competitor_companies", {"select": "*"})}
    for s in sources:
        s["_company"] = companies.get(s["company_id"], {})
    return sources


def save_crawl_log(client: SupabaseClient, source: dict, started_at, finished_at,
                    run_result: str, http_status, items_detected: int,
                    error_message: str = None) -> None:
    if not (isinstance(http_status, int) and 100 <= http_status <= 599):
        http_status = None
    client.insert("competitor_crawl_logs", [{
        "source_id": source["source_id"],
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "run_result": run_result,
        "http_status": http_status,
        "items_detected": items_detected,
        "new_items": 0,
        "updated_items": 0,
        "extraction_failures": 0 if run_result != "抽出失敗" else 1,
        "error_message": error_message,
    }], prefer="return=minimal")


def fetch_source(source: dict, proxies: dict, verify: bool) -> dict:
    """1情報源のURLから本文を取得する（article_crawler.extract_articleをそのまま利用）"""
    return extract_article(source["source_url"], proxies, verify)


def process_source(source: dict, client: SupabaseClient, proxies: dict, verify: bool) -> dict:
    """1情報源を取得し、クロールログを記録する。成功時は本文を含む結果を返す
    （呼び出し側でcompetitor_classifierに渡して分類する）"""
    started_at = datetime.now(timezone.utc)
    result = fetch_source(source, proxies, verify)
    finished_at = datetime.now(timezone.utc)

    if not result.get("ok"):
        save_crawl_log(
            client, source, started_at, finished_at,
            run_result="抽出失敗", http_status=result.get("http_status"),
            items_detected=0, error_message=result.get("error"),
        )
        return {"ok": False, "source": source, "error": result.get("error")}

    save_crawl_log(
        client, source, started_at, finished_at,
        run_result="成功", http_status=result.get("http_status"), items_detected=1,
    )
    return {"ok": True, "source": source, "text": result["text"], "title": result.get("title", ""),
            "final_url": result.get("final_url") or source["source_url"],
            "source_updated_at": result.get("updated_at")}


def process_records(client: SupabaseClient, azure_client, model: str,
                     company: dict, source: dict, records: list, source_updated_at=None) -> list:
    """classifier.classify_and_extract()が返したレコード一覧を変更検知に通す。
    変更イベントの配信判定はここでは行わず、main()の最後にdaily_digest.build_digest()で
    まとめて行う"""
    return [
        change_detector.process_extracted_record(
            client, azure_client, model, company=company, source=source, extracted=extracted,
            source_updated_at=source_updated_at)
        for extracted in records
    ]


def main(limit: int = None):
    config = load_config()
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", REQUEST_VERIFY_DEFAULT)
    client = SupabaseClient(config)
    azure_client, model = make_openai_client(config)
    if not azure_client:
        print("  ⚠ Azure OpenAI / OpenAI のAPIキーが未設定です。取得・ログ記録のみ行い、"
              "分類・変更検知はスキップします。")

    sources = list_sources(client)
    if limit:
        sources = sources[:limit]

    print(f"競合情報源 {len(sources)}件を処理します...")
    fetched = []
    for i, source in enumerate(sources, 1):
        company = source["_company"]
        company_name = company.get("company_name", "?")
        print(f"  [{i}/{len(sources)}] {company_name}: {source['source_url']} ...", end=" ", flush=True)
        try:
            result = process_source(source, client, proxies, verify)
        except Exception as e:
            # プロキシ瞬断等の想定外エラーでバッチ全体を止めない（次の情報源に進む）
            print(f"想定外のエラーでスキップ: {type(e).__name__}: {e}")
            continue
        if not result["ok"]:
            print(f"失敗: {result['error']}")
            continue

        print(f"取得完了（{len(result['text'])}文字）", end="")
        fetched.append(result)

        if source.get("source_type") == "SUSTAINABILITY_HOME":
            try:
                found = discovery.discover_candidates(client, proxies, verify, company, source)
                print(f" / 候補URL{found}件発見", end="")
            except Exception as e:
                print(f" / 候補URL発見エラー: {type(e).__name__}: {e}", end="")

        if not azure_client:
            print()
            continue

        try:
            records = classifier.classify_and_extract(azure_client, model, company_name, result["text"])
        except Exception as e:
            print(f" / 分類エラー: {type(e).__name__}: {e}")
            continue

        outcomes = process_records(client, azure_client, model, company, source, records,
                                    source_updated_at=result.get("source_updated_at"))
        kinds = [o.get("kind") for o in outcomes]
        print(f" / レコード{len(records)}件抽出 → {kinds}")

    print(f"取得完了: {len(fetched)}/{len(sources)}件")

    if azure_client:
        digest = daily_digest.build_digest(client, config)
        if digest is None:
            print("本日の変更イベントは0件のため、日次ダイジェストは作成しませんでした。")
        else:
            print(f"日次ダイジェストを更新しました（digest_id={digest['digest_id']}, "
                  f"review_status={digest.get('review_status', '?')}）")

    return fetched


if __name__ == "__main__":
    _limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=_limit)
