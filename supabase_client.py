"""Supabase(PostgREST)の薄いラッパー。外部情報収集の共通部品。

もともと article_crawler.py に同居していたものを、収集基盤を共通化するために切り出した。
中身は一切変えていない。既存コードの `from article_crawler import SupabaseClient` は、
article_crawler.py からの再公開で、そのまま動く。
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from config_utils import make_proxies


# ─── Supabase（service_role・PostgREST）薄いラッパー ──────────────────
class SupabaseClient:
    """service_role key で crawl管理・記事テーブルを読み書きする"""

    def __init__(self, config: dict):
        sb = config.get("supabase", {})
        url = sb.get("url", "").rstrip("/")
        if url.endswith("/rest/v1"):
            url = url[: -len("/rest/v1")]
        api_key = sb.get("service_role_key") or sb.get("key")
        if not url or not api_key:
            raise ValueError("config.json に supabase.url / service_role_key がありません")
        self.base_url = url
        self.headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.proxies = make_proxies(config)
        self.verify = config.get("ssl", {}).get("verify", True)

    def select(self, table: str, params: dict) -> list:
        """呼び出し側がlimit/offsetを指定していない場合は「全件取得」の意図とみなし、
        PostgRESTの1リクエストあたりの上限（既定1000件）を超えないよう自動でページングする。
        （この上限に気づかず一部の行だけを取得してしまうバグを防ぐため）

        ORDER BYを指定しないままoffsetページングすると、PostgREST/Postgres側の行順が
        リクエストごとに変わることがあり、同じ条件で問い合わせても取りこぼし・重複が
        発生する（実際に発生を確認: 書き込みが無い状態でも同一クエリを連続実行すると
        結果セットが変わった）。そのためorder未指定時は、selectで指定された最初の列
        （このコードベースでは慣例的に主キー列を先頭に書く）を昇順の並び替えキーとして
        補い、ページ間の順序を安定させる。select="*"等で列名が分からない場合は、
        取得件数が1000件を超えるほど大きいテーブルでない前提として何もしない。"""
        if "limit" in params or "offset" in params:
            resp = requests.get(
                f"{self.base_url}/rest/v1/{table}", headers=self.headers,
                params=params, proxies=self.proxies, verify=self.verify, timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

        if "order" not in params:
            first_col = (params.get("select") or "").split(",")[0].strip()
            if first_col and first_col != "*":
                params = {**params, "order": f"{first_col}.asc"}

        page_size = 1000
        # 1ページ目はPrefer: count=exactを付け、PostgRESTがContent-Range(例: 0-999/12345)
        # ヘッダで返す総件数を使って残りページ数を先に確定させる。
        # これにより2ページ目以降を（順番待ちせず）ThreadPoolExecutorで並列取得できる
        # （_select_in_chunksと同じ理由・同じパターン。1000件超の全件取得が逐次ページングで
        # 遅かった問題に対応。実測: articles/article_analysisの全件取得で約49秒→大幅短縮）。
        # Content-Rangeが取得できない/解釈できない場合は、安全側として従来の逐次ページングに
        # フォールバックする。
        resp = requests.get(
            f"{self.base_url}/rest/v1/{table}", headers={**self.headers, "Prefer": "count=exact"},
            params={**params, "limit": page_size, "offset": 0},
            proxies=self.proxies, verify=self.verify, timeout=30,
        )
        resp.raise_for_status()
        first_rows = resp.json()
        all_rows = list(first_rows)

        total = None
        content_range = resp.headers.get("Content-Range", "")
        if "/" in content_range:
            total_part = content_range.rsplit("/", 1)[1]
            if total_part.isdigit():
                total = int(total_part)

        if len(first_rows) < page_size:
            return all_rows

        if total is None:
            # フォールバック: 従来通りの逐次ページング
            offset = page_size
            while True:
                resp = requests.get(
                    f"{self.base_url}/rest/v1/{table}", headers=self.headers,
                    params={**params, "limit": page_size, "offset": offset},
                    proxies=self.proxies, verify=self.verify, timeout=30,
                )
                resp.raise_for_status()
                rows = resp.json()
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size
            return all_rows

        def _fetch_page(off: int):
            # select()には本文全文(extracted_text)等の重い列を含む呼び出しもあり、
            # 同時実行数が多いとPostgREST/Postgres側が瞬間的に500を返すことがあるため
            # （実測確認済み）、並列数を抑えつつ簡単なリトライを入れる。
            last_exc = None
            for attempt in range(3):
                try:
                    r = requests.get(
                        f"{self.base_url}/rest/v1/{table}", headers=self.headers,
                        params={**params, "limit": page_size, "offset": off},
                        proxies=self.proxies, verify=self.verify, timeout=30,
                    )
                    r.raise_for_status()
                    return r.json()
                except requests.exceptions.HTTPError as e:
                    last_exc = e
                    if e.response is not None and e.response.status_code < 500:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            raise last_exc

        remaining_offsets = list(range(page_size, total, page_size))
        if not remaining_offsets:
            return all_rows
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_fetch_page, off) for off in remaining_offsets]
            for future in futures:
                all_rows.extend(future.result())
        return all_rows

    def insert(self, table: str, rows: list, prefer: str = "return=representation"):
        resp = requests.post(
            f"{self.base_url}/rest/v1/{table}",
            headers={**self.headers, "Prefer": prefer},
            json=rows, proxies=self.proxies, verify=self.verify, timeout=30,
        )
        resp.raise_for_status()
        return resp.json() if resp.text else None

    def update(self, table: str, params: dict, patch: dict, prefer: str = None):
        """prefer未指定時は従来通り（Prefer: return=minimal、戻り値なし）。
        prefer="return=representation" を渡した場合のみ、PostgRESTに実際に
        更新された行を返させ、そのリストをそのまま返す（0件なら「paramsの条件に
        一致する行が無かった」＝呼び出し側が条件付きUPDATEで排他制御したい時に使う。
        send_state_machine.py の atomic claim design がこれに依存する）。
        既存の全呼び出し箇所はpreferを渡さないため、挙動は一切変わらない"""
        resp = requests.patch(
            f"{self.base_url}/rest/v1/{table}",
            headers={**self.headers, "Prefer": prefer or "return=minimal"},
            params=params, json=patch, proxies=self.proxies, verify=self.verify, timeout=30,
        )
        resp.raise_for_status()
        if prefer:
            return resp.json() if resp.text else []

    def delete(self, table: str, params: dict):
        resp = requests.delete(
            f"{self.base_url}/rest/v1/{table}",
            headers={**self.headers, "Prefer": "return=minimal"},
            params=params, proxies=self.proxies, verify=self.verify, timeout=30,
        )
        resp.raise_for_status()

    def rpc(self, fn_name: str, params: dict):
        resp = requests.post(
            f"{self.base_url}/rest/v1/rpc/{fn_name}", headers=self.headers,
            json=params, proxies=self.proxies, verify=self.verify, timeout=30,
        )
        resp.raise_for_status()
        return resp.json() if resp.text else None


def _select_in_chunks(client, table: str, base_params: dict, id_field: str, ids: list,
                       chunk_size: int = 100, max_workers: int = 8) -> list:
    """idsを1つの巨大な in.(id1,id2,...) にまとめると、件数が多い場合にURLが長くなりすぎて
    PostgRESTが400 Bad Requestを返すことがある（実際に記事数が700件を超えて発生した）ため、
    chunk_size件ずつに分割してクエリし、結果を連結する。

    2026-09-14: chunkを順次(逐次)実行していたため、chunk数が多い呼び出し
    （例: /api/competitors/changes、after_record_id 2842件→29chunk）で
    社内プロキシ経由のラウンドトリップが積み上がり15秒超のタイムアウトが発生していた。
    各chunkは独立した読み取りクエリで結果の集約順序に依存しないため、
    ThreadPoolExecutorで並列実行する（実測: 29chunkで約12秒→約2秒に短縮）。"""
    chunks = [ids[i:i + chunk_size] for i in range(0, len(ids), chunk_size)]
    if not chunks:
        return []
    if len(chunks) == 1:
        return client.select(table, {**base_params, id_field: f"in.({','.join(chunks[0])})"})
    rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(client.select, table, {**base_params, id_field: f"in.({','.join(c)})"})
            for c in chunks
        ]
        for future in futures:
            rows.extend(future.result())
    return rows
