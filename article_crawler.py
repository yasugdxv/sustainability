"""
記事クロール・保存モジュール

Supabaseの crawl_targets（クロール先マスタ）を巡回し、直近 lookback_days 以内に
公開された記事を収集して article_urls / articles へ保存する。
実行結果は crawl_logs に1クロール先1行で記録する。

対応クロール手法: RSS, HTML（汎用本文抽出）, ブラウザ操作（Playwright、bot対策サイト向け）。
※ブラウザ操作は本番バッチ(run_daily.py)の実行環境では使用不可のため、
  2026-08-24以降は新規クロール先に設定しない（手動実行での検証用途のみ残置）。
API・メール・手動は対象外（サイトごとに個別実装が必要なため今回は範囲外）。

その他の処理:
    - PDF/Word(.docx)は本文抽出して articles/article_files へ保存
    - URL正規化（トラッキングパラメータ除去等）・canonical URL採用で重複URLを防止
    - 抽出後の定型文（Cookie通知・シェア誘導等）除去
    - 公開日時に加え、更新日時(source_updated_at)も取得しUTCへ統一

使い方:
    python article_crawler.py          # 全RSS/HTML対象をクロール
    python article_crawler.py 10       # 先頭10件だけ試す（動作確認用）
"""
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import trafilatura
from dateutil import parser as dateutil_parser
from docx import Document as DocxDocument
from lxml import html as lxml_html
from pypdf import PdfReader

from config_utils import load_config, make_proxies  # noqa: F401 (load_configは他モジュールもここ経由でimportする)

DEFAULT_LOOKBACK_DAYS = 7
REQUEST_TIMEOUT = 45  # 20秒だと重いページ(JS/トラッカー多数等)で正常応答なのに時間切れになる例が多発したため延長
MAX_HTML_LINKS_PER_TARGET = 30   # HTML一覧ページから拾う候補リンクの上限
ARTICLE_FETCH_SLEEP = 0.3        # 記事ページ取得の間隔（相手サーバへの配慮）
BROWSER_NAV_TIMEOUT_MS = 25000   # Playwrightのページ遷移タイムアウト
BROWSER_RENDER_WAIT_MS = 1500    # JS描画待ちの簡易ウェイト
SUPPORTED_METHODS = ("RSS", "HTML", "ブラウザ操作", "Zyte", "JSON API")

# PDF/.docxは本文抽出対応（下記DOCUMENT_EXTENSIONS）。それ以外の添付系拡張子は
# 記事本文として扱わない（.doc(旧形式)は対応ライブラリが無いため対象外）
DOCUMENT_EXTENSIONS = (".pdf", ".docx")
NON_ARTICLE_EXTENSIONS = (
    ".xlsx", ".xls", ".csv", ".doc", ".pptx", ".ppt",
    ".zip", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mp3",
    ".md",      # 記事と同じ内容のMarkdown版（例: CDPのサイトマップに、記事URL+.mdが並ぶ。重複して保存しない）
)
# ── 記事ではないURLの除外（2026-10-23追加）──────────────────────────────────────
# include_paths="/" の一覧では、問い合わせフォーム・登録・求人・検索・APIなどのリンクまで候補になり、
# 取得に失敗して、有料のAgentまで使い切っていた（2026-10-23: 最終失敗170記事のうち13件が記事ではないページ。
# 例: gea.com/ja/contact、jobs.yum.com、globallivingwage.org/subscribe、generalmills.com/api/download）。
# パスの「区切りごとの完全一致」で判定し、記事のスラッグ(例: contact-center-report)は巻き込まない。
NON_ARTICLE_PATH_SEGMENTS = frozenset({
    "contact", "contacts", "contact-us", "contactus", "regional-contacts", "inquiry", "forms", "form",
    "subscribe", "subscription", "newsletter-signup", "recruit", "career", "careers", "jobs",
    "search", "sitemap", "login", "signin", "sign-in", "auth", "api", "wp-content", "wp-admin", "wp-json",
})
NON_ARTICLE_HOST_PREFIXES = ("jobs.", "careers.", "idp.", "auth.", "login.", "sso.", "accounts.")


# ── フィード型の判定とサイレント故障の検知（2026-10-23追加）──────────────────────
# 方式を「RSS→Zyte/HTML」に変えても、種別(endpoint_type)がRSSのままだと、フィードのURLを「1ページ」として
# 扱い、中の記事を取りに行かなかった（8対象が、全期間で記事0件。Amnestyはフィードの XML 7万文字を1件の
# 「記事」として保存していた）。種別がRSSで、URLがフィードの形なら、方式によらずRSSとして一覧を読む。
_FEED_PATH_RE = re.compile(r"(rss|feed|atom|\.xml$)", re.IGNORECASE)


def _is_feed_target(target: dict) -> bool:
    """種別がRSSで、一覧のURLがフィードの形(rss/feed/atom/.xml)か（HTMLのニュース一覧は含めない）"""
    if (target.get("endpoint_type") or "") != "RSS":
        return False
    return bool(_FEED_PATH_RE.search(urlparse(target.get("target_url") or "").path))


# 「条件(include_paths)に合う記事が0件なのに、一覧には記事らしいリンクがある」を検知する
# （include_pathsの設定ミス・サイト構成の変更。2026-10-23: 6対象が半年近く記事0件のまま気づかれなかった）
INCLUDE_NOMATCH_MIN_LINKS = 3
_ARTICLE_PATH_WORDS = ("news", "press", "blog", "insight", "publication", "report", "article", "stor", "media",
                       "resource", "update", "event", "release", "research", "analysis", "statement", "speech",
                       "newsroom", "magazine", "ニュース", "お知らせ", "プレス")
_DATE_IN_PATH_RE = re.compile(r"(19|20)\d{2}([/\-_]|$|\d)|/\d{4}/\d{1,2}/")


def _looks_article_like(path: str) -> bool:
    """URLのパスが、記事らしいか（2階層以上で、日付・記事系の語・長いスラッグのいずれかがある）"""
    segs = [x for x in path.split("/") if x]
    if len(segs) < 2:
        return False
    low = path.lower()
    return bool(_DATE_IN_PATH_RE.search(low)) or any(w in low for w in _ARTICLE_PATH_WORDS) or len(segs[-1]) > 25


def _include_nomatch_funnel(target: dict) -> dict:
    """list_html_candidates()が残した統計から、funnelへ入れる警告を作る（無ければ空）"""
    st = target.get("_listing_stats") or {}
    if not st.get("include_nomatch"):
        return {}
    return {"include_nomatch": {"n": st["include_nomatch"], "top": st.get("top", [])}}


def _is_non_article_url(parsed, path: str, include_paths: list) -> bool:
    """記事ではないURL(問い合わせ・登録・求人・検索・API・ログイン等)か。
    include_pathsで、"/"以外の具体的なパスが明示的に指定されている場合は、その指定を優先して除外しない"""
    explicit = [p for p in include_paths if p.rstrip("/") not in ("", "/")]
    if explicit and any(_path_matches(path, p) for p in explicit):
        return False
    if parsed.netloc.lower().startswith(NON_ARTICLE_HOST_PREFIXES):
        return True
    return any(seg.lower() in NON_ARTICLE_PATH_SEGMENTS for seg in path.split("/") if seg)


DOCUMENT_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}

# 抽出後の本文からURLと同様に取り除く定型文（Cookie通知・シェア誘導等の簡易パターン）
_BOILERPLATE_LINE_PATTERNS = [
    re.compile(r"^(cookie|クッキー)", re.IGNORECASE),
    re.compile(r"^(share (this|on)|シェアする|シェア|フォローする)", re.IGNORECASE),
    re.compile(r"^(subscribe|購読|メルマガ登録)", re.IGNORECASE),
    re.compile(r"^(advertisement|広告|スポンサー)$", re.IGNORECASE),
    re.compile(r"^(read more|続きを読む)$", re.IGNORECASE),
]

# 有料会員限定記事で、本文プレビューの途中から会員登録・ログイン誘導の定型文に
# 切り替わるサイト（例: sustainablejapan.jp）向け。この種の定型文は一致箇所から
# 記事末尾まで続くブロックのため、_BOILERPLATE_LINE_PATTERNSの1行単位の除去では
# 対応できず、一致箇所以降を丸ごと切り捨てる（_truncate_at_paywall参照）
_PAYWALL_TRUNCATE_PATTERNS = [
    re.compile(r"この記事は有料会員限定です"),
    re.compile(r"無料会員に登録すると"),
    re.compile(r"有料記事の「閲覧チケット」"),
]


def _truncate_at_paywall(text: str) -> str:
    if not text:
        return text
    earliest = min(
        (m.start() for pat in _PAYWALL_TRUNCATE_PATTERNS if (m := pat.search(text))),
        default=None,
    )
    return text[:earliest].rstrip() if earliest is not None else text

# Bot対策（人間認証・アクセス拒否）ページの検知パターン。
# 誤検知を避けるため、本文の大半を占める短文でのマッチのみを対象とする（本文中の
# 一部にたまたま含まれるケースを除外するため、文字数チェックと組み合わせて使う）。
_BOT_BLOCK_MAX_CHARS = 1000  # このチェックは短い本文にのみ適用する
_BOT_BLOCK_URL_DOMAINS = (
    "perfdrive.com",       # Radware Bot Manager の人間検証ページ
    "distilnetworks.com",
    "captcha-delivery.com",  # DataDome
    "hcaptcha.com",
)
_BOT_BLOCK_TEXT_PATTERNS = [
    re.compile(r"you are a bot", re.IGNORECASE),
    re.compile(r"verify you are (a )?human", re.IGNORECASE),
    re.compile(r"are you a robot", re.IGNORECASE),
    re.compile(r"unusual traffic", re.IGNORECASE),
    re.compile(r"automated (queries|access)", re.IGNORECASE),
    re.compile(r"access to this page has been denied", re.IGNORECASE),
    re.compile(r"ロボットではないことを確認"),
    re.compile(r"アクセスが拒否されました"),
]


def _detect_bot_block(final_url: str, text: str) -> str | None:
    """本文がBot対策（人間認証・アクセス拒否）ページと判定できれば、その理由文字列を返す。
    該当しなければNone。"""
    domain = urlparse(final_url).netloc.lower()
    for blocked_domain in _BOT_BLOCK_URL_DOMAINS:
        if blocked_domain in domain:
            return f"Bot対策検証ページへのリダイレクトを検知（{blocked_domain}）"
    if text and len(text) <= _BOT_BLOCK_MAX_CHARS:
        for pat in _BOT_BLOCK_TEXT_PATTERNS:
            if pat.search(text):
                return f"Bot対策の定型文を検知（\"{pat.pattern}\"）"
    return None


# JS必須サイト等で本文が描画されず、サイト共通のナビゲーションメニューや
# フッターリンクの羅列だけが「本文」として抽出されてしまうケースの検知
# （2026-09-14、意味不明な本文が表示されるとの指摘対応）。
# Bot対策検知と同じく、短文かつ定型語の複数ヒットのみを対象として誤検知を避ける。
_NAV_MENU_MAX_CHARS = 600
_NAV_MENU_MIN_HITS = 3
_NAV_MENU_WORDS = (
    "ホーム", "会社概要", "お問い合わせ", "サイトマップ", "プライバシーポリシー",
    "利用規約", "採用情報", "ニュース一覧", "検索する", "メニュー", "ログイン",
    "JavaScriptを有効に", "著作権", "広告掲載",
)


def _detect_nav_menu_only(text: str) -> str | None:
    """本文抽出結果が実質的にサイト共通のナビゲーションメニュー・フッターリンクの
    羅列になっているケースを検知する。該当しなければNone。"""
    if not text or len(text) > _NAV_MENU_MAX_CHARS:
        return None
    hits = sum(1 for w in _NAV_MENU_WORDS if w in text)
    if hits >= _NAV_MENU_MIN_HITS:
        return f"本文がナビゲーションメニューの列挙と判定（定型語一致{hits}件）"
    return None


# URL正規化で取り除くトラッキングパラメータ
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = {
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "yclid", "igshid", "ref", "ref_src", "spm", "_ga",
}

# 更新日時（source_updated_at）を拾うためのメタタグ候補
_UPDATED_META_XPATHS = [
    "//meta[@property='article:modified_time']/@content",
    "//meta[@property='og:updated_time']/@content",
    "//meta[@itemprop='dateModified']/@content",
    "//meta[@name='last-modified']/@content",
    "//time[@itemprop='dateModified']/@datetime",
]

# endpoint_typeによる挙動の分岐:
#   単一ページ監視型 = そのURL自体を1件として本文抽出する（ナビゲーションリンクを
#   拾ってしまう問題を避けるため、リンク収集は行わない）
SINGLE_PAGE_ENDPOINT_TYPES = ("サイトトップ", "固定ページ・データベース")
#   一覧ページ型 = ページ内のリンクを収集して個別記事を辿る
LISTING_ENDPOINT_TYPES = ("ニュース・一覧ページ", "検索結果・クエリ")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


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


# ─── 日付ユーティリティ ──────────────────────────────────────────
# 「2026年09月24日」のような和暦区切り表記はdateutilが解釈できず例外になる
# （例: kubota.co.jpのJSON APIが日付をこの形式で返す、2026-10-04確認）。
# dateutilに渡す前にISO風の区切りへ正規化する
_JA_DATE_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")


def _parse_datetime(value):
    """文字列をdatetime(UTC)へ変換する。失敗時はNone"""
    if not value:
        return None
    text = str(value)
    ja_match = _JA_DATE_RE.search(text)
    if ja_match:
        text = f"{ja_match.group(1)}-{ja_match.group(2)}-{ja_match.group(3)}"
    try:
        dt = dateutil_parser.parse(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _within_lookback(pub_dt, lookback_days: int) -> bool:
    """公開日が対象期間内か判定する。

    2026-09-14変更: 日付不明(None)は「除外しない」（対象に含める）。以前は除外していたが、
    trafilaturaが公開日メタデータを抽出できないサイト（例: env.go.jp）で全候補が毎回
    無条件に弾かれ、しかもエラーとして記録されない（run_result='更新なし'のまま）ため、
    クロール自体は成功しているのに記事が一切保存されない不具合が続いていた。
    日付不明の候補を通しても、save_article()側でURL/本文ハッシュによる重複判定が
    既に行われているため、同じ記事を毎回重複保存する心配は無い。"""
    if pub_dt is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return pub_dt >= cutoff


# trafilaturaは本文中のコピーライト年・設立年等の無関係な日付を、記事の公開日と
# 誤って抽出することがある（例: 明確な日付が無いページで2000-01-01を抽出）。
# 誤った日付のまま保存すると記事一覧の日付表示等に誤情報が残るため、明らかに
# 誤りと疑われる日付は「不明」(None)として扱う（2026-09-14以降、日付不明の候補は
# _within_lookbackで除外されず対象に含める方針に変更済みのため、ここでNoneにしても
# 記事が失われることはない）。
_MIN_PLAUSIBLE_ARTICLE_YEAR = 2020


def _parse_content_date(value):
    """trafilaturaが本文から抽出した日付をパースする。実装上の理由は
    _MIN_PLAUSIBLE_ARTICLE_YEAR のコメントを参照"""
    dt = _parse_datetime(value)
    if dt is None:
        return None
    if dt.year < _MIN_PLAUSIBLE_ARTICLE_YEAR:
        return None
    if dt.month == 1 and dt.day == 1:
        # コピーライト表記等から年だけを拾い、月日を1月1日にフォールバックする
        # trafilaturaの挙動を疑う（実例: kirinholdings.comの複数の異なる記事で
        # 発行日が一律「今年の1月1日」として誤抽出され、実際は当日公開の記事なのに
        # lookback判定で「7日以上前」扱いされ除外され続けていた、2026-09-14発見）。
        # 元日ちょうどの発行日は統計的に不自然なほど頻出するため「不明」として扱う。
        return None
    return dt


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _coerce_utc(dt):
    """tzなしdatetimeにUTCを付与する（文書メタデータ由来の日時用）"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ─── URL正規化・canonical URL ────────────────────────────────────
def normalize_url(url: str) -> str:
    """トラッキングパラメータ除去・末尾スラッシュ統一・フラグメント除去でURLを正規化する。
    重複記事URLの判定キー（article_urls.article_url）として使う"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    query_pairs = sorted(
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES) and k.lower() not in _TRACKING_PARAM_NAMES
    )
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query_pairs), ""))


def _extract_canonical_url(tree, final_url: str) -> str:
    """<link rel=canonical> / og:url があればそれを、無ければリダイレクト後URLを返す"""
    if tree is None:
        return final_url
    canonical = tree.xpath("//link[@rel='canonical']/@href")
    if canonical:
        return urljoin(final_url, canonical[0].strip())
    og_url = tree.xpath("//meta[@property='og:url']/@content")
    if og_url:
        return urljoin(final_url, og_url[0].strip())
    return final_url


def _extract_updated_at(tree):
    """更新日時のメタタグを探し、UTCのdatetimeを返す（無ければNone）"""
    if tree is None:
        return None
    for xp in _UPDATED_META_XPATHS:
        values = tree.xpath(xp)
        if values:
            parsed = _parse_datetime(values[0])
            if parsed:
                return parsed
    return None


def _clean_text(text: str) -> str:
    """trafilatura抽出後に残る定型文（Cookie通知・シェア誘導等）を簡易除去する。
    有料会員限定記事の登録・ログイン誘導ブロックは、先に一致箇所以降を丸ごと
    切り捨ててから（_truncate_at_paywall）、残りに通常の行単位除去を適用する"""
    text = _truncate_at_paywall(text)
    if not text:
        return text
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if any(p.search(line) for p in _BOILERPLATE_LINE_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# ─── PDF/Word(.docx) 本文抽出 ─────────────────────────────────────
def _detect_document_kind(url: str, content_type: str = "") -> str:
    """URL拡張子またはContent-Typeから 'pdf' / 'docx' / None を判定する"""
    lower_path = urlparse(url).path.lower()
    if lower_path.endswith(".pdf"):
        return "pdf"
    if lower_path.endswith(".docx"):
        return "docx"
    ct = (content_type or "").split(";")[0].strip().lower()
    return DOCUMENT_MIME_TYPES.get(ct)


def _extract_pdf(content: bytes) -> dict:
    reader = PdfReader(io.BytesIO(content))
    meta = reader.metadata

    def _meta(attr):
        # メタデータの日付が壊れているPDF(例: 伊藤忠の一部のPDFで "Can not convert date")は、
        # 属性の参照でValueErrorになる。本文は読めるので、メタデータだけを諦めて続ける(2026-10-23)
        try:
            return getattr(meta, attr, None) if meta else None
        except Exception:  # noqa: BLE001
            return None

    title = (_meta("title") or "").strip()
    text = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    return {
        "title": title,
        "text": text,
        "published_at": _meta("creation_date"),
        "updated_at": _meta("modification_date"),
    }


def _extract_docx(content: bytes) -> dict:
    doc = DocxDocument(io.BytesIO(content))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    title = (doc.core_properties.title or "").strip() or (paragraphs[0] if paragraphs else "")
    text = "\n\n".join(paragraphs).strip()
    return {
        "title": title,
        "text": text,
        "published_at": doc.core_properties.created,
        "updated_at": doc.core_properties.modified,
    }


def _extract_document(doc_kind: str, content: bytes, final_url: str, status_code) -> dict:
    """PDF/.docxの本文を抽出する。成功時はraw_content(bytes)も含めて返す（article_files保存用）"""
    try:
        parsed = _extract_pdf(content) if doc_kind == "pdf" else _extract_docx(content)
    except Exception as e:
        return {"ok": False, "error": f"文書抽出エラー {type(e).__name__}: {e}", "http_status": status_code}

    text = _clean_text(parsed["text"])
    if not text:
        return {"ok": False, "error": "本文抽出に失敗（空、文書ファイル）", "http_status": status_code}

    return {
        "ok": True,
        "title": parsed["title"],
        "text": text,
        "published_at": _coerce_utc(parsed.get("published_at")),
        "updated_at": _coerce_utc(parsed.get("updated_at")),
        "final_url": final_url,
        "canonical_url": normalize_url(final_url),
        "http_status": status_code,
        "is_document": True,
        "document_kind": doc_kind,
        "raw_content": content,
    }


# ─── ブラウザ取得（Playwright、bot対策サイト向け） ────────────────────
# ブラウザの起動は重いため、プロセス内で1つだけ起動して使い回す（遅延初期化）
_playwright = None
_browser = None


def _get_browser(proxies: dict):
    global _playwright, _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _playwright = sync_playwright().start()
        launch_kwargs = {"headless": True}
        server = (proxies or {}).get("https") or (proxies or {}).get("http")
        if server:
            launch_kwargs["proxy"] = {"server": server}
        _browser = _playwright.chromium.launch(**launch_kwargs)
    return _browser


def close_browser():
    """クロール終了時にブラウザプロセスを片付ける"""
    global _playwright, _browser
    if _browser is not None:
        _browser.close()
        _browser = None
    if _playwright is not None:
        _playwright.stop()
        _playwright = None


def _fetch_with_browser(url: str, proxies: dict, verify: bool):
    """Playwrightで1ページ取得する。(html, final_url, status_code)を返す"""
    browser = _get_browser(proxies)
    context = browser.new_context(
        user_agent=_HEADERS["User-Agent"],
        ignore_https_errors=not verify,
    )
    try:
        page = context.new_page()
        resp = page.goto(url, timeout=BROWSER_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(BROWSER_RENDER_WAIT_MS)
        html = page.content()
        final_url = page.url
        status_code = resp.status if resp else None
        if status_code is not None and status_code >= 400:
            raise Exception(f"HTTPError: {status_code} Client Error for url: {url}")
        return html, final_url, status_code
    finally:
        context.close()


ZYTE_EXTRACT_URL = "https://api.zyte.com/v1/extract"
ZYTE_TIMEOUT = 90  # Browser RenderingはHTTPより時間がかかるため長めに取る
ZYTE_MIN_HTML_CHARS = 500  # これ未満ならHTTP取得を失敗とみなしBrowser Renderingへ切り替える
_zyte_api_key_cache = None


def _env_flag(name: str, default: bool = False, environ=None) -> bool:
    raw = ((environ if environ is not None else os.environ).get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# 2026-10-19: Zyteを一時停止する。ZyteのAPIアカウントが停止されていた
# （応答: 403 /auth/account-suspended。利用上限・請求状況の確認が必要）ため、毎回失敗して
# 時間を使うだけだった。TinyFish Fetch→TinyFish Agentだけで運用する。
# 再開は、Zyteの請求状況を直した上で、環境変数 CRAWL_ZYTE_ENABLED=true を設定する（コード変更不要）。
ZYTE_ENABLED = _env_flag("CRAWL_ZYTE_ENABLED", default=False)


class ZyteDisabledError(RuntimeError):
    """Zyteを停止中（CRAWL_ZYTE_ENABLEDが未設定）に、Zyte経由の取得が要求された"""


def _get_zyte_api_key() -> str:
    """config.jsonのzyte.api_key（推奨）または既存のgeo_intelligence.zyte.api_keyから読む
    （scripts/zyte_poc/zyte_test.pyと同じ読み込み規約）"""
    global _zyte_api_key_cache
    if _zyte_api_key_cache is None:
        config = load_config()
        key = config.get("zyte", {}).get("api_key", "")
        if not key:
            key = config.get("geo_intelligence", {}).get("zyte", {}).get("api_key", "")
        _zyte_api_key_cache = key.strip()
    return _zyte_api_key_cache


ZYTE_TRANSIENT_RETRY_MAX = 2
ZYTE_TRANSIENT_RETRY_DELAY_SEC = 3
# Zyteのレンダリング処理側の一時的な失敗（相手サイト側ではなくZyte基盤側の問題）で、
# 短い待機を挟んだ再試行で解消することが多い（2026-09-16、cofcointernational.com等で
# 12件中1件だけ520になる、といった間欠的な発生を確認）
ZYTE_TRANSIENT_STATUS_CODES = (520, 521, 522, 523, 524)


def _call_zyte(url: str, proxies: dict, verify: bool, browser: bool, wait_sec: int = 5):
    """Zyte APIを1回呼び出す（一時的な5xxはリトライする）。
    (html, final_url, status_code, raw_content)を返す。
    raw_contentはhttpResponseBody取得時のみ生バイト列（PDF/.docx抽出用）、Browser時はNone"""
    if not ZYTE_ENABLED:
        raise ZyteDisabledError("Zyteは停止中です（再開するには環境変数 CRAWL_ZYTE_ENABLED=true）")
    api_key = _get_zyte_api_key()
    if not api_key:
        raise RuntimeError("Zyte APIキーが未設定です(config.jsonのzyte.api_key)")
    if browser:
        # SPA(React/Next.js等)ではページ読み込み直後は「読込中」スピナーのみで、
        # 一覧本体は少し遅れてクライアント側フェッチで描画されることがある
        # （2026-09-16、cdp.netで確認: 待機なしだとスピナーのみ、5秒待つと実記事が出現）
        payload = {"url": url, "browserHtml": True, "actions": [{"action": "waitForTimeout", "timeout": wait_sec}]}
    else:
        payload = {"url": url, "httpResponseBody": True}

    for attempt in range(ZYTE_TRANSIENT_RETRY_MAX + 1):
        resp = requests.post(ZYTE_EXTRACT_URL, auth=(api_key, ""), json=payload,
                              proxies=proxies, verify=verify, timeout=ZYTE_TIMEOUT)
        if resp.status_code in ZYTE_TRANSIENT_STATUS_CODES and attempt < ZYTE_TRANSIENT_RETRY_MAX:
            time.sleep(ZYTE_TRANSIENT_RETRY_DELAY_SEC)
            continue
        break
    resp.raise_for_status()
    data = resp.json()
    status_code = data.get("statusCode", resp.status_code)
    final_url = data.get("url", url)
    if browser:
        return data.get("browserHtml", "") or "", final_url, status_code, None
    body_b64 = data.get("httpResponseBody", "")
    raw_content = base64.b64decode(body_b64) if body_b64 else b""
    return raw_content.decode("utf-8", errors="ignore"), final_url, status_code, raw_content


# SPA(React/Next.js等)がクライアント側フェッチ完了までの間だけ表示するローディング
# プレースホルダーの目印（2026-09-16、cdp.netで確認: HTTP取得(JS未実行)だと本文は
# 数十万文字あってもこのスピナーしか無く、実記事は含まれない）。これが検出された場合は
# 応答が長くてもBrowser Renderingへ強制的にエスカレーションする
_ZYTE_LOADING_PLACEHOLDER_MARKERS = ("animate-spin", "animate-pulse")


def _fetch_with_zyte(url: str, proxies: dict, verify: bool, force_browser: bool = False):
    """Zyte APIでページを取得する。まず安価なHTTP取得を試し、応答が短すぎる、または
    ローディングプレースホルダーの目印しか無い(Bot対策やSPAのクライアント側フェッチ待ちで
    本来のページが返っていない可能性が高い)場合のみ、より高価なBrowser Renderingへ
    フォールバックする。(html, final_url, status_code, raw_content)を返す。

    force_browser=True の場合はこの判定をスキップし常にBrowser Renderingを使う
    (2026-09-17、canada.caで確認: HTTP取得の応答が長くローディングプレースホルダーの
    目印も無いため上記の自動判定ではBrowser Renderingへ進まないが、実際には検索結果が
    クライアント側フェッチで後から描画される構造で、待機時間を延ばしたBrowser
    Renderingでないと個別記事リンクが取得できないサイトが存在するため)"""
    if force_browser:
        html, final_url, status_code, _ = _call_zyte(url, proxies, verify, browser=True, wait_sec=10)
        return html, final_url, status_code, None
    html, final_url, status_code, raw_content = _call_zyte(url, proxies, verify, browser=False)
    looks_like_placeholder = any(m in html for m in _ZYTE_LOADING_PLACEHOLDER_MARKERS)
    if len(html) >= ZYTE_MIN_HTML_CHARS and not looks_like_placeholder:
        return html, final_url, status_code, raw_content
    html2, final_url2, status_code2, _ = _call_zyte(url, proxies, verify, browser=True)
    if len(html2) >= len(html):
        return html2, final_url2, status_code2, None
    return html, final_url, status_code, raw_content


# ─── TinyFish（Zyteが失敗した場合の追加フォールバック、2026-10-09） ──────────
# PoC(scripts/tinyfish_poc/, scripts/zyte_poc/)での検証結果: Bot対策で失敗していた
# 52件の実対象に対し、Zyte・TinyFishともに単独で98.1%(51/52)を解決できたが、
# 「通常クロール→Zyte→TinyFish Fetch→TinyFish Agent」の順でチェーンさせると
# 52/52(100%)を解決でき、かつ最もコストの高いAgentが必要だったのは1件のみだった。
# Zyteは1リクエスト$0.0001〜0.016程度と極めて安価なため、TinyFishは「Zyteも
# 失敗した場合の保険」という位置づけとする（コスト最適化目的ではなく冗長性目的）。
TINYFISH_FETCH_URL = "https://api.fetch.tinyfish.ai"
TINYFISH_AGENT_URL = "https://agent.tinyfish.ai/v1/automation/run-sse"
TINYFISH_FETCH_TIMEOUT = 60
# Agentは自然言語タスク実行のため数分かかることがある。2026-10-19の実測(3件): 成功は22〜51秒、
# 失敗は512秒。失敗1件あたりの損失を減らしたい場合は、環境変数 TINYFISH_AGENT_TIMEOUT_SEC（例: 180）で短縮できる
# ── 2026-10-20 根本原因の修正 ────────────────────────────────────────────────
# TinyFishの仕様: agent_config.max_duration_seconds(実行時間の上限)のデフォルトは「無制限」、
# max_steps(操作回数の上限)のデフォルトは150。従来は両方を指定せずに呼んでいたため、ブロックされた
# ページでAgentが延々と操作を試し続け(実測: 失敗は約512秒)、クライアントが切断してもサーバー側の
# 実行と課金が続く恐れがあった。「こちらで待つのをやめる」のではなく、Agent自身に上限を渡して
# サービス側で止まるようにする。実測(10/19): 成功は22〜51秒。
TINYFISH_AGENT_MAX_DURATION_SEC = int(os.environ.get("TINYFISH_AGENT_MAX_DURATION_SEC") or 180)
# max_stepsはベータ機能で、アカウントが未登録だと403(「Your account is not enabled for custom max steps yet」。
# 2026-10-20に実サービスで確認)。既定は指定しない(0)。ベータ登録後に、環境変数で指定できる
TINYFISH_AGENT_MAX_STEPS = int(os.environ.get("TINYFISH_AGENT_MAX_STEPS") or 0)
TINYFISH_AGENT_CANCEL_URL = "https://agent.tinyfish.ai/v1/runs/{run_id}/cancel"
# クライアント側の見張り(保険)。サービス側の上限(max_duration)に、通信・後処理の余裕を足した値
TINYFISH_AGENT_TIMEOUT = int(os.environ.get("TINYFISH_AGENT_TIMEOUT_SEC") or (TINYFISH_AGENT_MAX_DURATION_SEC + 60))
# イベントが途切れてよい最大の間隔(秒)。requestsのtimeoutは「データが来ない時間」の上限であって
# 全体の所要時間の上限ではないため、全体の上限は_call_tinyfish_agent()内で別に時計で見張る
TINYFISH_AGENT_READ_GAP_SEC = 180
TINYFISH_AGENT_GOAL = (
    "Read this web page and extract the main article. Reply with ONLY a JSON object, no other text. "
    'If you could read the article text: {"status": "success", "title": "<article title>", '
    '"body": "<the full article body text>"}. '
    "If the page is blocked, shows a bot-check / CAPTCHA / 'checking your browser' screen, requires login, "
    "or you could not read the article text for any reason: "
    '{"status": "failure", "reason": "<short reason>"}. '
    "Never put an explanation of a failure into the body field."
)
# ── Agentの上限（2026-10-21変更）──────────────────────────────────────────────
# 従来は「1プロセスあたり10件(バックログは30件)」の先着順で、記事が多い1サイトが上限を使い切り、
# 後ろのサイトにAgentが回らなかった（2026-10-21: 最終失敗40サイトのうち25サイトが未実施）。
# 今は「Agentを使うサイト数」と「1サイトあたりの記事数」で上限を決める（いずれも1プロセスあたり）。
# 環境変数 TINYFISH_AGENT_MAX_SITES / TINYFISH_AGENT_MAX_PER_SITE で変更できる。
TINYFISH_AGENT_MAX_SITES = int(os.environ.get("TINYFISH_AGENT_MAX_SITES") or 50)
TINYFISH_AGENT_MAX_PER_SITE = int(os.environ.get("TINYFISH_AGENT_MAX_PER_SITE") or 10)
# 全体の安全弁（サイト数×1サイトの件数と同じ。--agent-capで、バックログ実行時に小さくもできる）
TINYFISH_AGENT_RUN_CAP = TINYFISH_AGENT_MAX_SITES * TINYFISH_AGENT_MAX_PER_SITE
_tinyfish_agent_site_calls = {}          # crawl_target_id -> このプロセスでAgentを呼んだ回数
_tinyfish_api_key_cache = None
_tinyfish_agent_call_count = 0


def _get_tinyfish_api_key() -> str:
    """config.jsonのapi_keys.tinyfishから読む（scripts/tinyfish_poc/tinyfish_test.pyと同じ規約）"""
    global _tinyfish_api_key_cache
    if _tinyfish_api_key_cache is None:
        config = load_config()
        _tinyfish_api_key_cache = config.get("api_keys", {}).get("tinyfish", "").strip()
    return _tinyfish_api_key_cache


def _tinyfish_agent_budget_available(target_id=None) -> bool:
    """Agentを呼べるか。target_idを渡すと、サイト数の上限と、そのサイトの記事数の上限も見る"""
    if _tinyfish_agent_call_count >= TINYFISH_AGENT_RUN_CAP:
        return False
    if target_id is None:
        return True
    used = _tinyfish_agent_site_calls.get(target_id)
    if used is None:                                    # このプロセスで初めてAgentを使うサイト
        return len(_tinyfish_agent_site_calls) < TINYFISH_AGENT_MAX_SITES
    return used < TINYFISH_AGENT_MAX_PER_SITE


def _record_tinyfish_agent_call(target_id=None) -> None:
    global _tinyfish_agent_call_count
    _tinyfish_agent_call_count += 1
    if target_id is not None:
        _tinyfish_agent_site_calls[target_id] = _tinyfish_agent_site_calls.get(target_id, 0) + 1
    if _tinyfish_agent_call_count == TINYFISH_AGENT_RUN_CAP:
        print(f"[TinyFish Agent] 1回の実行あたりの上限({TINYFISH_AGENT_RUN_CAP}件)に達しました。"
              f"以降はこの実行中エスカレーションしません。")


def _call_tinyfish_fetch(url: str, proxies: dict, verify: bool, want_links: bool = False):
    """TinyFish Fetch APIを1回呼び出す。(html, final_url, status_code)を返す。

    Fetch自体はリンク構造を保持しない読みやすい形式のテキストしか返さないため
    (2026-10-08、TinyFish社に確認済み。ただしlinks=Trueを指定すればページ内の
    リンク一覧を別途取得できる)、タイトル・本文は最小限のHTMLへラップしてから返す。
    want_links=Trueの場合は、取得したリンク一覧を<a href>として同じラップHTMLへ
    追加し、一覧ページのリンク抽出(list_html_candidates)が既存のパース処理
    (tree.xpath("//a/@href"))をそのまま使えるようにする。"""
    api_key = _get_tinyfish_api_key()
    if not api_key:
        raise RuntimeError("TinyFish APIキーが未設定です(config.jsonのapi_keys.tinyfish)")
    payload = {"urls": [url], "format": "markdown"}
    if want_links:
        payload["links"] = True
    resp = requests.post(TINYFISH_FETCH_URL, headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                         json=payload, proxies=proxies, verify=verify, timeout=TINYFISH_FETCH_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("results", [])
    if not items:
        errors = data.get("errors", [])
        raise RuntimeError(f"TinyFish Fetch: 取得失敗({errors[0] if errors else '不明なエラー'})")
    item = items[0]
    final_url = item.get("final_url", url)
    title = item.get("title", "") or ""
    text = item.get("text", "") or ""
    links_html = ""
    if want_links:
        links_html = "".join(f'<a href="{html_escape(link, quote=True)}"></a>' for link in (item.get("links") or []))
    html = (f"<html><head><title>{html_escape(title)}</title></head>"
            f"<body><article>{html_escape(text)}</article>{links_html}</body></html>")
    return html, final_url, 200


# Agentが「取得できなかった」ことを自然文で説明してくる場合の目印（先頭付近に現れたら失敗として扱う）。
# 2026-10-20に、consilium.europa.euの記事2件で、この種の説明文が記事本文として保存されていたのを確認した
_AGENT_FAILURE_PHRASES = (
    "unable to extract", "was unable", "could not extract", "could not retrieve", "unable to access",
    "unable to retrieve", "bot protection", "checking your browser", "captcha", "access denied",
    "blocked by", "i cannot", "i can't", "i could not", "not able to",
)
_AGENT_MIN_BODY_CHARS = 200


def _parse_agent_result(final_result) -> str:
    """Agentの返答(result)から、記事本文のテキストを取り出す。取り出せない(=取得に失敗した)場合は例外。
    - 指示どおりのJSON({"status":"success","title":...,"body":...})が最優先
    - {"status":"failure","reason":...}、または失敗を説明する自然文は、本文として扱わず失敗にする
    - 構造化されていない自然文は、失敗の言い回しが先頭付近に無く、十分な長さがある場合だけ受け入れる"""
    def _as_obj(value):
        if isinstance(value, dict):
            inner = value.get("result")
            if "status" not in value and isinstance(inner, (str, dict)):
                return _as_obj(inner)        # {"result": "..."} の入れ子
            return value
        if isinstance(value, str):
            text = value.strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
            if text.startswith("{"):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    pass
            return text
        return value

    obj = _as_obj(final_result)
    if isinstance(obj, dict):
        if str(obj.get("status", "")).lower() == "failure":
            raise RuntimeError(f"TinyFish Agent: 目標未達（{obj.get('reason') or obj}）")
        body = obj.get("body") or obj.get("text") or obj.get("content")
        if isinstance(body, str) and body.strip():
            title = (obj.get("title") or "").strip()
            text = f"{title}\n\n{body.strip()}" if title else body.strip()
        else:
            raise RuntimeError(f"TinyFish Agent: 本文が空の応答です（{json.dumps(obj, ensure_ascii=False)[:120]}）")
    else:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    head = text[:300].lower()
    if any(p in head for p in _AGENT_FAILURE_PHRASES):
        raise RuntimeError(f"TinyFish Agent: 取得できなかった旨の応答です（{text[:120]!r}）")
    if len(text) < _AGENT_MIN_BODY_CHARS:
        raise RuntimeError(f"TinyFish Agent: 本文が短すぎます（{len(text)}文字）")
    return text


def _cancel_tinyfish_run(run_id, api_key: str, proxies: dict, verify: bool) -> None:
    """中断した実行を、サービス側でも取り消す（取り消さないと、実行と課金が続く恐れがある）。
    取り消しの失敗は、元のエラーを隠さないよう握りつぶす（ベストエフォート）"""
    if not run_id:
        return
    try:
        requests.post(TINYFISH_AGENT_CANCEL_URL.format(run_id=run_id), headers={"X-API-Key": api_key},
                      proxies=proxies, verify=verify, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _call_tinyfish_agent(url: str, proxies: dict, verify: bool):
    """TinyFish Agent APIを1回呼び出す（最後の手段。従量課金のため呼び出し回数はTINYFISH_AGENT_RUN_CAPで抑える）。
    (html, final_url, status_code)を返す。

    2026-10-20: agent_configで、Agent自身に実行時間・操作回数の上限を渡す（上記の定数のコメント参照）。
    また、サービスは「実行は完了したが目標は未達」を COMPLETE + result={"status":"failure","reason":...} で
    返す（仕様: COMPLETEは「インフラが動いた」ことしか保証しない）ため、これを成功として扱わず、
    失敗理由つきのエラーにする。中断した場合は、サービス側の実行も取り消す"""
    api_key = _get_tinyfish_api_key()
    if not api_key:
        raise RuntimeError("TinyFish APIキーが未設定です(config.jsonのapi_keys.tinyfish)")
    final_result = None
    status = None
    run_id = None
    finished = False
    # requestsのtimeout(stream=True)は「次のデータが来るまでの待ち」の上限で、全体の上限ではない。
    # サービス側の上限が効かなかった場合に備えた、クライアント側の見張り（保険）
    deadline = time.monotonic() + TINYFISH_AGENT_TIMEOUT
    agent_config = {"max_duration_seconds": TINYFISH_AGENT_MAX_DURATION_SEC}
    if TINYFISH_AGENT_MAX_STEPS > 0:
        agent_config["max_steps"] = TINYFISH_AGENT_MAX_STEPS
    payload = {"url": url, "goal": TINYFISH_AGENT_GOAL, "agent_config": agent_config}
    try:
        with requests.post(TINYFISH_AGENT_URL, headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                           json=payload, proxies=proxies, verify=verify,
                           timeout=(10, min(TINYFISH_AGENT_TIMEOUT, TINYFISH_AGENT_READ_GAP_SEC)),
                           stream=True) as resp:
            resp.raise_for_status()
            # SSEはUTF-8（仕様）。Content-Typeにcharsetが無いと、requestsはISO-8859-1と誤判定し、
            # 日本語などの非ASCII文字が文字化けする（2026-10-20、失敗理由の日本語が化けて発覚）
            resp.encoding = "utf-8"
            for raw_line in resp.iter_lines(decode_unicode=True):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"TinyFish Agent: {TINYFISH_AGENT_TIMEOUT}秒以内に完了しませんでした")
                if not raw_line:
                    continue
                line = raw_line[len("data:"):].strip() if raw_line.startswith("data:") else raw_line.strip()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "STARTED":
                    run_id = event.get("run_id") or run_id
                if event.get("type") == "COMPLETE":
                    status = event.get("status")
                    final_result = event.get("result")
                    finished = True
                    break
                if event.get("type") == "ERROR":
                    raise RuntimeError(f"TinyFish Agent error: {event.get('message') or event}")
    finally:
        if not finished:
            _cancel_tinyfish_run(run_id, api_key, proxies, verify)
    if status and str(status).upper() != "COMPLETED":
        raise RuntimeError(f"TinyFish Agent: 実行が{status}で終了しました（{final_result or event.get('error')}）")
    if final_result is None:
        raise RuntimeError(f"TinyFish Agent: 完了イベント未受信(最終status={status})")
    text = _parse_agent_result(final_result)
    html = f"<html><body><article>{html_escape(text)}</article></body></html>"
    return html, url, 200


def _fetch_with_escalation(url: str, proxies: dict, verify: bool, *, start_with_zyte: bool = False,
                            force_zyte_browser: bool = False, want_links: bool = False,
                            allow_tinyfish_agent: bool = False, allow_escalation: bool = True):
    """通常HTTP取得を軸に、失敗時は Zyte → TinyFish Fetch → (allow_tinyfish_agent時のみ)
    TinyFish Agent の順に自動でエスカレーションする。(html, final_url, status_code, raw_content)を返す
    （raw_contentはZyteのHTTPモードで取得できた場合のみ値が入る、それ以外はNone）。

    明確な404(ページが存在しない)はエスカレーションしない。コストのかかる手段を
    存在しないページに使っても無駄なため（通常取得のみで判定、Zyte/TinyFish側の
    404はそのまま空に近い結果として返る想定で、そこから先はエスカレーションしない
    設計にはしていない＝通常取得の時点での404だけを見る）。

    start_with_zyte=Trueの場合は通常取得を省略しZyteから開始する
    (crawl_method='Zyte'等、既に通常取得が機能しないと分かっている対象向け、
    無駄な失敗リクエストを省く。以前からの挙動を維持)。

    allow_escalation=Falseの場合、対象本来の手段(start_with_zyte=Falseなら通常HTTP、
    Trueなら最初からZyte)を1回試すだけで、失敗時にそれ以上のフォールバックを行わず
    例外をそのまま送出する(2026-10-14追加: 一覧ページ取得(list_html_candidates等)を
    Pass1では軽量な1回試行に留め、必要な場合だけPass2でまとめてフルエスカレーションする
    ため。以前は一覧取得だけがこの関数経由で常にフルエスカレーションしており、
    bot対策サイトの一覧取得待ちがPass1内で同期的に発生し他targetの処理を圧迫していた)"""
    last_exc = None
    if not start_with_zyte:
        try:
            resp = _requests_get_with_retry(url, proxies, verify, REQUEST_TIMEOUT, _HEADERS)
            resp.raise_for_status()
            return _decoded_html(resp), resp.url, resp.status_code, resp.content
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise
            last_exc = e
        except Exception as e:
            last_exc = e
        if not allow_escalation:
            raise last_exc
    elif not allow_escalation:
        return _fetch_with_zyte(url, proxies, verify, force_browser=force_zyte_browser)

    if ZYTE_ENABLED:  # 停止中は、失敗が確定している呼び出しで時間を使わずTinyFishへ進む
        try:
            html, final_url, status_code, raw_content = _fetch_with_zyte(url, proxies, verify,
                                                                          force_browser=force_zyte_browser)
            return html, final_url, status_code, raw_content
        except Exception as e:
            last_exc = e

    try:
        html, final_url, status_code = _call_tinyfish_fetch(url, proxies, verify, want_links=want_links)
        return html, final_url, status_code, None
    except Exception as e:
        last_exc = e

    if allow_tinyfish_agent and _tinyfish_agent_budget_available():
        html, final_url, status_code = _call_tinyfish_agent(url, proxies, verify)
        _record_tinyfish_agent_call()
        return html, final_url, status_code, None

    raise last_exc


def _decoded_html(resp: requests.Response) -> str:
    """Content-TypeヘッダーにcharsetがないHTML（<meta charset>頼みのページ）では、
    requestsがHTTP仕様のデフォルトであるISO-8859-1と誤判定し文字化けすることがあるため、
    その場合は実データから判定するapparent_encodingを優先する"""
    content_type = resp.headers.get("Content-Type", "")
    if "charset" not in content_type.lower() and resp.apparent_encoding:
        resp.encoding = resp.apparent_encoding
    return resp.text


def _safe_lxml_parse(html: str):
    """デコード済み文字列の先頭にXML宣言(<?xml ... encoding=...?>)が残っていると、
    lxmlは「Unicode strings with encoding declaration are not supported」で例外を
    出す（デコード済みなので宣言のencodingは既に無関係）。パース前に宣言を除去する"""
    html = re.sub(r"^\s*<\?xml[^>]*\?>", "", html, count=1)
    return lxml_html.fromstring(html)


_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\']\s*(\d+)\s*;\s*url=([^"\']+)',
    re.IGNORECASE,
)
META_REFRESH_MAX_WAIT_SEC = 10  # これを超える待機秒数はユーザー向け案内ページの可能性が高く追従しない
META_REFRESH_MAX_HOPS = 2


def _follow_meta_refresh(html: str, final_url: str):
    """<meta http-equiv="refresh" content="N;URL=..."> による転送を追従する
    (2026-09-17、mlit.go.jpで確認: target_urlが当月の実ページへ0秒refreshするだけの
    中継ページで、これを追従しないと候補記事が一切取れなかった)。
    転送先が無い/待機秒数が長い場合はそのまま返す(呼び出し元は通常のhtmlとして扱う)"""
    m = _META_REFRESH_RE.search(html)
    if not m or int(m.group(1)) > META_REFRESH_MAX_WAIT_SEC:
        return html, final_url, False
    return html, urljoin(final_url, m.group(2).strip()), True


# 社内プロキシ越しの通信で稀に発生するSSL瞬断(SSLEOFError等)・接続断に対して、
# 短い間隔でリトライする(2026-09-30、nature.comでSSLError多発かつ翌日は成功、を
# 交互に繰り返しており一過性のTLS切断と判断。requests.get()呼び出し全般に適用する)
HTTP_FETCH_MAX_RETRIES = 2
HTTP_FETCH_RETRY_DELAY_SEC = 3
_HTTP_RETRYABLE_EXCEPTIONS = (requests.exceptions.SSLError, requests.exceptions.ConnectionError)


def _requests_get_with_retry(url: str, proxies: dict, verify: bool, timeout: int, headers: dict):
    last_exc = None
    for attempt in range(HTTP_FETCH_MAX_RETRIES + 1):
        try:
            return requests.get(url, proxies=proxies, verify=verify, timeout=timeout, headers=headers)
        except _HTTP_RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt < HTTP_FETCH_MAX_RETRIES:
                time.sleep(HTTP_FETCH_RETRY_DELAY_SEC)
    raise last_exc


def _fetch_page(url: str, proxies: dict, verify: bool, use_browser: bool, use_zyte: bool = False,
                 force_zyte_browser: bool = False, want_links: bool = False, allow_escalation: bool = True):
    """通常HTTP・ブラウザ・Zyte API・TinyFishのいずれかでページを取得する。
    (html, final_url, status_code)を返す。meta refreshによる転送(短時間のもののみ)は自動で追従する。

    use_browser=Falseの場合、取得失敗時は_fetch_with_escalation()によりZyte→TinyFish Fetchへ
    自動でエスカレーションする(2026-10-09追加)。want_links=Trueを渡すと、TinyFish Fetch経由で
    解決した場合にページ内のリンク一覧を<a href>として埋め込んだHTMLを返す
    (一覧ページのリンク抽出=list_html_candidates向け)。allow_escalation=Falseは
    _fetch_with_escalation()へそのまま伝播する(2026-10-14追加、list_html_candidates参照)。"""
    if use_browser:
        html, final_url, status_code = _fetch_with_browser(url, proxies, verify)
    else:
        html, final_url, status_code, _ = _fetch_with_escalation(
            url, proxies, verify, start_with_zyte=use_zyte, force_zyte_browser=force_zyte_browser,
            want_links=want_links, allow_escalation=allow_escalation)

    for _ in range(META_REFRESH_MAX_HOPS):
        html, next_url, followed = _follow_meta_refresh(html, final_url)
        if not followed or next_url == final_url:
            break
        final_url = next_url
        if use_browser:
            html, final_url, status_code = _fetch_with_browser(final_url, proxies, verify)
        else:
            html, final_url, status_code, _ = _fetch_with_escalation(
                final_url, proxies, verify, start_with_zyte=use_zyte, force_zyte_browser=force_zyte_browser,
                want_links=want_links, allow_escalation=allow_escalation)
    return html, final_url, status_code


def _fetch_page_with_tier(url: str, proxies: dict, verify: bool, tier: str, want_links: bool = False):
    """ページ取得を、指定された1段階(tier: "tinyfish_fetch"/"zyte"/"tinyfish_agent")だけで
    試す。(html, final_url, status_code)を返す。

    2026-10-15追加: crawl_method='Zyte'のtargetは、通常のHTTP取得が機能しないと
    既に分かっているため、Pass1では一切試行せずPass2のエスカレーション
    (escalate_pending_listings)へ丸ごと回送する方式に変更した。そのPass2側で、
    記事単位のエスカレーション(ESCALATION_TIERS)と同じ順序(TinyFish Fetch→Zyte→Agent)で
    一覧取得も試せるようにするための、tier指定版の_fetch_page()"""
    if tier == "tinyfish_fetch":
        html, final_url, status_code = _call_tinyfish_fetch(url, proxies, verify, want_links=want_links)
    elif tier == "zyte":
        html, final_url, status_code, _ = _fetch_with_zyte(url, proxies, verify)
    elif tier == "tinyfish_agent":
        html, final_url, status_code = _call_tinyfish_agent(url, proxies, verify)
    else:
        raise ValueError(f"未対応のtier: {tier}")
    return html, final_url, status_code


# 社内プロキシ越しの通信が稀に瞬断・破損し、UTF-8バイト列がLatin-1として誤デコード
# されたような文字化けタイトル・本文がそのまま保存されてしまうことがある
# （sustainability_dashboard_core._looks_garbledと同種のヒューリスティック。
# 取得直後にここで検知してリトライすることで、破損データがDBに入る前に防ぐ）
ENCODING_CORRUPTION_MAX_RETRIES = 2
ENCODING_CORRUPTION_RETRY_DELAY_SEC = 2
_ENCODING_CORRUPTION_PAT = re.compile("[ -ÿ]")

def _looks_encoding_corrupted(text: str, threshold: float = 0.2) -> bool:
    if not text:
        return False
    return len(_ENCODING_CORRUPTION_PAT.findall(text)) / len(text) > threshold


# ─── 記事本文抽出（RSS・HTML共通） ────────────────────────────────
def extract_article(url: str, proxies: dict, verify: bool, use_browser: bool = False,
                     use_zyte: bool = False, tier: str = None) -> dict:
    """記事URLから本文・タイトル・公開日を抽出する。PDF/.docxは専用抽出、
    それ以外はtrafilaturaでHTMLから抽出する。取得結果のタイトル・本文が
    文字化けして見える場合、ENCODING_CORRUPTION_MAX_RETRIES回まで再取得を試みる
    （プロキシ瞬断による破損を、リトライで正常な応答に置き換えるため）。

    tier: Noneなら従来通りuse_browser/use_zyteに従う。"zyte"/"tinyfish_fetch"/
    "tinyfish_agent"を指定すると、その手段だけを1回試す(main()のPass2用、
    _extract_article_once()参照)"""
    for attempt in range(ENCODING_CORRUPTION_MAX_RETRIES + 1):
        result = _extract_article_once(url, proxies, verify, use_browser, use_zyte=use_zyte, tier=tier)
        if not result.get("ok"):
            return result
        if not (_looks_encoding_corrupted(result.get("title", "")) or
                _looks_encoding_corrupted(result.get("text", "")[:500])):
            return result
        if attempt < ENCODING_CORRUPTION_MAX_RETRIES:
            time.sleep(ENCODING_CORRUPTION_RETRY_DELAY_SEC)
    result["encoding_suspect"] = True  # 全リトライ後も文字化けが疑われる（呼び出し元の判断に委ねる）
    return result


def _extract_article_once(url: str, proxies: dict, verify: bool, use_browser: bool = False,
                           use_zyte: bool = False, tier: str = None) -> dict:
    """tierを指定すると、use_browser/use_zyteによらずその手段を1回だけ試す
    （tier: "zyte" / "tinyfish_fetch" / "tinyfish_agent"）。tier未指定(None)なら
    従来通りuse_browser/use_zyteに従って1回だけ取得する。

    2026-10-13変更: 以前はこの関数内でZyte→TinyFish Fetch→Agentの順に自動で
    エスカレーションしていたが、1target内に失敗記事が複数あると、それぞれが
    独立してこのチェーンを最後まで通してしまい、1targetの処理に1時間以上
    かかって他targetの処理を圧迫する問題が見つかった(2026-10-09〜12の本番実測)。
    そのため、エスカレーション(段階的な再試行)は呼び出し元(main()のPass2、
    escalate_pending()参照)で、全target分の失敗をまとめてから行う方式に変更し、
    この関数自体は「指定された1手段だけを試す」単純な形に戻した。"""
    doc_kind = _detect_document_kind(url)
    if doc_kind:
        use_browser = False  # 文書ファイルはブラウザ経由で扱わない（PDFビューア化を避ける）
        use_zyte = False     # 文書ファイルはZyte経由でも扱わない（httpResponseBodyで代用可能だが未検証のため通常取得を使う）
        tier = None

    try:
        if tier == "tinyfish_fetch":
            html, final_url, status_code = _call_tinyfish_fetch(url, proxies, verify, want_links=False)
            content_type, raw_content = "", None
        elif tier == "tinyfish_agent":
            html, final_url, status_code = _call_tinyfish_agent(url, proxies, verify)
            content_type, raw_content = "", None
        elif tier == "zyte" or use_zyte:
            html, final_url, status_code, raw_content = _fetch_with_zyte(url, proxies, verify)
            content_type = ""
        elif use_browser:
            html, final_url, status_code = _fetch_with_browser(url, proxies, verify)
            content_type, raw_content = "", None
        else:
            resp = _requests_get_with_retry(url, proxies, verify, REQUEST_TIMEOUT, _HEADERS)
            resp.raise_for_status()
            html, final_url, status_code = _decoded_html(resp), resp.url, resp.status_code
            content_type, raw_content = resp.headers.get("Content-Type", ""), resp.content
    except Exception as e:
        status_code = e.response.status_code if isinstance(e, requests.exceptions.HTTPError) and e.response is not None else None
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "http_status": status_code}

    if not doc_kind:
        doc_kind = _detect_document_kind(final_url, content_type)
    if doc_kind:
        if raw_content is None:
            return {"ok": False, "error": "文書ファイルはブラウザ経由での取得に未対応です", "http_status": status_code}
        return _extract_document(doc_kind, raw_content, final_url, status_code)

    try:
        tree = _safe_lxml_parse(html)
    except Exception:
        tree = None

    try:
        json_str = trafilatura.extract(
            html, url=url, output_format="json",
            with_metadata=True, favor_recall=True,
        )
    except Exception as e:
        return {"ok": False, "error": f"抽出処理エラー {type(e).__name__}: {e}", "http_status": status_code}

    if not json_str:
        return {"ok": False, "error": "本文抽出に失敗（対象外ページの可能性）", "http_status": status_code}

    data = json.loads(json_str)
    text = _clean_text((data.get("text") or "").strip())
    if not text:
        return {"ok": False, "error": "本文抽出に失敗（空）", "http_status": status_code}

    bot_block_reason = _detect_bot_block(final_url, text)
    if bot_block_reason:
        return {"ok": False, "error": bot_block_reason, "http_status": status_code}

    nav_menu_reason = _detect_nav_menu_only(text)
    if nav_menu_reason:
        return {"ok": False, "error": nav_menu_reason, "http_status": status_code}

    canonical_url = normalize_url(_extract_canonical_url(tree, final_url))

    return {
        "ok": True,
        "title": data.get("title") or "",
        "text": text,
        "published_at": _parse_content_date(data.get("date")),
        "updated_at": _extract_updated_at(tree),
        "final_url": final_url,
        "canonical_url": canonical_url,
        "http_status": status_code,
        "is_document": False,
    }


# ─── RSS ────────────────────────────────────────────────────────
def list_rss_candidates(target: dict, proxies: dict, verify: bool, allow_escalation: bool = True) -> list:
    """RSSフィードから候補記事(URL・仮タイトル・公開日)を一覧する。

    2026-09-14: extract_article()側の文字化けリトライ（プロキシ瞬断対策）は
    本文抽出(trafilatura)のtitleにしか適用されておらず、それが空で
    このRSS仮タイトルへフォールバックする記事（後述crawl_articles()の
    `extracted["title"] or cand.get("title")`）では文字化けチェックが
    一切行われていなかった。実際に厚労省・防衛省RSS由来の記事で
    「repair_mojibakeでも復元不能な文字化けタイトル」が継続的に発生していたのは
    これが原因だったため、同じ考え方でRSSフィード取得自体をリトライする。

    allow_escalation=Falseは_list_rss_candidates_once()へそのまま伝播し、
    RSS取得失敗時のZyteへの自動フォールバックを行わない
    (2026-10-14追加、process_target()のPass1から呼ぶ場合用)"""
    for attempt in range(ENCODING_CORRUPTION_MAX_RETRIES + 1):
        candidates, status_code = _list_rss_candidates_once(target, proxies, verify,
                                                              allow_escalation=allow_escalation)
        if not any(_looks_encoding_corrupted(c["title"]) for c in candidates):
            return candidates, status_code
        if attempt < ENCODING_CORRUPTION_MAX_RETRIES:
            time.sleep(ENCODING_CORRUPTION_RETRY_DELAY_SEC)
    return candidates, status_code  # 全リトライ後もダメならそのまま返す(呼び出し元でextracted["title"]優先により救済される場合がある)


# ── RSSの要約での代用保存（2026-10-23追加）────────────────────────────────────────
# FT・McKinsey・Reuters・Council of the EU・ICG等は、記事ページがBot対策で、Fetch・Agentでも取得できない。
# しかし、RSSには、題名と要約(150〜300字)がある。ニューラルは、この程度の情報から、記事を選んでいる。
# 全段階で本文を取得できず、最終失敗した記事に限り、フィードの題名＋要約を、記事として保存する（補った内容は足さない）。
# 保存した記事は、extraction_errorに RSS_SUMMARY_ONLY を記録し、分析では「要約のみ」と明示し、人の確認が必要(needs_review)にする。
# 後で本文を取得できれば、既存の仕組み(本文が違えば新版)で、自動で全文に置き換わる。
RSS_SUMMARY_ONLY_MARK = "RSS_SUMMARY_ONLY"
RSS_SUMMARY_MIN_CHARS = 80       # これ未満の要約は、記事として使えないため、代用しない
RSS_SUMMARY_MAX_CHARS = 1500


def _rss_entry_summary(entry) -> str:
    """フィードの1項目から、要約のテキストを取り出す(HTMLタグを除き、空白を整える)。content:encodedが、より長ければそちらを使う"""
    import html as _html
    parts = [entry.get("summary") or entry.get("description") or ""]
    for c in entry.get("content") or []:
        parts.append(c.get("value") or "")
    best = max((re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", p))).strip() for p in parts), key=len, default="")
    return best[:RSS_SUMMARY_MAX_CHARS]


def _list_rss_candidates_once(target: dict, proxies: dict, verify: bool, allow_escalation: bool = True) -> list:
    """RSS取得。素のHTTPがBot対策等で失敗した場合はZyte経由に自動フォールバックする
    (2026-09-17、unwater.org/stand.earth等で確認: 素のrequestsは403でも、Zyteの
    HTTPモード経由なら普通に取得できるケースがあった)。
    allow_escalation=Falseの場合はこのZyteフォールバックを行わず、例外をそのまま送出する"""
    try:
        resp = requests.get(target["target_url"], proxies=proxies, verify=verify,
                             timeout=REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        content, final_url, status_code = resp.content, resp.url, resp.status_code
    except requests.exceptions.RequestException:
        if not allow_escalation:
            raise
        html, final_url, status_code, raw = _fetch_with_zyte(target["target_url"], proxies, verify)
        content = raw if raw else html.encode("utf-8", errors="ignore")
    feed = feedparser.parse(content)

    candidates = []
    for entry in feed.entries[:50]:
        link = entry.get("link", "")
        title = entry.get("title", "").strip()
        if not link or not title:
            continue
        # <link>が相対パスのRSSフィード（例: 防衛省, ファーストリテイリング）向けに
        # フィード自身のURL（リダイレクト後のfinal_url）を基準に絶対URLへ解決する
        link = urljoin(final_url, link)
        pp = entry.get("published_parsed") or entry.get("updated_parsed")
        pub_dt = datetime(*pp[:6], tzinfo=timezone.utc) if pp else None
        candidates.append({"url": link, "title": title, "published_at": pub_dt, "summary": _rss_entry_summary(entry)})
    return candidates, status_code


# ─── XMLサイトマップ ─────────────────────────────────────────────
_URL_DATE_RE = re.compile(r"/((?:19|20)\d{2})/(\d{1,2})(?:/(\d{1,2}))?(?=/|$)")


def _sitemap_sort_key(pub_dt, path: str):
    """サイトマップの候補を、新しい順に並べるための日時。lastmodがあればそれを、無ければURLの中の日付
    (/2026/10/ や /2026/10/20/)を使う（並べ替えにだけ使い、記事の公開日としては扱わない）。日付が無ければNone"""
    if pub_dt is not None:
        return pub_dt
    m = _URL_DATE_RE.search(path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


SITEMAP_MAX_CHILDREN = 40      # サイトマップ索引から読む、子サイトマップの上限（lastmodが新しい順。search_conditionsの max_children:N で変更可）


def _clean_loc(text: str) -> str:
    """<loc>の中身。CDATAで包まれている場合(例: WBCSDの索引)も、取り除く"""
    return re.sub(r"<!\[CDATA\[|\]\]>", "", text).strip()


def _fetch_sitemap_text(url: str, proxies: dict, verify: bool) -> str:
    resp = requests.get(url, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    if url.lower().endswith(".gz"):                 # 圧縮された子サイトマップ(.xml.gz)
        import gzip
        return gzip.decompress(resp.content).decode("utf-8", errors="replace")
    return resp.text


def _parse_lastmod(text):
    try:
        d = dateutil_parser.parse(text.strip())
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def list_sitemap_candidates(target: dict, proxies: dict, verify: bool) -> list:
    """XMLサイトマップ(<url><loc>...</loc><lastmod>...</lastmod></url>)から候補記事を
    一覧する。<a href>を汎用収集するlist_html_candidates()とは構造が異なる
    （例: Paboco等、更新頻度は低いがJS一覧の代わりにsitemap.xmlで全ページを把握できるサイト向け）

    2026-10-23: 従来は、ファイルの先頭から上限(既定30件)まで取っていたため、古い順に並ぶサイトマップ
    (ISSB/IFRS: 2018年〜。ニュース記事2,135件)では、古い30件しか見ず、新しい記事に届かなかった。
    条件に合う候補をすべて集め、新しい順(lastmod、無ければURLの日付)に並べてから、上限を適用する。
    日付がまったく無いサイトマップは、従来どおり、ファイルの順で上限まで取る

    2026-10-23: サイトマップ索引(<sitemapindex>。EPA=38件の子、WBCSD、IEAなど)に対応。子サイトマップを
    lastmodの新しい順に、最大max_children件(既定40)読み、全体を新しい順に並べてから、上限を適用する。
    子の取得に失敗しても、他の子で続ける"""
    url = target["target_url"]
    domain = target.get("domain") or urlparse(url).netloc
    include_paths = [p.strip() for p in (target.get("include_paths") or "").split(",") if p.strip()]
    exclude_paths = [p.strip() for p in (target.get("exclude_paths") or "").split(",") if p.strip()]

    resp = requests.get(url, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()

    root_text = resp.text
    if "<sitemapindex" in root_text:
        children = []
        for sm in re.findall(r"<sitemap>(.*?)</sitemap>", root_text, re.S):
            m = re.search(r"<loc>(.*?)</loc>", sm, re.S)
            if m:
                lm = re.search(r"<lastmod>(.*?)</lastmod>", sm)
                children.append((_clean_loc(m.group(1)), _parse_lastmod(lm.group(1)) if lm else None))
        if any(c[1] for c in children):             # lastmodが新しい順（無いものは最後。同じ時刻は、索引の順）
            children = sorted(children, key=lambda c: (c[1] is None, -(c[1].timestamp() if c[1] else 0)))
        try:
            limit = int(_search_setting(target, "max_children", SITEMAP_MAX_CHILDREN))
        except ValueError:
            limit = SITEMAP_MAX_CHILDREN
        blocks = []
        for child_url, _ in children[:limit]:
            try:
                blocks.extend(re.findall(r"<url>(.*?)</url>", _fetch_sitemap_text(child_url, proxies, verify), re.S))
            except Exception:       # 子の1つが失敗しても、他の子で続ける
                continue
    else:
        blocks = re.findall(r"<url>(.*?)</url>", root_text, re.S)

    candidates = []
    sort_keys = []
    for block in blocks:
        loc_match = re.search(r"<loc>(.*?)</loc>", block, re.S)
        if not loc_match:
            continue
        loc = _clean_loc(loc_match.group(1))
        parsed = urlparse(loc)
        if not parsed.scheme.startswith("http") or domain not in parsed.netloc:
            continue
        path = parsed.path
        if path.lower().endswith(NON_ARTICLE_EXTENSIONS):
            continue
        if include_paths and not any(_path_matches(path, p) for p in include_paths):
            continue
        if exclude_paths and any(_path_matches(path, p) for p in exclude_paths):
            continue

        lastmod_match = re.search(r"<lastmod>(.*?)</lastmod>", block)
        pub_dt = _parse_lastmod(lastmod_match.group(1)) if lastmod_match else None

        candidates.append({"url": loc, "title": None, "published_at": pub_dt})
        sort_keys.append(_sitemap_sort_key(pub_dt, path))

    max_links = _target_max_links(target)
    if any(k is not None for k in sort_keys):
        order = sorted(range(len(candidates)), key=lambda i: (sort_keys[i] is None, -(sort_keys[i].timestamp() if sort_keys[i] else 0), i))
        candidates = [candidates[i] for i in order]      # 新しい順。日付の無いものは、最後（ファイルの順）
    return candidates[:max_links], resp.status_code


# ─── HTML（汎用） ─────────────────────────────────────────────────
def _path_matches(path: str, pattern: str) -> bool:
    """include_paths/exclude_pathsの1パターンに対する一致判定。
    先頭が"*"の場合は部分一致(pathのどこかにpattern[1:]が含まれるか)、
    それ以外は従来通りの前方一致(startswith)。
    （2026-09-17、canada.caで追加: 記事URLが/en/<部署名>/news/2026/09/<slug>.html
    のように可変の部署名セグメントを挟むサイトでは前方一致だけでは絞り込めないため）"""
    if pattern.startswith("*"):
        return pattern[1:] in path
    return path.startswith(pattern)


def _search_setting(target: dict, key: str, default=None):
    """search_conditions列の「キー:値」の指定を読む。複数ある場合は「;」区切り（例: max_links:60;max_children:50）。
    従来の単独指定(max_links:30)も、そのまま読める（2026-10-23: サイトマップ索引の子の上限を足すため、複数指定に対応）"""
    for part in re.split(r"[;；]", target.get("search_conditions") or ""):
        part = part.strip()
        if part.startswith(key + ":"):
            return part.split(":", 1)[1].strip()
    return default


def _target_max_links(target: dict) -> int:
    """search_conditions列に"max_links:N"形式の指定があれば、このターゲットの一覧候補上限として
    使う（既定はMAX_HTML_LINKS_PER_TARGET=30）。2026-10-01、環境省(水規制)で確認:
    一覧ページがカテゴリ別に記事を束ねているだけで日付順になっておらず、新着記事が
    カテゴリの並び順次第で既定の上限30件を超えた位置に追いやられ取りこぼされていた。
    全ターゲット共通の既定値を引き上げると全HTML方式ターゲットのクロール負荷が増えるため、
    このsearch_conditions指定で対象ターゲットだけ上限を緩和できるようにする
    （同列はforce_zyte_browserフラグとしても流用済みのため、形式が重複しないよう
    "max_links:"プレフィックスで判別する）"""
    value = _search_setting(target, "max_links")
    if value:
        try:
            return int(value)
        except ValueError:
            pass
    return MAX_HTML_LINKS_PER_TARGET


def list_html_candidates(target: dict, proxies: dict, verify: bool, use_browser: bool = False,
                          use_zyte: bool = False, allow_escalation: bool = True, tier: str = None) -> list:
    """HTML一覧ページから候補記事リンクを抽出する（<a>を汎用的に収集し、
    include_paths/exclude_paths・ドメイン一致で絞り込む）。
    allow_escalation=Falseは_fetch_page()経由で_fetch_with_escalation()へ伝播し、
    一覧取得失敗時のZyte/TinyFishへの自動フォールバックを行わない
    (2026-10-14追加、process_target()のPass1から呼ぶ場合用)。

    tierを指定すると、use_browser/use_zyte/allow_escalationによらず_fetch_page_with_tier()で
    その1段階だけを試す(2026-10-15追加。crawl_method='Zyte'のtargetをPass2で
    ESCALATION_TIERS順に再試行するescalate_pending_listings()から使う)"""
    url = target["target_url"]
    domain = target.get("domain") or urlparse(url).netloc
    include_paths = [p.strip() for p in (target.get("include_paths") or "").split(",") if p.strip()]
    exclude_paths = [p.strip() for p in (target.get("exclude_paths") or "").split(",") if p.strip()]

    if tier is not None:
        html, final_url, status_code = _fetch_page_with_tier(url, proxies, verify, tier, want_links=True)
    else:
        # search_conditions列を「Zyte強制ブラウザレンダリング」フラグとして流用
        # (2026-09-17、canada.caで確認: 通常のZyte自動判定ではBrowser Renderingへ
        # エスカレーションされないが、実際には検索結果がクライアント側フェッチで
        # 後から描画されるため長めの待機付きBrowser Renderingが必須なサイト向け)
        force_zyte_browser = (target.get("search_conditions") == "force_zyte_browser")
        html, final_url, status_code = _fetch_page(url, proxies, verify, use_browser, use_zyte=use_zyte,
                                                    force_zyte_browser=force_zyte_browser, want_links=True,
                                                    allow_escalation=allow_escalation)
    max_links = _target_max_links(target)

    tree = _safe_lxml_parse(html)
    # <base href="..."> がある場合、相対リンクはfinal_urlではなくこちらを基準に解決する
    # (例: mnd.gov.twは<base href="/">を指定しており、これを無視すると相対リンクが
    # 現在ページのパス配下に誤って連結されてしまう=urljoinの標準動作とHTML仕様の齟齬)
    base_href = next(iter(tree.xpath("//base/@href")), None)
    link_base = urljoin(final_url, base_href) if base_href else final_url
    # 一覧ページ自身が候補に混入するのを防ぐ自己リンク判定。target_url(DB登録値)が
    # bare domainでもサイト側がwwwへリダイレクトすることがあり(例:
    # coca-colacompany.com -> www.coca-colacompany.com)、その場合はurlとの単純比較
    # では一致せず素通りしてしまう(2026-10-04、Coca-Cola Company等で確認)。
    # final_url側も同じ正規化をして自己リンク集合に加える
    self_urls = {url.rstrip("/")}
    final_parsed = urlparse(final_url)
    self_urls.add(f"{final_parsed.scheme}://{final_parsed.netloc}{final_parsed.path.rstrip('/') or '/'}")
    seen = set()
    candidates = []
    excluded_like = []      # include_pathsで除外された、記事らしいリンク（サイレント故障の検知用）
    for href in tree.xpath("//a/@href"):
        full = urljoin(link_base, href)
        parsed = urlparse(full)
        if not parsed.scheme.startswith("http"):
            continue
        if domain not in parsed.netloc:
            continue
        path = _strip_aem_content_prefix(parsed.path)
        # WWF(panda.org)のような旧CMSは記事識別子自体をクエリに埋め込む
        # （例: /?15703966/slug）ため、パスが空でクエリが数字始まりのものは
        # 通常のinclude_paths/exclude_paths判定（パスベース）を素通りさせて
        # 無条件で記事候補として扱う（このパターン以外はパスが空でも記事ではないため対象外）
        is_query_id_article = (path.rstrip("/") or "/") == "/" and re.match(r"^\d+/", parsed.query)
        if not is_query_id_article:
            if path.lower().endswith(NON_ARTICLE_EXTENSIONS):
                continue
            if include_paths and not any(_path_matches(path, p) for p in include_paths):
                if _looks_article_like(path) and not _is_non_article_url(parsed, path, []):
                    excluded_like.append(path)
                continue
            if exclude_paths and any(_path_matches(path, p) for p in exclude_paths):
                continue
            if _is_non_article_url(parsed, path, include_paths):
                continue
        normalized_path = path.rstrip("/") or "/"
        if is_query_id_article:
            normalized = f"{parsed.scheme}://{parsed.netloc}/?{parsed.query}"
        else:
            normalized = f"{parsed.scheme}://{parsed.netloc}{normalized_path}"
        if normalized in seen or normalized in self_urls:
            continue
        seen.add(normalized)
        candidates.append({"url": normalized, "title": None, "published_at": None})
        if len(candidates) >= max_links:
            break
    # 条件に合う記事が0件なのに、記事らしいリンクが複数ある場合は、統計を残す（funnelに警告として記録される）
    target.pop("_listing_stats", None)
    if not candidates and len(excluded_like) >= INCLUDE_NOMATCH_MIN_LINKS:
        top = Counter("/" + "/".join([x for x in p.split("/") if x][:2]) for p in excluded_like)
        target["_listing_stats"] = {"include_nomatch": len(excluded_like), "top": [k for k, _ in top.most_common(3)]}
    return candidates, status_code


def _strip_aem_content_prefix(path: str) -> str:
    """AEM(Adobe Experience Manager)サイトは内部コンテンツパス(/content/<サイト名>/...)を
    <a href>やJSON APIレスポンスにそのまま露出することがあるが、公開URLはこのプレフィックスを
    含まない（例: /content/ifrs/home/news-and-events/x.html → /home/news-and-events/x.html）。
    2026-09-29、ifrs.org(ISSB)で確認: list_html_candidates()側はこの変換をしておらず、
    include_paths="/news-and-events/"に一件も一致せず記事を検出できていなかった"""
    if path.startswith("/content/"):
        parts = path.split("/", 3)
        if len(parts) == 4:
            return "/" + parts[3]
    return path


# JSON APIのレスポンス内で記事URL・タイトル・日付を指す可能性があるキー名
# （サイトごとに命名が異なるため複数候補を順に試す。例: mcdonalds.comの
# /bin/getCategory は articlePath/title/defaultDate、他サイトは url/link/date等の場合がある）
_JSON_URL_KEYS = ("articlePath", "url", "link", "path", "href", "urlId")
_JSON_TITLE_KEYS = ("title", "headline", "name")
_JSON_DATE_KEYS = ("defaultDate", "publishDate", "date", "pubDate", "published")


def list_json_api_candidates(target: dict, proxies: dict, verify: bool, use_zyte: bool = False,
                              tier: str = None) -> list:
    """JSON API(記事一覧をJSON配列で返すエンドポイント)から候補記事を抽出する。
    2026-09-16、corporate.mcdonalds.comの/bin/getCategoryで確認: 通常のHTML一覧ページ
    には記事リンクが一切含まれず(ナビゲーションのみ)、実際の一覧はこのJSON APIから
    クライアント側で取得・描画される構造だった。レスポンス形式はサイトごとに異なる
    ため、よくあるキー名を順に試す簡易実装（完全な汎用パーサではない）。

    tierを指定する場合、"tinyfish_fetch"は非対応(TinyFish Fetchはページをリンク構造を
    保持しない読みやすいmarkdownへ変換するため、JSON構造を壊してしまい使えない。
    2026-10-15、_ZYTE_METHOD_TIERSでJSON API方式のtier順から意図的に除外済み)"""
    url = target["target_url"]
    domain = target.get("domain") or urlparse(url).netloc

    if tier is not None:
        if tier == "zyte":
            text, final_url, status_code, _ = _fetch_with_zyte(url, proxies, verify)
        elif tier == "tinyfish_agent":
            text, final_url, status_code = _call_tinyfish_agent(url, proxies, verify)
        else:
            raise ValueError(f"JSON APIが対応していないtier: {tier}")
    elif use_zyte:
        text, final_url, status_code, _ = _fetch_with_zyte(url, proxies, verify)
    else:
        resp = requests.get(url, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        text, final_url, status_code = resp.text, resp.url, resp.status_code

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [], status_code

    # レスポンスが{"items": [...]}のように配列を包んでいる場合も考慮する。
    # Cision検索API(storaenso.com等)のように{success,error,message,result:{items:[...]}}
    # と一段ネストしているケースもあるため、まず"result"を優先的に開いてから同じ判定を行う
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if isinstance(data, dict):
        for key in ("items", "results", "articles", "data", "news"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if not isinstance(data, list):
        return [], status_code

    # urlId等、記事一覧APIのURL自体とは別の基準パスに対して相対解決すべきサイト向け
    # (例: storaenso.comのCision検索APIはurlIdが"2026/9/slug"のようなbareパスで、
    # APIエンドポイント自身ではなくニュースルームのアーカイブパスが基準になる)
    url_base = target.get("json_url_base") or target.get("fallback_url") or final_url

    candidates = []
    for item in data:
        if not isinstance(item, dict):
            continue
        path = next((item[k] for k in _JSON_URL_KEYS if item.get(k)), None)
        if not path:
            continue
        path = _strip_aem_content_prefix(path)
        full = urljoin(url_base, path)
        parsed = urlparse(full)
        if domain not in parsed.netloc:
            continue
        title = next((item[k] for k in _JSON_TITLE_KEYS if item.get(k)), None)
        # WordPress REST API等はtitleが{"rendered": "..."}のようなネスト構造になっている
        if isinstance(title, dict):
            title = title.get("rendered")
        date_str = next((item[k] for k in _JSON_DATE_KEYS if item.get(k)), None)
        candidates.append({
            "url": normalize_url(full),
            "title": title,
            "published_at": _parse_datetime(date_str) if date_str else None,
        })
        if len(candidates) >= MAX_HTML_LINKS_PER_TARGET:
            break
    return candidates, status_code


# ─── 保存 ─────────────────────────────────────────────────────────
_FIGURE_CAPTION_RE = re.compile(r"^\s*(?:Figure|図)\s*[0-9０-９]+\s*[:：]", re.IGNORECASE)


def strip_figure_captions(text: str) -> str:
    """本文抽出時に元記事の画像キャプション（例:「Figure 1: ...」「図1：...」）が
    独立した段落としてそのまま地の文に混入することがあるため、段落（改行区切り）の
    先頭がこのパターンに一致する段落だけを除去する。「(see Figure 1)」のように文中に
    自然に出てくる言及は文脈を壊すため残す。"""
    if not text:
        return text
    paragraphs = text.split("\n")
    kept = [p for p in paragraphs if not _FIGURE_CAPTION_RE.match(p)]
    return "\n".join(kept)


_LISTING_PAGE_RE = re.compile(
    r"Results?\s+\d+\s*(?:to|-|–)\s*\d+\s+of\s+\d+"
    r"|Ergebnisse?\s+\d+\s*(?:bis|-)\s*\d+\s+von\s+\d+"
    r"|結果\s*\d+\s*[〜~\-]\s*\d+\s*件"
    r"|全\s*\d+\s*件"
    r"|\d+\s*件中\s*\d+"
    r"|Page\s+\d+\s+of\s+\d+"
    r"|Showing\s+\d+\s*(?:to|-)\s*\d+\s+of"
    r"|検索結果",
    re.IGNORECASE,
)


def looks_like_listing_page(text: str) -> bool:
    """本文冒頭に「結果1〜20件」「Page 1 of N」等の検索結果・一覧ページ特有の
    文言があれば、個別記事ではなく一覧・ナビゲーションページを誤って取得したと判定する
    （2026-09-09、川崎さんからの不具合報告対応。ドイツ環境省・M&S等のクロール先で、
    include_pathsが緩いために一覧ページやカテゴリページが個別記事として保存されて
    いたケースが複数見つかった）。"""
    if not text:
        return False
    return bool(_LISTING_PAGE_RE.search(text[:400]))


def save_article(client: SupabaseClient, target: dict, article_url: str, fetched_url: str,
                  final_url: str, title: str, pub_dt, updated_dt, text: str,
                  document_info: dict = None, extraction_error: str = None) -> tuple:
    """article_urls / articles へ保存する。document_info(PDF/.docx用)があれば
    article_filesにも本文ファイルとして記録する。(is_new_url, is_new_version)を返す。
    本文が一覧・検索結果ページの特徴を持つ場合や、最終URLがクロール先のtarget_url自体
    （トップページ・一覧ページ）と一致する場合は記事として保存せず(False, False)を返す
    （include_pathsが緩いクロール先で一覧・トップページ自体を記事として拾ってしまう
    事故対策）。"""
    target_url = (target.get("target_url") or "").rstrip("/").split("?")[0]
    if final_url and final_url.rstrip("/").split("?")[0] == target_url:
        return False, False
    if looks_like_listing_page(text):
        return False, False
    text = strip_figure_captions(text)
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = client.select("article_urls", {"select": "*", "article_url": f"eq.{article_url}"})
    is_new_url = not existing

    if is_new_url:
        rows = client.insert("article_urls", [{
            "article_url": article_url,
            "domain": urlparse(article_url).netloc,
            "publisher_name": target["publisher_name"],
            "publisher_tag_id": target["publisher_tag_id"],
            "first_detected_at": now_iso,
            "last_detected_at": now_iso,
        }])
        article_url_row = rows[0]
    else:
        article_url_row = existing[0]
        client.update("article_urls", {"article_url_id": f"eq.{article_url_row['article_url_id']}"},
                      {"last_detected_at": now_iso})

    article_url_id = article_url_row["article_url_id"]

    # 既存の最新版と本文が同一なら新版を作らない（更新なし扱い）
    current = client.select("articles", {
        "select": "extracted_text",
        "article_url_id": f"eq.{article_url_id}",
        "is_current": "eq.true",
    })
    if current and _text_hash(current[0].get("extracted_text")) == _text_hash(text):
        return is_new_url, False

    article_rows = client.insert("articles", [{
        "article_url_id": article_url_id,
        "crawl_target_id": target["crawl_target_id"],
        "title": title,
        "published_at": pub_dt.isoformat() if pub_dt else None,
        "source_updated_at": updated_dt.isoformat() if updated_dt else None,
        "fetched_at": now_iso,
        "fetched_url": fetched_url,
        "final_url": final_url,
        "extracted_text": text,
        "extraction_status": "成功",
        "character_count": len(text) if text else 0,
        **({"extraction_error": extraction_error} if extraction_error else {}),
    }])

    if document_info:
        content = document_info["raw_content"]
        doc_kind = document_info["document_kind"]
        file_name = urlparse(final_url).path.rsplit("/", 1)[-1] or None
        client.insert("article_files", [{
            "article_id": article_rows[0]["article_id"],
            "file_role": "本文",
            "file_url": fetched_url,
            "final_url": final_url,
            "file_name": file_name,
            "file_extension": doc_kind,
            "mime_type": next((k for k, v in DOCUMENT_MIME_TYPES.items() if v == doc_kind), None),
            "file_size_bytes": len(content),
            "content_hash": hashlib.sha256(content).hexdigest(),
            "fetched_at": now_iso,
            "fetch_status": "成功",
            "http_status": document_info.get("http_status"),
            "extracted_text": text,
            "extraction_status": "成功",
        }], prefer="return=minimal")

    return is_new_url, True


def save_crawl_log(client: SupabaseClient, target: dict, started_at, finished_at,
                    run_result: str, http_status, items_detected: int,
                    new_items: int, updated_items: int, extraction_failures: int, error_message,
                    notes: str = None, crawl_log_id: str = None) -> str:
    """crawl_logsへ1行保存する。戻り値は保存した行のcrawl_log_id
    (2026-10-13追加: Pass2(escalate_pending)が後からこの行を更新できるように、
    呼び出し元へ返すようにした。crawl_log_id未指定ならここで新規採番する)"""
    crawl_log_id = crawl_log_id or str(uuid.uuid4())
    # LinkedIn等、bot対策として非標準のHTTPステータス(例:999)を返すサイトがあり、
    # crawl_logs.http_statusのCHECK制約(100〜599)に違反してINSERT自体が失敗し
    # クロールログが1件も残らない不具合があったため、範囲外の値はNULLとして保存する
    if not (isinstance(http_status, int) and 100 <= http_status <= 599):
        http_status = None
    client.insert("crawl_logs", [{
        "crawl_log_id": crawl_log_id,
        "crawl_target_id": target["crawl_target_id"],
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "run_result": run_result,
        "http_status": http_status,
        "items_detected": items_detected,
        "new_items": new_items,
        "updated_items": updated_items,
        "extraction_failures": extraction_failures,
        "error_message": error_message,
        "crawler_version": "article_crawler.py v1",
        "notes": notes,
    }], prefer="return=minimal")
    return crawl_log_id


def _compute_run_result(error_message, extraction_failures: int, new_items: int, updated_items: int) -> str:
    """run_result判定ロジック(process_target()の既存ルールをそのまま関数化。
    Pass2でのcrawl_logs再計算にも使い回す)"""
    if error_message:
        return "失敗"
    if extraction_failures > 0 and (new_items or updated_items):
        return "一部成功"
    if new_items or updated_items:
        return "成功"
    if extraction_failures > 0:
        return "抽出失敗"
    return "更新なし"


# ─── crawl_logs.notes へのファネル記録（2026-10-16追加。DBスキーマは変更しない） ───
# 以前は、Pass1で失敗した件数(再試行待ち)も、Pass2の各段階(TinyFish Fetch/Zyte/Agent)の
# 件数も、標準出力(チャンクログ)にしか残らず、DBから追えなかった。チャンクが時間切れで
# 終了するとPass2まで到達しないことがあり、その場合は「Pass1で失敗したのに記録が無い」
# 状態になっていた。notesに次の書式で追記する:
#   [Bot対策検知N件 | ]funnel={"p1_listing_failed":1,"p1_article_failed":3,
#                              "listing_retry":"ok","tiers":{"zyte":[試行,成功]},"final_failed":1}
# 既存のdowngrade_bot_blocked_targets()は notes が「Bot対策検知」で始まるかを見るため、
# Bot対策検知の表記は必ず先頭に置く。
FUNNEL_NOTES_MARKER = "funnel="


def _split_notes(notes) -> tuple:
    """notesを (Bot対策検知などの先頭部分, ファネル辞書) に分ける"""
    notes = notes or ""
    idx = notes.find(FUNNEL_NOTES_MARKER)
    if idx == -1:
        return notes.strip(" |"), {}
    prefix = notes[:idx].strip(" |")
    try:
        funnel = json.loads(notes[idx + len(FUNNEL_NOTES_MARKER):])
        if not isinstance(funnel, dict):
            funnel = {}
    except ValueError:
        funnel = {}
    return prefix, funnel


def _compose_notes(prefix: str, funnel: dict):
    """先頭部分とファネル辞書からnotes文字列を作る（どちらも空ならNone）"""
    parts = []
    if prefix:
        parts.append(prefix)
    if funnel:
        parts.append(FUNNEL_NOTES_MARKER + json.dumps(funnel, ensure_ascii=False, separators=(",", ":")))
    return " | ".join(parts) if parts else None


def _merge_funnel(existing: dict, patch: dict) -> dict:
    """tiersは段階ごとにマージし、それ以外のキーは上書きする"""
    merged = dict(existing)
    for k, v in (patch or {}).items():
        if k == "tiers":
            merged["tiers"] = {**(existing.get("tiers") or {}), **v}
        elif v is None:
            merged.pop(k, None)  # Noneは「このキーを消す」(再試行待ちの解消時に使う)
        else:
            merged[k] = v
    return merged


# ─── 再試行待ち(Pass1で失敗した記事)の永続化（2026-10-19追加） ────────────────
# 従来、再試行待ちはメモリ上のリストにしか無く、チャンクが時間切れで強制終了されるとPass2に
# 到達しないまま失われていた（本番実測: 再試行待ちの記事の84〜100%にPass2の記録が無く、
# しかも該当targetのcrawl_logsは「更新なし」等で残り、取りこぼしが見えなかった）。
# Pass1でcrawl_logsを保存する時点で、再試行待ちの記事を notes の funnel.p1_pending に残し、
# Pass2で解消されたら消す。未解消のものは専用ステップ(run_pass2_backlog)が引き継ぐ。
PENDING_STORE_MAX = 40          # 1ログ行のnotesへ保存する再試行待ち記事の上限（超過分は件数のみ記録）
PENDING_MAX_AGE_HOURS = 72      # これを超えて未解消のものは、再試行を諦めて失敗として確定する


def _pending_entry(item: dict) -> dict:
    """再試行待ちの1記事を、notesに保存できる小さな辞書にする（u=URL, t=タイトル, d=公開日, b=Bot対策）"""
    cand = item["cand"]
    entry = {"u": cand["url"]}
    if cand.get("title"):
        entry["t"] = str(cand["title"])[:80]
    pub = cand.get("published_at")
    if pub:
        entry["d"] = pub.isoformat() if hasattr(pub, "isoformat") else str(pub)
    if item.get("is_bot_block"):
        entry["b"] = 1
    if cand.get("summary"):
        entry["s"] = cand["summary"][:300]       # RSSの要約（最終失敗時の代用保存用。notesが大きくなりすぎないよう300字まで）
    return entry


def _entry_to_cand(entry: dict) -> dict:
    """_pending_entry()の逆変換（Pass2が使う候補の形に戻す）"""
    return {"url": entry["u"], "title": entry.get("t"), "published_at": _parse_datetime(entry.get("d")), "summary": entry.get("s") or ""}


def update_crawl_log_after_escalation(client: SupabaseClient, crawl_log_id: str, *,
                                       recovered_items_detected: int = 0, recovered_new_items: int = 0,
                                       recovered_updated_items: int = 0, final_failures: int = 0,
                                       bot_block_detections: int = 0, funnel_patch: dict = None) -> None:
    """2026-10-13追加: Pass2(escalate_pending)でのエスカレーション結果を、
    Pass1で既に保存済みのcrawl_logs行へ反映する。救済できた分は
    items_detected/new_items/updated_itemsへ加算し、最後まで失敗した分は
    extraction_failuresへ加算した上で、run_resultを再計算する。

    bot_block_detectionsは「Pass2の全段階を経ても結局Bot対策で失敗したまま」
    だった件数。notesへ反映し、既存のdowngrade_bot_blocked_targets()
    (直近3回連続Bot対策検知でcrawl_methodを自動降格する仕組み)が
    Pass1・Pass2のどちらで検知されたかによらず正しく動作するようにする"""
    rows = client.select("crawl_logs", {"select": "*", "crawl_log_id": f"eq.{crawl_log_id}", "limit": "1"})
    if not rows:
        return
    row = rows[0]
    new_items = (row.get("new_items") or 0) + recovered_new_items
    updated_items = (row.get("updated_items") or 0) + recovered_updated_items
    items_detected = (row.get("items_detected") or 0) + recovered_items_detected
    extraction_failures = (row.get("extraction_failures") or 0) + final_failures
    run_result = _compute_run_result(row.get("error_message"), extraction_failures, new_items, updated_items)
    patch = {
        "items_detected": items_detected,
        "new_items": new_items,
        "updated_items": updated_items,
        "extraction_failures": extraction_failures,
        "run_result": run_result,
    }
    prefix, funnel = _split_notes(row.get("notes"))
    if bot_block_detections:
        prefix = f"Bot対策検知{bot_block_detections}件"
    if funnel_patch:
        funnel = _merge_funnel(funnel, funnel_patch)
    if bot_block_detections or funnel_patch:
        patch["notes"] = _compose_notes(prefix, funnel)
    client.update("crawl_logs", {"crawl_log_id": f"eq.{crawl_log_id}"}, patch)


def _update_crawl_log_full(client: SupabaseClient, crawl_log_id: str, finished_at, run_result: str,
                            http_status, items_detected: int, new_items: int, updated_items: int,
                            extraction_failures: int, error_message, notes: str = None) -> None:
    """2026-10-14追加: Pass1で一覧取得自体の失敗により暫定行(件数0)として保存したcrawl_logsを、
    Pass2での一覧取得リトライ(escalate_pending_listings()、フルエスカレーション許可)の結果で
    まるごと上書きする。update_crawl_log_after_escalation()は記事単位の加算更新用で、
    こちらは一覧取得からやり直した場合の全置換用"""
    if not (isinstance(http_status, int) and 100 <= http_status <= 599):
        http_status = None
    client.update("crawl_logs", {"crawl_log_id": f"eq.{crawl_log_id}"}, {
        "finished_at": finished_at.isoformat(),
        "run_result": run_result,
        "http_status": http_status,
        "items_detected": items_detected,
        "new_items": new_items,
        "updated_items": updated_items,
        "extraction_failures": extraction_failures,
        "error_message": error_message,
        "notes": notes,
    })


# ─── 1クロール先の処理 ───────────────────────────────────────────
def _list_candidates_for_target(target: dict, proxies: dict, verify: bool, allow_escalation: bool = True):
    """process_target()から一覧取得部分だけを切り出したもの。
    (candidates, http_status, single_page_mode, error_message, is_404, use_browser, use_zyte)を返す。

    allow_escalation=Falseの場合、一覧取得自体が失敗した時にZyte/TinyFishへ
    エスカレーションせず、その場で諦める(Pass1用。2026-10-14追加: 以前はこの一覧取得が
    list_html_candidates/list_rss_candidates経由で常にフルエスカレーションしており、
    bot対策サイトの一覧取得待ちがPass1内で同期的に発生し他targetの処理を圧迫していた。
    HTML方式で+32%、RSS方式で+13%の1targetあたり処理時間増加として実測された)。
    明確な404はエスカレーション不要のためis_404=Trueで区別して返す
    （process_target()側で「即座に失敗確定」と「Pass2での一覧取得リトライへ回送」を分けるため）"""
    method = target["crawl_method"]
    endpoint_type = target.get("endpoint_type") or ""
    # 'ブラウザ操作'はHTMLと同じ収集ロジックをPlaywright経由で行う（bot対策サイト向け）
    # 'Zyte'も同様にHTMLと同じ収集ロジックをZyte API経由で行う（Playwright不採用のBot対策サイト向け、2026-09-16）
    use_browser = (method == "ブラウザ操作")
    # 'JSON API'は記事一覧を返すエンドポイント向け。当該サイト(mcdonalds.com等)は
    # 通常のHTML/HTTP取得がプロキシ経由で頻繁にタイムアウトするため、本文抽出も
    # Zyte経由で行う（use_zyteをZyteと同様にTrueにする）
    use_zyte = method in ("Zyte", "JSON API")

    error_message = None
    is_404 = False
    http_status = None
    candidates = []
    # 単一ページ監視型（サイトトップ等）はリンク収集をせず、URL自体を1件として扱う。
    # 他の記事と同じくlookback_daysでフィルタする
    # （日付が抽出できない場合はNoneとして扱われ、_within_lookbackにより除外される）。
    single_page_mode = False

    try:
        if method == "RSS":
            candidates, http_status = list_rss_candidates(target, proxies, verify,
                                                            allow_escalation=allow_escalation)
        elif method == "JSON API":
            candidates, http_status = list_json_api_candidates(target, proxies, verify, use_zyte=True)
        elif method in ("HTML", "ブラウザ操作", "Zyte"):
            if _is_feed_target(target):
                # 種別RSS+フィードURL: 方式がZyte/HTMLでも、フィードとして読む（1ページ扱いにしない）
                candidates, http_status = list_rss_candidates(target, proxies, verify,
                                                                allow_escalation=allow_escalation)
            elif endpoint_type == "サイトマップ":
                candidates, http_status = list_sitemap_candidates(target, proxies, verify)
            elif endpoint_type in LISTING_ENDPOINT_TYPES:
                candidates, http_status = list_html_candidates(target, proxies, verify, use_browser=use_browser,
                                                                 use_zyte=use_zyte,
                                                                 allow_escalation=allow_escalation)
            else:
                # SINGLE_PAGE_ENDPOINT_TYPES、および未知のendpoint_typeは安全側で単一ページ扱い
                single_page_mode = True
                candidates = [{"url": target["target_url"], "title": None, "published_at": None}]
        else:
            raise ValueError(f"未対応のcrawl_method: {method}")
    except requests.exceptions.HTTPError as e:
        error_message = f"{type(e).__name__}: {e}"
        if e.response is not None and e.response.status_code == 404:
            is_404 = True
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"

    return candidates, http_status, single_page_mode, error_message, is_404, use_browser, use_zyte


# crawl_method='Zyte'/'JSON API'のtargetは、通常のHTTP取得が機能しないと既に分かっている
# ため、Pass1では一切試行せずPass2へ丸ごと回送し、ここに定義した順序でエスカレーション段階を
# 試す(2026-10-15追加）。以前はPass1内でuse_zyte=Trueとして毎回同期的にZyteを呼んでおり、
# 記事数の多いtarget(例: McKinsey)で一覧取得+全記事のZyte呼び出しが合計85分に達し、
# Pass1全体の進行を圧迫していた(本番実測、2026-10-09以降にZyte APIキーが実際に有効化され
# てから顕在化)。
# JSON API方式は"tinyfish_fetch"を含めない: TinyFish FetchはページをJSON構造を保持しない
# 読みやすいmarkdownへ変換してしまうため、JSON APIのレスポンスには使えない
_ZYTE_METHOD_TIERS_ALL = {
    "Zyte": ("tinyfish_fetch", "zyte", "tinyfish_agent"),
    "JSON API": ("zyte", "tinyfish_agent"),
}
_REQUIRES_ZYTE_METHODS = ("JSON API",)  # Zyte無しでは取得できない方式（Agentは構造を保持できず代替にならない）


def _build_zyte_method_tiers(zyte_enabled: bool) -> dict:
    """Zyte停止中は、各方式の段階から'zyte'を外す。JSON API方式はZyte無しでは取得できないため
    段階を空にする（process_target()が「Zyte停止中のため取得できない」失敗として記録する）"""
    out = {}
    for method, tiers in _ZYTE_METHOD_TIERS_ALL.items():
        if zyte_enabled:
            out[method] = tiers
        elif method in _REQUIRES_ZYTE_METHODS:
            out[method] = ()
        else:
            out[method] = tuple(t for t in tiers if t != "zyte")
    return out


_ZYTE_METHOD_TIERS = _build_zyte_method_tiers(ZYTE_ENABLED)


def _list_candidates_with_tier(target: dict, proxies: dict, verify: bool, tier: str):
    """_list_candidates_for_target()のtier指定版。crawl_method='Zyte'/'JSON API'の
    targetについて、指定された1段階(tier)だけで一覧取得を試す。
    (candidates, http_status, single_page_mode, error_message, is_404)を返す
    (_list_candidates_for_target()と異なりuse_browser/use_zyteは返さない。
    これらのtargetではブラウザ操作は使わず、article_tierとしてtierをそのまま
    記事取得にも使い回すため不要)"""
    endpoint_type = target.get("endpoint_type") or ""
    method = target["crawl_method"]
    error_message = None
    is_404 = False
    http_status = None
    candidates = []
    single_page_mode = False
    try:
        if method == "JSON API":
            candidates, http_status = list_json_api_candidates(target, proxies, verify, tier=tier)
        elif _is_feed_target(target):
            # フィード(XML)は、Fetch/Agentでは構造が壊れて読めない。通常のHTTPで読み、Agentでは試さない（無駄な課金を避ける）
            if tier == "tinyfish_agent":
                raise ValueError("RSSフィードはAgentでは取得しません（XMLの構造が保てないため）")
            candidates, http_status = list_rss_candidates(target, proxies, verify, allow_escalation=False)
        elif endpoint_type == "サイトマップ":
            candidates, http_status = list_sitemap_candidates(target, proxies, verify)
        elif endpoint_type in LISTING_ENDPOINT_TYPES:
            candidates, http_status = list_html_candidates(target, proxies, verify, tier=tier)
        else:
            single_page_mode = True
            candidates = [{"url": target["target_url"], "title": None, "published_at": None}]
    except requests.exceptions.HTTPError as e:
        error_message = f"{type(e).__name__}: {e}"
        if e.response is not None and e.response.status_code == 404:
            is_404 = True
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
    return candidates, http_status, single_page_mode, error_message, is_404


def _listing_fallback_without_zyte(target: dict, client: SupabaseClient, proxies: dict, verify: bool,
                                    first_error: str, first_http_status, lookback: int, crawl_log_id: str,
                                    started_at) -> dict:
    """Zyte停止中に、HTML/RSSの一覧取得が通常HTTPで失敗した場合の処理（2026-10-20追加）。
    従来は「更新なし」の暫定行を書いてPass2へ回していたが、Pass2はチャンクの時間切れとバックログの
    予算切れで届かず、10/19〜20は20対象(HTML 13・RSS 7)が再試行すらされず、失敗理由も記録されなかった。
    - HTML: その場でTinyFish Fetchで一覧取得を再試行し、取れたら記事もTinyFish Fetchで取得する
    - RSS: フィードはXMLで、TinyFishは読みやすいテキストへ変換するため使えない（Zyteが無いと
      フォールバックできない）。Pass2へ回しても必ず失敗するので、理由つきの「失敗」として記録する
    どちらも失敗理由をcrawl_logsのerror_messageに残す（「更新なし」に隠さない）"""
    method = target["crawl_method"]
    if method == "RSS":
        message = f"{first_error}（RSSはZyte停止中のためフォールバックできません）"
        return _process_target_articles(target, client, proxies, verify, [], first_http_status, False, message,
                                         False, False, lookback, crawl_log_id, started_at)
    candidates, http_status, single_page_mode, error_message, is_404 = \
        _list_candidates_with_tier(target, proxies, verify, "tinyfish_fetch")
    if error_message is not None:
        message = f"{first_error} / TinyFish Fetchでも失敗: {error_message}"
        return _process_target_articles(target, client, proxies, verify, [], http_status, single_page_mode, message,
                                         False, False, lookback, crawl_log_id, started_at)
    return _process_target_articles(
        target, client, proxies, verify, candidates, http_status, single_page_mode, None, False, False,
        lookback, crawl_log_id, started_at, article_tier="tinyfish_fetch",
        extra_funnel={"p1_listing_fallback": "tinyfish_fetch", "p1_tinyfish_direct": 1})


def process_target(target: dict, client: SupabaseClient, proxies: dict, verify: bool) -> dict:
    """2026-10-13変更: 記事本文取得(extract_article)は「対象targetの通常の手段を
    1回だけ試す」(tier未指定)形に変更し、失敗した候補(明確な404を除く)は
    即座に抽出失敗扱いにせずpending_escalationsへ積んで呼び出し元(main())へ返す。
    main()は全target分のpending_escalationsをまとめた上でescalate_pending()を呼び、
    Zyte→TinyFish Fetch→TinyFish Agentの順で段階的に再試行する(1targetに失敗記事が
    集中して他targetの処理を圧迫する問題への対応、2026-10-09〜12の本番実測で発見)。

    2026-10-14変更: 一覧取得(list_html_candidates/list_rss_candidates)もPass1では
    エスカレーションなしの1回だけに留め、(明確な404を除き)失敗した場合はtarget全体を
    pending_listing_escalationとして返す。main()はPass2でescalate_pending_listings()を
    呼び、一覧取得からフルエスカレーション許可でやり直す(一覧取得だけが従来通り
    Pass1内で同期的にフルエスカレーションしてしまっていた問題への対応)

    2026-10-15変更: crawl_method='Zyte'/'JSON API'のtargetは、通常のHTTP取得が
    機能しないと既に分かっているため、Pass1では一覧取得・記事取得とも一切試行せず
    即座にpending_listing_escalationへ回送する(_ZYTE_METHOD_TIERS参照)。
    以前はこれらのtargetもPass1内で同期的にZyteを呼んでおり、記事数の多いtargetで
    合計85分かかることがあり他targetの処理を圧迫していた"""
    lookback = target.get("lookback_days") or DEFAULT_LOOKBACK_DAYS
    started_at = datetime.now(timezone.utc)
    crawl_log_id = str(uuid.uuid4())
    method = target["crawl_method"]

    if method in _ZYTE_METHOD_TIERS and not _ZYTE_METHOD_TIERS[method]:
        message = f"{method}方式はZyte停止中のため取得できません（再開は環境変数 CRAWL_ZYTE_ENABLED=true）"
        save_crawl_log(client, target, started_at, datetime.now(timezone.utc), "失敗", None,
                        0, 0, 0, 0, message, crawl_log_id=crawl_log_id)
        return {
            "run_result": "失敗", "items_detected": 0, "new_items": 0, "updated_items": 0,
            "extraction_failures": 0, "error_message": message, "bot_block_detections": 0,
            "crawl_log_id": crawl_log_id, "pending_escalations": [],
        }

    # 2026-10-20: Zyte停止中のcrawl_method='Zyte'は、Pass1でTinyFish Fetchを直接使う。
    # 従来はPass1で一切試さずPass2(チャンク末尾・バックログ)へ回していたが、Pass2はチャンクの時間切れで
    # 届かず、バックログもAgentに予算を使い切られ、95対象のうち85対象(約9割)が一覧取得すら
    # 行われず「更新なし」のまま残っていた(10/19〜20実測)。Pass2頼みをやめ、通常クロールの中で処理する。
    # 一覧取得に失敗した場合は、記事を取れないだけで済むよう「失敗」として記録する（一覧取得をAgentへ
    # 回すと、記事ごとにAgentを呼ぶ高コストの経路になるため、ここではPass2へ回さない）
    tiers_for_method = _ZYTE_METHOD_TIERS.get(method)
    if tiers_for_method and not ZYTE_ENABLED and tiers_for_method[0] == "tinyfish_fetch":
        candidates, http_status, single_page_mode, error_message, is_404 = \
            _list_candidates_with_tier(target, proxies, verify, "tinyfish_fetch")
        return _process_target_articles(
            target, client, proxies, verify, candidates, http_status, single_page_mode,
            error_message, False, False, lookback, crawl_log_id, started_at,
            article_tier="tinyfish_fetch", extra_funnel={"p1_tinyfish_direct": 1},
        )

    if method in _ZYTE_METHOD_TIERS:
        finished_at = datetime.now(timezone.utc)
        # Pass1の失敗ではなく、最初からPass2へ直送した対象。Pass2まで到達しないと暫定行の
        # 「更新なし」のまま残るため、notesのp1_routed_to_pass2で本物の更新なしと区別する
        save_crawl_log(client, target, started_at, finished_at, "更新なし", None,
                        0, 0, 0, 0, None, crawl_log_id=crawl_log_id,
                        notes=_compose_notes("", {"p1_routed_to_pass2": method}))
        return {
            "run_result": "更新なし",
            "items_detected": 0, "new_items": 0, "updated_items": 0,
            "extraction_failures": 0, "error_message": None, "bot_block_detections": 0,
            "crawl_log_id": crawl_log_id, "pending_escalations": [],
            "pending_listing_escalation": {
                "target": target, "crawl_log_id": crawl_log_id, "lookback": lookback,
                "started_at": started_at, "tiers": _ZYTE_METHOD_TIERS[method],
            },
        }

    candidates, http_status, single_page_mode, error_message, is_404, use_browser, use_zyte = \
        _list_candidates_for_target(target, proxies, verify, allow_escalation=False)

    if error_message is not None and not is_404 and not ZYTE_ENABLED and target["crawl_method"] in ("HTML", "RSS"):
        return _listing_fallback_without_zyte(target, client, proxies, verify, error_message, http_status,
                                               lookback, crawl_log_id, started_at)

    if error_message is not None and not is_404:
        finished_at = datetime.now(timezone.utc)
        # 暫定行。Pass2(一覧取得リトライ)まで到達しないと、この行は「更新なし」のまま残る。
        # notesのp1_listing_failedで、本当の更新なしと区別できるようにする
        save_crawl_log(client, target, started_at, finished_at, "更新なし", None,
                        0, 0, 0, 0, None, crawl_log_id=crawl_log_id,
                        notes=_compose_notes("", {"p1_listing_failed": 1}))
        return {
            "run_result": "更新なし",
            "items_detected": 0, "new_items": 0, "updated_items": 0,
            "extraction_failures": 0, "error_message": None, "bot_block_detections": 0,
            "crawl_log_id": crawl_log_id, "pending_escalations": [],
            "pending_listing_escalation": {
                "target": target, "crawl_log_id": crawl_log_id, "lookback": lookback,
                "started_at": started_at,
            },
        }

    return _process_target_articles(
        target, client, proxies, verify, candidates, http_status, single_page_mode,
        error_message, use_browser, use_zyte, lookback, crawl_log_id, started_at,
    )


# ─── 1対象あたりの時間上限（2026-10-19追加） ────────────────────────────────
# 実測(10/17〜19): 1,337ログのうち、300秒を超えた8件が全体の14%(2.4時間)を使い、特にMcKinsey(RSS)は
# 1回約38分(以前は約85分の例もある)かかる。候補を順番に全件取得するため、1件ごとの遅延が積み重なる。
# 1チャンク(1時間)の半分以上を1対象が使うことがあるため、上限を超えたら残りの候補は再試行待ち
# (Pass2・バックログのTinyFish Fetch)へ回し、次の対象へ進む。0以下で無効。
PER_TARGET_BUDGET_SEC = int(os.environ.get("CRAWL_TARGET_BUDGET_SEC") or 600)


def _defer_remaining_candidates(client: SupabaseClient, target: dict, remaining: list, crawl_log_id: str,
                                 lookback: int) -> tuple:
    """時間上限を超えた対象の、未取得の候補を再試行待ちにする。(再試行待ちのリスト, 既知でスキップした件数)。
    すでにDBにある記事のURLは、毎回取得し直している既知の記事なので、有料の再試行に回さずスキップする"""
    deferred, known = [], 0
    for cand in remaining:
        try:
            seen = bool(client.select("articles", {"select": "article_id", "fetched_url": f"eq.{cand['url']}",
                                                   "limit": "1"}))
        except Exception:  # noqa: BLE001  DBに問い合わせられない場合は、取りこぼさないよう再試行待ちにする
            seen = False
        if seen:
            known += 1
            continue
        deferred.append({"target": target, "cand": cand, "crawl_log_id": crawl_log_id,
                         "lookback": lookback, "is_bot_block": False})
    return deferred, known


def _process_target_articles(target: dict, client: SupabaseClient, proxies: dict, verify: bool,
                              candidates: list, http_status, single_page_mode: bool, error_message,
                              use_browser: bool, use_zyte: bool, lookback: int, crawl_log_id: str,
                              started_at, *, is_recovery: bool = False, article_tier: str = None,
                              listing_funnel: dict = None, extra_funnel: dict = None) -> dict:
    """process_target()から一覧取得より後ろ(記事単位の抽出・保存・crawl_logs書き込み)を
    切り出したもの。is_recovery=Trueの場合、Pass1で既にcrawl_logsへ暫定行(件数0)を
    INSERT済みであることを前提に、その行をUPDATEで上書きする
    (escalate_pending_listings()から一覧取得リトライ成功後に呼ばれる場合用、2026-10-14追加)。

    article_tierを指定すると、use_browser/use_zyteによらず各候補の記事取得を
    その1段階(tier)だけで試す(2026-10-15追加。crawl_method='Zyte'/'JSON API'の
    targetで、一覧取得が成功したtierをそのまま記事取得にも使い回すため。
    一覧取得できたサイトは本文ページも同じ手段で取得できる可能性が高いという想定)"""
    items_detected = 0
    new_items = 0
    updated_items = 0
    extraction_failures = 0
    bot_block_detections = 0
    pending_escalations = []
    budget_deferred = 0        # 時間上限で再試行待ちへ回した候補の数
    budget_skipped_known = 0   # 時間上限を超えたが、既知の記事なので取得しなかった候補の数
    target_t0 = time.monotonic()

    if error_message is None:
        extracted_pairs = []
        for idx, cand in enumerate(candidates):
            if PER_TARGET_BUDGET_SEC > 0 and time.monotonic() - target_t0 > PER_TARGET_BUDGET_SEC:
                deferred, budget_skipped_known = _defer_remaining_candidates(
                    client, target, candidates[idx:], crawl_log_id, lookback)
                pending_escalations.extend(deferred)
                budget_deferred = len(deferred)
                break
            if article_tier is not None:
                extracted = extract_article(cand["url"], proxies, verify, tier=article_tier)
            else:
                extracted = extract_article(cand["url"], proxies, verify, use_browser=use_browser,
                                             use_zyte=use_zyte)
            time.sleep(ARTICLE_FETCH_SLEEP)

            if single_page_mode:
                http_status = extracted.get("http_status")

            if not extracted["ok"]:
                # 明確な404(ページが存在しない)だけはこの場で抽出失敗として確定する。
                # それ以外(Bot対策・タイムアウト・接続エラー等)はPass2(escalate_pending)で
                # まとめて再試行するため、ここではまだ失敗として数えない
                if extracted.get("http_status") == 404:
                    extraction_failures += 1
                    continue
                is_bot_block = (extracted.get("error") or "").startswith("Bot対策")
                pending_escalations.append({
                    "target": target, "cand": cand, "crawl_log_id": crawl_log_id,
                    "lookback": lookback, "is_bot_block": is_bot_block,
                })
                continue

            # 2026-09-02にextract_article()側へ追加されたリトライ機構が、全リトライ後も
            # 文字化けが疑われる場合にencoding_suspectを立てていたにも関わらず、
            # このクロールループ側でそれを一切見ておらず、化けたまま保存され続けていた
            # 不具合を2026-09-14に発見。repair_garbled_articles.py（過去分の手動修復用）は
            # このフラグを正しく見ていたため、新規クロール分だけがすり抜けていた。
            if extracted.get("encoding_suspect"):
                extraction_failures += 1
                continue

            extracted_pairs.append((cand, extracted))

        # 同一クロール内で、本文からのtrafilatura抽出日付（候補側に日付が無い場合の
        # フォールバック）が複数候補にわたって全く同じ値になっている場合、記事個別の
        # 公開日ではなくページ共通のテンプレート日付（コピーライト表記・最終レビュー日等）
        # を誤抽出していると疑う（例: fao.org、2026-09-29発見。全候補が同一日付に固定され
        # lookback判定で全滅し、クロールは成功しているのに記事が1件も保存されない
        # 不具合が続いていた）。
        fallback_date_counts = Counter(
            extracted["published_at"].date()
            for cand, extracted in extracted_pairs
            if not cand.get("published_at") and extracted.get("published_at")
        )
        suspect_dates = {
            d for d, count in fallback_date_counts.items()
            if count >= 3 and count >= len(extracted_pairs) * 0.5
        }

        for cand, extracted in extracted_pairs:
            # RSSは一覧取得の時点で候補ごとの正確な日付(published_parsed)を持っている
            # ため、それを優先する。HTML一覧ページ等、候補側に日付が無いものだけ
            # 本文からのtrafilatura抽出日付(誤抽出対策済み)にフォールバックする。
            pub_dt = cand.get("published_at") or extracted["published_at"]
            if pub_dt is not None and not cand.get("published_at") and pub_dt.date() in suspect_dates:
                pub_dt = None
            if not _within_lookback(pub_dt, lookback):
                continue

            title = extracted["title"] or cand.get("title") or ""

            # タイトルがクロール対象サイト自身の名前そのもの＝個別記事ではなく
            # ハブ/一覧ページ等を誤って記事候補として拾ってしまったケース
            # （例: Ipsos。include_pathsが緩いサイトのトップ/一覧ページが軒並み
            # "Ipsos"という無内容な記事として保存されていた。2026-09-14発見）
            site_name_variants = {
                (target.get("publisher_name") or "").strip().lower(),
                (target.get("target_name") or "").strip().lower(),
            }
            site_name_variants.discard("")
            if title.strip().lower() in site_name_variants:
                extraction_failures += 1
                continue

            # 軽量キーワードフィルタ（メディア・データ提供機関カテゴリ限定）は
            # ここでは行わない。記事は無条件で保存し（案A）、フィルタ判定と
            # LLM分析スキップはarticle_analyzer.py側の責務とする
            # （フィルタールール_たたき台_20260720.md 3.4節 案A）。

            items_detected += 1
            final_url = extracted.get("final_url") or cand["url"]
            canonical_url = extracted.get("canonical_url") or normalize_url(final_url)
            document_info = extracted if extracted.get("is_document") else None
            is_new_url, is_new_version = save_article(
                client, target, canonical_url, cand["url"], final_url,
                title, pub_dt, extracted.get("updated_at"), extracted["text"],
                document_info=document_info,
            )
            if is_new_version:
                new_items += 1 if is_new_url else 0
                updated_items += 0 if is_new_url else 1

    finished_at = datetime.now(timezone.utc)
    run_result = _compute_run_result(error_message, extraction_failures, new_items, updated_items)

    crawl_funnel = dict(extra_funnel or {})   # 例: Pass1でTinyFish Fetchを直接使った印(p1_tinyfish_direct)
    if is_recovery:
        crawl_funnel.update({**(listing_funnel or {"p1_listing_failed": 1}), "listing_retry": "ok"})
    if pending_escalations:
        crawl_funnel["p1_article_failed"] = len(pending_escalations)
        # チャンクが時間切れで止まってもPass2を引き継げるよう、再試行待ちの記事をDBに残す
        crawl_funnel["p1_pending"] = [_pending_entry(p) for p in pending_escalations[:PENDING_STORE_MAX]]
        if len(pending_escalations) > PENDING_STORE_MAX:
            crawl_funnel["p1_pending_overflow"] = len(pending_escalations) - PENDING_STORE_MAX
    crawl_funnel.update(_include_nomatch_funnel(target))     # 条件に合う記事が0件なのに、記事らしいリンクがある（設定ミスの疑い）
    if budget_deferred or budget_skipped_known:
        crawl_funnel["p1_budget_deferred"] = budget_deferred          # 時間上限で再試行待ちへ回した候補
        crawl_funnel["p1_budget_skipped_known"] = budget_skipped_known  # 既知の記事のため取得しなかった候補
    crawl_notes = _compose_notes(
        f"Bot対策検知{bot_block_detections}件" if bot_block_detections else "", crawl_funnel)
    if is_recovery:
        _update_crawl_log_full(client, crawl_log_id, finished_at, run_result, http_status,
                                items_detected, new_items, updated_items, extraction_failures,
                                error_message, notes=crawl_notes)
    else:
        save_crawl_log(client, target, started_at, finished_at, run_result, http_status,
                        items_detected, new_items, updated_items, extraction_failures, error_message,
                        notes=crawl_notes, crawl_log_id=crawl_log_id)

    return {
        "run_result": run_result,
        "items_detected": items_detected,
        "new_items": new_items,
        "updated_items": updated_items,
        "extraction_failures": extraction_failures,
        "error_message": error_message,
        "bot_block_detections": bot_block_detections,
        "crawl_log_id": crawl_log_id,
        "pending_escalations": pending_escalations,
        "budget_deferred": budget_deferred,
        "budget_skipped_known": budget_skipped_known,
    }


def _save_rss_summary_fallback(client: SupabaseClient, item: dict, reason: str):
    """全段階で本文を取得できなかった記事を、フィードの題名＋要約で代用保存する。保存した場合は(is_new_url, is_new_version)、
    対象外(要約が短い・期間外・サイト名のみの題名・一覧ページ等)ならNoneを返す"""
    cand, target = item["cand"], item["target"]
    summary = (cand.get("summary") or "").strip()
    title = (cand.get("title") or "").strip()
    if len(summary) < RSS_SUMMARY_MIN_CHARS or not title:
        return None
    if not _within_lookback(cand.get("published_at"), item.get("lookback") or DEFAULT_LOOKBACK_DAYS):
        return None
    if title.lower() in {(target.get("publisher_name") or "").strip().lower(), (target.get("target_name") or "").strip().lower()}:
        return None
    if summary.lower().startswith(title.lower()):          # 題名の繰り返しで始まる要約は、題名を重ねない
        text = summary
    else:
        text = f"{title}\n\n{summary}"
    url = cand["url"]
    return save_article(client, target, normalize_url(url), url, url, title, cand.get("published_at"), None, text,
                        extraction_error=f"{RSS_SUMMARY_ONLY_MARK}: 本文を取得できなかったため、RSSの題名と要約で代用(理由: {(reason or '不明')[:80]})")


def _apply_recovered_candidate(client: SupabaseClient, target: dict, cand: dict, extracted: dict,
                                lookback: int):
    """Pass2(escalate_pending)で再取得に成功した候補へ、process_target()後半と同じ
    lookbackフィルタ・サイト名誤抽出除外・保存(save_article)を適用する。
    戻り値: 保存した場合は(is_new_url, is_new_version)、対象外として破棄した場合はNone。

    process_target()の該当ロジックと異なり、同一target内の複数候補にまたがる
    テンプレート日付誤抽出の検知(suspect_dates)は行わない(Pass2は候補を
    targetをまたいで1件ずつ独立に処理するため、兄弟候補の情報を参照できない。
    発生頻度の低いエッジケースのため、この簡略化は許容する)"""
    pub_dt = cand.get("published_at") or extracted.get("published_at")
    if not _within_lookback(pub_dt, lookback):
        return None

    title = extracted.get("title") or cand.get("title") or ""
    site_name_variants = {
        (target.get("publisher_name") or "").strip().lower(),
        (target.get("target_name") or "").strip().lower(),
    }
    site_name_variants.discard("")
    if title.strip().lower() in site_name_variants:
        return None

    final_url = extracted.get("final_url") or cand["url"]
    canonical_url = extracted.get("canonical_url") or normalize_url(final_url)
    document_info = extracted if extracted.get("is_document") else None
    return save_article(
        client, target, canonical_url, cand["url"], final_url,
        title, pub_dt, extracted.get("updated_at"), extracted["text"],
        document_info=document_info,
    )


# ─── Pass2: 全target分の失敗候補をまとめて段階的に再試行する ─────────────
# 2026-10-13追加。通常クロール(Pass1)で失敗した候補(明確な404を除く)を全target分
# まとめてから、TinyFish Fetch→Zyte→TinyFish Agentの順に段階的に再試行する。
# 1target内に失敗記事が集中していても、Pass1は全targetについて「通常クロールを
# 1回試す」だけで完了するため、他targetの処理が遅延・圧迫されない
# (2026-10-09〜12の本番実測で、1記事ずつ最後までチェーンさせる旧方式は
# 1targetの処理に最大87分かかり、1日の処理件数が507件→233件まで減っていた)。
# 段階の順番（2026-10-19）: 最初の2段(TinyFish Fetch / Zyte)は、1記事あたりの期待所要時間
#   E(A→B) = t_A + (1 - p_A) × t_B   （t=1回の所要秒数, p=抽出成功率）
# が小さい順に並べる。TinyFish Agentは遅く費用も大きいため、常に最後（残りだけを処理する）。
# TIER_PROFILEは「通常取得で失敗した記事」に対する実測値。None(未計測)が1つでもあれば、
# 比較できないため従来の順(TinyFish Fetch→Zyte)を維持する。計測できたら数値を入れるだけでよい。
# 実測の取り方: crawl_funnel_report.pyのtiers(試行数・成功数・秒数)。ただし2段目は1段目の失敗分
# だけを見るため成功率が低めに偏る。順番の比較には、同じ記事で両方を試した計測を使うこと。
TIER_PROFILE = {
    "tinyfish_fetch": {"p": 0.76, "t": None},  # p: 2026-10-18〜19の実測(投入155→成功118)。tは未計測
    "zyte": {"p": None, "t": None},            # 未計測（タイムアウトはTinyFish Fetch 60秒 / Zyte 90秒）
}
_KNOWN_TIERS = ("tinyfish_fetch", "zyte", "tinyfish_agent")


def choose_tier_order(profile: dict = None, default: tuple = ("tinyfish_fetch", "zyte")) -> tuple:
    """最初の2段の順を期待所要時間で決め、TinyFish Agentを最後に付けて返す"""
    profile = profile or TIER_PROFILE
    a, b = default
    pa, pb = profile.get(a) or {}, profile.get(b) or {}
    values = (pa.get("p"), pa.get("t"), pb.get("p"), pb.get("t"))
    if any(v is None for v in values):
        return (a, b, "tinyfish_agent")
    expected_ab = pa["t"] + (1 - pa["p"]) * pb["t"]
    expected_ba = pb["t"] + (1 - pb["p"]) * pa["t"]
    return (a, b, "tinyfish_agent") if expected_ab <= expected_ba else (b, a, "tinyfish_agent")


def _tier_order_from_env(environ=None) -> tuple:
    """環境変数 CRAWL_TIER_ORDER（例: "zyte,tinyfish_fetch,tinyfish_agent"）で順番を上書きできる。
    3段すべてを1回ずつ含む指定のみ有効。不正な指定は無視して、決定ロジックの結果を使う"""
    raw = ((environ if environ is not None else os.environ).get("CRAWL_TIER_ORDER") or "").strip()
    if raw:
        tiers = tuple(t.strip() for t in raw.split(",") if t.strip())
        if sorted(tiers) == sorted(_KNOWN_TIERS):
            return tiers
        print(f"[警告] CRAWL_TIER_ORDER={raw!r}は無効です（3段を1回ずつ含めてください）。既定の順を使います")
    return choose_tier_order()


def _active_tiers(tiers: tuple, zyte_enabled: bool) -> tuple:
    """Zyte停止中は、段階から'zyte'を外す（TinyFish Fetch→Agentの2段になる）"""
    return tuple(t for t in tiers if zyte_enabled or t != "zyte")


ESCALATION_TIERS = _active_tiers(_tier_order_from_env(), ZYTE_ENABLED)

# ─── バックログ処理の時間予算（2026-10-19） ────────────────────────────────
# 実測(10/17〜19): 通常クロール(HTML/RSS 410対象)に約5.5時間かかり、crawlステップの上限(6時間)に
# 余裕が無い。Pass1の失敗は毎日約480〜500記事、一覧取得が未完了の対象は毎日約70。
# 予算は固定値ではなく、実際の件数から算出する。1件あたりの秒数は仮置き（段階別の所要秒数の
# 記録が貯まったら、crawl_funnel_report.pyの平均秒数で見直すこと）。
BACKLOG_ARTICLE_SEC = 30        # 仮置き: 1記事あたりの期待所要秒数（全段階を通した平均）
BACKLOG_LISTING_SEC = 90        # 仮置き: 一覧取得の再試行1対象あたり
BACKLOG_BUDGET_MIN_SEC = 1800
BACKLOG_BUDGET_MAX_SEC = 7200
BACKLOG_AGENT_CAP = TINYFISH_AGENT_MAX_SITES * TINYFISH_AGENT_MAX_PER_SITE   # バックログ処理で使うTinyFish Agentの上限件数（サイト数×1サイトの件数）


def compute_backlog_budget(n_articles: int, n_listing: int) -> int:
    """件数からバックログの時間予算(秒)を算出する（下限・上限あり）"""
    need = n_articles * BACKLOG_ARTICLE_SEC + n_listing * BACKLOG_LISTING_SEC
    return int(min(BACKLOG_BUDGET_MAX_SEC, max(BACKLOG_BUDGET_MIN_SEC, need * 1.2)))


# ── 失敗理由の記録（2026-10-22追加）──────────────────────────────────────────
# 従来は、記事ごとの失敗理由がDBに残らず（件数だけ）、「Fetchが全滅した原因がサイト側かTinyFish側か」を
# 後から切り分けられなかった（2026-10-22: Fetch 223件・Agent 42件が全滅、原因不明）。
# 最終失敗した記事について、理由の分類(件数)と、記事URLの例を、notesのfunnelに残す。
FAIL_SAMPLE_MAX = 3             # 1ログ行に残す、失敗した記事URLの例の上限


def _classify_failure(tier, reason) -> str:
    """失敗理由の文字列を、集計しやすい短い分類にする（上から順に判定）"""
    r = re.sub(r"https?://\S+", " ", str(reason or "").lower())    # 記事URLの数字(403など)を誤って拾わないよう除く
    if re.search(r"402", r) or "payment required" in r:
        return "credit_exhausted"          # TinyFishの残高切れ
    if re.search(r"429", r) or "too many requests" in r or "rate limit" in r:
        return "rate_limited"
    if any(k in r for k in ("login", "log in", "password", "credentials", "authentication", "sign in")):
        return "login_required"
    if any(k in r for k in ("captcha", "cloudflare", "access denied", "security verification", "verify you are human",
                            "bot protection", "bot-check", "blocked", "forbidden")) or re.search(r"403", r):
        return "bot_blocked"
    if any(k in r for k in ("not an article", "no article", "not found", "contact form", "does not exist",
                            "job listing", "not article")) or re.search(r"404", r):
        return "not_article"
    if any(k in r for k in ("短すぎ", "本文が空", "empty")):
        return "empty_or_short"
    if any(k in r for k in ("timeout", "timed out", "chunkedencodingerror", "connection", "ended prematurely")):
        return "network_error"
    if "文書抽出エラー" in r:
        return "document_error"
    if tier == "tinyfish_fetch" and "取得失敗" in r:
        return "fetch_failed"              # Fetchが理由を返さず失敗（サイト側の拒否の可能性）
    return "other"


def escalate_pending(pending: list, client: SupabaseClient, proxies: dict, verify: bool,
                      deadline: float = None, stats: dict = None) -> dict:
    """pendingは process_target() が返した pending_escalations を全target分まとめたもの。
    戻り値: {"recovered": 件数, "final_failed": 件数}（時間切れで未完了が残った場合のみ"unfinished"を追加）

    2026-10-19変更:
    - deadline(time.monotonic()基準)を過ぎたら新しい試行を始めず、未完了の記事は
      「再試行待ち(p1_pending)」としてnotesに残す（失敗としては確定しない）。
      最終失敗は、全段階を試し切った記事だけに限る。
    - 解消した(救済・最終失敗・対象外)ログ行は pass2=done にして p1_pending を消す。
    - 各段階の投入数・成功数は、救済が無かった対象も含め、その場でnotesへ書く
      （途中で打ち切られても、実際に試した件数が残るように）。"""
    remaining = list(pending)
    recovered_count = 0
    log_tiers = defaultdict(dict)  # crawl_log_id -> {段階: [試行数, 抽出成功数]}（DBのnotesへ記録）
    tier_totals = {}               # 段階 -> [試行数, 抽出成功数]（チャンクログへの要約用）
    all_log_ids = {item["crawl_log_id"] for item in pending}
    error_counts = Counter()       # "段階|失敗理由" -> 件数（statsへ渡す。異常検出・原因調査用）
    timed_out = False
    unfinished = []
    for tier in ESCALATION_TIERS:
        if not remaining:
            break
        print(f"\n[Pass2: {tier}] 対象{len(remaining)}件を再試行します")
        still_failed = []
        recovered_by_log = defaultdict(lambda: {"items_detected": 0, "new_items": 0, "updated_items": 0})
        tier_attempted = defaultdict(int)
        tier_ok = defaultdict(int)
        tier_sec = defaultdict(float)  # 段階ごとの所要秒数（順番の見直し用。perf_counterで計測）
        for idx, item in enumerate(remaining):
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                unfinished = still_failed + remaining[idx:]
                print(f"[Pass2: {tier}] 時間予算を使い切ったため中断します（未完了{len(unfinished)}件は次回へ繰り越し）")
                break
            if tier == "tinyfish_agent" and not _tinyfish_agent_budget_available(item["target"].get("crawl_target_id")):
                item["agent_skipped"] = True     # Agentの上限で試せなかった（失敗理由の記録用）
                still_failed.append(item)
                continue
            tier_attempted[item["crawl_log_id"]] += 1
            _t0 = time.perf_counter()
            extracted = extract_article(item["cand"]["url"], proxies, verify, tier=tier)
            tier_sec[item["crawl_log_id"]] += time.perf_counter() - _t0
            if tier == "tinyfish_agent":
                _record_tinyfish_agent_call(item["target"].get("crawl_target_id"))
            time.sleep(ARTICLE_FETCH_SLEEP)
            if not extracted.get("ok") or extracted.get("encoding_suspect"):
                reason = extracted.get("error") or f"http_{extracted.get('http_status')}" if not extracted.get("ok") \
                    else "文字化け疑い"
                error_counts[f"{tier}|{str(reason)[:60]}"] += 1
                item["last_fail"] = (tier, str(reason))     # 最後に失敗した段階と理由（最終失敗の記録用）
                still_failed.append(item)
                continue
            tier_ok[item["crawl_log_id"]] += 1
            applied = _apply_recovered_candidate(client, item["target"], item["cand"], extracted,
                                                  item["lookback"])
            if applied is None:
                continue  # lookback対象外・サイト名誤抽出のため対象外(失敗としては数えない)
            is_new_url, is_new_version = applied
            agg = recovered_by_log[item["crawl_log_id"]]
            agg["items_detected"] += 1
            if is_new_version:
                if is_new_url:
                    agg["new_items"] += 1
                else:
                    agg["updated_items"] += 1
            recovered_count += 1
        for crawl_log_id, n in tier_attempted.items():
            # [試行数, 抽出成功数, 所要秒数]
            log_tiers[crawl_log_id][tier] = [n, tier_ok.get(crawl_log_id, 0), round(tier_sec[crawl_log_id], 1)]
        tier_totals[tier] = [sum(tier_attempted.values()), sum(tier_ok.values()), round(sum(tier_sec.values()), 1)]
        # 救済の有無によらず、この段階を試した全ログ行へ、段階別の試行・成功数をその場で記録する
        for crawl_log_id in set(recovered_by_log) | set(tier_attempted):
            agg = recovered_by_log.get(crawl_log_id) or {"items_detected": 0, "new_items": 0, "updated_items": 0}
            update_crawl_log_after_escalation(
                client, crawl_log_id, recovered_items_detected=agg["items_detected"],
                recovered_new_items=agg["new_items"], recovered_updated_items=agg["updated_items"],
                funnel_patch={"tiers": dict(log_tiers[crawl_log_id])})
        if timed_out:
            break
        remaining = still_failed

    # 全段階を試し切っても失敗したままの候補だけを、抽出失敗として確定する（時間切れの場合は確定しない）
    final = [] if timed_out else remaining
    final_failed_by_log = defaultdict(int)
    bot_block_by_log = defaultdict(int)
    fail_reasons_by_log = defaultdict(Counter)
    fail_samples_by_log = defaultdict(list)
    agent_skipped_by_log = defaultdict(int)
    summary_saved_by_log = defaultdict(int)
    for item in final:
        final_failed_by_log[item["crawl_log_id"]] += 1
        if item.get("is_bot_block"):
            bot_block_by_log[item["crawl_log_id"]] += 1
        last_tier, last_reason = item.get("last_fail") or (None, "")
        fail_reasons_by_log[item["crawl_log_id"]][_classify_failure(last_tier, last_reason)] += 1
        if _classify_failure(last_tier, last_reason) != "not_article":
            try:                     # 失敗しても、Pass2全体は止めない（Failure Isolation）
                if _save_rss_summary_fallback(client, item, str(last_reason)):
                    summary_saved_by_log[item["crawl_log_id"]] += 1
            except Exception as e:  # noqa: BLE001
                print(f"  [RSS要約の代用保存に失敗] {item['cand']['url'][:80]}: {type(e).__name__}: {str(e)[:80]}")
        if len(fail_samples_by_log[item["crawl_log_id"]]) < FAIL_SAMPLE_MAX:
            fail_samples_by_log[item["crawl_log_id"]].append(
                {"u": item["cand"]["url"][:100], "r": f"{last_tier}:{last_reason}"[:100]})
        if item.get("agent_skipped"):
            agent_skipped_by_log[item["crawl_log_id"]] += 1
    unfinished_by_log = defaultdict(list)
    for item in unfinished:
        unfinished_by_log[item["crawl_log_id"]].append(item)
    for crawl_log_id in all_log_ids:
        left = unfinished_by_log.get(crawl_log_id)
        patch = {"tiers": dict(log_tiers.get(crawl_log_id, {}))}
        if left:
            patch["p1_pending"] = [_pending_entry(i) for i in left]   # 未完了分だけを残す
        else:
            patch["p1_pending"] = None
            patch["pass2"] = "done"
        if crawl_log_id in final_failed_by_log:
            patch["final_failed"] = final_failed_by_log[crawl_log_id]
            patch["fail_reasons"] = dict(fail_reasons_by_log[crawl_log_id])       # 例: {"bot_blocked": 3, "not_article": 1}
            patch["fail_samples"] = fail_samples_by_log[crawl_log_id]             # 例: [{"u": URL, "r": "tinyfish_agent:..."}]
            if agent_skipped_by_log.get(crawl_log_id):
                patch["agent_skipped"] = agent_skipped_by_log[crawl_log_id]       # Agentの上限で試せなかった記事数
            if summary_saved_by_log.get(crawl_log_id):
                patch["summary_saved"] = summary_saved_by_log[crawl_log_id]       # RSSの要約で代用保存した記事数
        update_crawl_log_after_escalation(
            client, crawl_log_id, final_failures=final_failed_by_log.get(crawl_log_id, 0),
            bot_block_detections=bot_block_by_log.get(crawl_log_id, 0), funnel_patch=patch)

    print(f"\nPass2完了: 救済{recovered_count}件 / 最終的に失敗{len(final)}件"
          f"(うちBot対策検知{sum(bot_block_by_log.values())}件)"
          + (f" / 未完了{len(unfinished)}件(次回へ繰り越し)" if unfinished else ""))
    # チャンクログ用のファネル要約（Pass1失敗 → 各段階の投入/成功 → 最終失敗）
    print("[funnel] Pass1失敗(記事単位)=" + str(len(pending)) + " | "
          + " | ".join(f"{t}: 投入{a}→成功{o}（{sec:.0f}秒）" for t, (a, o, sec) in tier_totals.items())
          + f" | 最終失敗={len(final)} | 未完了={len(unfinished)}")
    if stats is not None:
        acc = stats.setdefault("articles", {"attempted_items": 0, "recovered": 0, "final_failed": 0,
                                            "unfinished": 0, "timed_out": False, "agent_cap_reached": False,
                                            "tiers": {}, "errors": {}})
        acc["attempted_items"] += len(pending)
        acc["recovered"] += recovered_count
        acc["final_failed"] += len(final)
        acc["unfinished"] += len(unfinished)
        acc["timed_out"] = acc["timed_out"] or timed_out
        acc["agent_cap_reached"] = acc["agent_cap_reached"] or not _tinyfish_agent_budget_available()
        for t, (a, o, sec) in tier_totals.items():
            cur = acc["tiers"].setdefault(t, [0, 0, 0.0])
            cur[0] += a
            cur[1] += o
            cur[2] = round(cur[2] + sec, 1)
        for k, v in error_counts.items():
            acc["errors"][k] = acc["errors"].get(k, 0) + v
    result = {"recovered": recovered_count, "final_failed": len(final)}
    if unfinished:
        result["unfinished"] = len(unfinished)
    return result


def _resolve_tiered_listing(target: dict, proxies: dict, verify: bool, tiers: tuple):
    """tiersで指定された順(例: ESCALATION_TIERSと同じTinyFish Fetch→Zyte→Agent)に
    一覧取得を試し、最初に成功した段階とその結果を返す。
    (resolved_tier, candidates, http_status, single_page_mode, error_message, is_404)を返す。
    全段階が失敗した場合はresolved_tier=Noneとなり、error_messageは最後に試した段階のもの
    (2026-10-15追加、crawl_method='Zyte'/'JSON API'のtarget用)"""
    error_message = None
    is_404 = False
    for tier in tiers:
        # 2026-10-19: 記事の再試行にしか効いていなかったAgentの上限を、一覧取得にも効かせる
        # （Agentは1回数十秒〜数分・有料。上限を超えたら使わず、失敗として返す）
        if tier == "tinyfish_agent":
            if not _tinyfish_agent_budget_available(target.get("crawl_target_id")):
                error_message = (f"{error_message}（TinyFish Agentは上限に達したため未実施）" if error_message
                                 else "TinyFish Agentの上限に達したため一覧取得を試行しませんでした")
                continue
            _record_tinyfish_agent_call(target.get("crawl_target_id"))
        candidates, http_status, single_page_mode, error_message, is_404 = \
            _list_candidates_with_tier(target, proxies, verify, tier)
        if error_message is None:
            return tier, candidates, http_status, single_page_mode, error_message, is_404
        if is_404:
            break  # 明確な404はこれ以上他の段階を試しても無駄
    return None, [], None, False, error_message, is_404


def _record_listing_stat(stats: dict, target: dict, outcome: str, sec: float = 0.0, **extra) -> None:
    """バックログ実行レポート用に、一覧取得の再試行1対象ぶんの結果を残す"""
    if stats is None:
        return
    stats.setdefault("listing", []).append({
        "target": (target.get("target_name") or "")[:40], "method": target.get("crawl_method"),
        "outcome": outcome, "sec": round(sec, 1), **extra})


def escalate_pending_listings(pending_listings: list, client: SupabaseClient, proxies: dict, verify: bool,
                               deadline: float = None, stats: dict = None) -> list:
    """2026-10-14追加: Pass1で一覧取得自体が(明確な404を除き)失敗したtargetについて、
    一覧取得をフルエスカレーション許可(allow_escalation=True)で再試行する。
    取得できた候補はそのまま通常の記事抽出パス(_process_target_articles、tier未指定の
    1回試行)に流し、そこで新たに判明した記事単位の失敗は呼び出し元(main())が
    既存のescalate_pending()(TinyFish Fetch→Zyte→TinyFish Agent)へまとめて渡せるよう、
    pending_escalationsのリストとして返す（一覧取得の救済と記事取得の救済を
    同じ3段エスカレーションの土俵に合流させる）。

    2026-10-15追加: item内に"tiers"キーがある場合(process_target()がcrawl_method=
    'Zyte'/'JSON API'のtargetをPass1で一切試行せず丸ごと回送したケース)は、
    _list_candidates_for_target()の通常のエスカレーション順ではなく、"tiers"で
    指定された順(TinyFish Fetch→Zyte→Agent)で一覧取得を試す。一覧取得が成功した
    段階を、そのtargetの記事取得(article_tier)にもそのまま使い回す。

    一覧取得がこの再試行でも失敗した場合は、Pass1で保存した暫定crawl_logs行を
    「失敗」で確定させる"""
    if not pending_listings:
        return []
    print(f"\n[Pass2: 一覧取得リトライ] 対象{len(pending_listings)}件を再試行します")
    merged_pending_escalations = []
    recovered = 0
    still_failed = 0
    skipped = 0
    for item in pending_listings:
        if deadline is not None and time.monotonic() >= deadline:
            skipped += 1  # 時間予算切れ。ログ行は触らず、次回のバックログ処理へ繰り越す
            _record_listing_stat(stats, item["target"], "skipped")
            continue
        _listing_t0 = time.perf_counter()
        target = item["target"]
        crawl_log_id = item["crawl_log_id"]
        lookback = item["lookback"]
        started_at = item["started_at"]
        tiers = item.get("tiers")
        # 直送した対象(tiersあり)はPass1の失敗ではないため、失敗と区別して記録する
        listing_funnel = {"p1_routed_to_pass2": target["crawl_method"]} if tiers else {"p1_listing_failed": 1}
        if tiers:
            resolved_tier, candidates, http_status, single_page_mode, error_message, is_404 = \
                _resolve_tiered_listing(target, proxies, verify, tiers)
            use_browser, use_zyte = False, False
        else:
            resolved_tier = None
            candidates, http_status, single_page_mode, error_message, is_404, use_browser, use_zyte = \
                _list_candidates_for_target(target, proxies, verify, allow_escalation=True)
        if error_message is not None and not is_404:
            _update_crawl_log_full(client, crawl_log_id, datetime.now(timezone.utc), "失敗", None,
                                    0, 0, 0, 0, error_message,
                                    notes=_compose_notes("", {**listing_funnel, "listing_retry": "failed"}))
            still_failed += 1
            _record_listing_stat(stats, target, "failed", time.perf_counter() - _listing_t0,
                                 error=str(error_message)[:80])
            continue
        result = _process_target_articles(
            target, client, proxies, verify, candidates, http_status, single_page_mode,
            error_message, use_browser, use_zyte, lookback, crawl_log_id, started_at,
            is_recovery=True, article_tier=resolved_tier, listing_funnel=listing_funnel,
        )
        merged_pending_escalations.extend(result.get("pending_escalations") or [])
        recovered += 1
        _record_listing_stat(stats, target, "ok", time.perf_counter() - _listing_t0,
                             candidates=len(candidates), new_items=result.get("new_items", 0),
                             pending=len(result.get("pending_escalations") or []))
    if skipped:
        print(f"[Pass2: 一覧取得リトライ] 時間予算を使い切ったため{skipped}件を次回へ繰り越します")
    print(f"[Pass2: 一覧取得リトライ] 完了: 一覧取得成功{recovered}件 / 一覧取得も失敗{still_failed}件"
          f"（記事単位の再試行要求{len(merged_pending_escalations)}件は通常のPass2へ合流）")
    return merged_pending_escalations


# ─── Bot対策検知によるクロール先の自動降格 ───────────────────────────
BOT_BLOCK_DOWNGRADE_STREAK = 3  # 直近何回連続でBot対策検知が続いたら降格するか
_CRAWL_METHOD_DOWNGRADE_PATH = {"HTML": "手動"}


def downgrade_bot_blocked_targets(client: SupabaseClient, targets: list) -> None:
    """直近BOT_BLOCK_DOWNGRADE_STREAK回連続でBot対策検知だったクロール先について、
    crawl_methodを段階的に降格する（HTML→ブラウザ操作→手動）。
    新しいステータス用カラムは設けず、既存のcrawl_method/crawl_notesのみを使う。"""
    for target in targets:
        current_method = target["crawl_method"]
        next_method = _CRAWL_METHOD_DOWNGRADE_PATH.get(current_method)
        if not next_method:
            continue
        recent_logs = client.select("crawl_logs", {
            "select": "notes",
            "crawl_target_id": f"eq.{target['crawl_target_id']}",
            "order": "started_at.desc",
            "limit": BOT_BLOCK_DOWNGRADE_STREAK,
        })
        if len(recent_logs) < BOT_BLOCK_DOWNGRADE_STREAK:
            continue
        if not all((log.get("notes") or "").startswith("Bot対策検知") for log in recent_logs):
            continue

        today = datetime.now(timezone.utc).date().isoformat()
        existing_notes = (target.get("crawl_notes") or "").strip()
        history_line = (f"[{today}] Bot対策検知が{BOT_BLOCK_DOWNGRADE_STREAK}回連続のため"
                         f"{current_method}→{next_method}へ自動変更")
        new_notes = f"{existing_notes}\n{history_line}" if existing_notes else history_line
        client.update("crawl_targets", {"crawl_target_id": f"eq.{target['crawl_target_id']}"},
                      {"crawl_method": next_method, "crawl_notes": new_notes})
        print(f"  [自動降格] {target.get('target_name')}: {current_method} → {next_method}")


# ─── メイン ───────────────────────────────────────────────────────
def _get_today_attempted_target_ids(client: SupabaseClient) -> set:
    """本日(UTC 00:00以降)既にcrawl_logsへ行が保存されたcrawl_target_idの集合を返す。
    2026-10-15追加: 日次バッチを複数回のチャンクに分けて実行する(run_daily.py参照)際、
    前のチャンクで既に試行済みのtargetを除外して「残り」だけを次のチャンクに回すために使う"""
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    rows = client.select("crawl_logs", {"select": "crawl_target_id", "started_at": f"gte.{today_start}"})
    return {r["crawl_target_id"] for r in rows}


def _budget_note(summary: dict) -> str:
    """Pass1の1行表示に付ける、1対象の時間上限で記事取得を打ち切ったことの注記（無ければ空）"""
    deferred, known = summary.get("budget_deferred") or 0, summary.get("budget_skipped_known") or 0
    if not deferred and not known:
        return ""
    return (f"・時間上限({PER_TARGET_BUDGET_SEC}秒)で{deferred}件を繰越"
            + (f"、既知{known}件は省略" if known else ""))


def main(limit: int = None, methods=SUPPORTED_METHODS, skip_today_attempted: bool = False):
    """skip_today_attempted=Trueの場合、本日既にcrawl_logsへ記録済みのtargetを除外してから
    limitを適用する(run_daily.pyがチャンク単位で繰り返し呼び出す際の「残りだけ処理する」用。
    2026-10-15追加。デフォルトFalseのため、手動実行やテストでの既存の挙動は変わらない)"""
    config = load_config()
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)
    client = SupabaseClient(config)

    targets = client.select("crawl_targets", {
        "select": "*",
        "crawl_method": f"in.({','.join(methods)})",
    })
    if skip_today_attempted:
        attempted_ids = _get_today_attempted_target_ids(client)
        before = len(targets)
        targets = [t for t in targets if t["crawl_target_id"] not in attempted_ids]
        print(f"本日試行済み{before - len(targets)}件を除外（残り{len(targets)}件）")
    if limit:
        targets = targets[:limit]

    print(f"クロール対象: {len(targets)}件（{'/'.join(methods)}）")
    totals = {"成功": 0, "一部成功": 0, "失敗": 0, "更新なし": 0, "抽出失敗": 0}
    pending_escalations = []
    pending_listing_escalations = []
    try:
        for i, target in enumerate(targets, 1):
            name = (target.get("target_name") or "")[:30]
            print(f"[{i}/{len(targets)}] {name} ({target['crawl_method']}) ...", end=" ", flush=True)
            try:
                summary = process_target(target, client, proxies, verify)
            except Exception as e:
                print(f"予期しないエラー: {type(e).__name__}: {e}")
                continue
            totals[summary["run_result"]] = totals.get(summary["run_result"], 0) + 1
            pending_escalations.extend(summary.get("pending_escalations") or [])
            if summary.get("pending_listing_escalation"):
                pending_listing_escalations.append(summary["pending_listing_escalation"])
            print(f"{summary['run_result']}"
                  f"（検出{summary['items_detected']}件 新規{summary['new_items']}件"
                  f" 更新{summary['updated_items']}件 抽出失敗{summary['extraction_failures']}件"
                  f" 要再試行{len(summary.get('pending_escalations') or [])}件"
                  f"{'・一覧取得は再試行へ回送' if summary.get('pending_listing_escalation') else ''}"
                  f"{_budget_note(summary)}）")
    finally:
        close_browser()

    print("─" * 40)
    print("Pass1(通常クロール)集計:", totals, "※Pass2での救済分は未反映の暫定値")

    if pending_listing_escalations:
        pending_escalations.extend(
            escalate_pending_listings(pending_listing_escalations, client, proxies, verify))

    if pending_escalations:
        print(f"\nPass2開始: 全target分の要再試行{len(pending_escalations)}件をまとめて処理します")
        escalate_pending(pending_escalations, client, proxies, verify)

    print("─" * 40)
    downgrade_bot_blocked_targets(client, targets)


# ─── Pass2バックログ: 時間切れ等で未完了になったPass2を引き継ぐ（2026-10-19追加） ───────
def load_pass2_backlog(client: SupabaseClient, now=None, max_age_hours: int = PENDING_MAX_AGE_HOURS) -> tuple:
    """crawl_logs.notesから、未完了のPass2を復元する。
    戻り値: (article_items, listing_items, expired)
      article_items: 記事単位の再試行待ち（escalate_pending()へ渡せる形）
      listing_items: 一覧取得が未完了のtarget（escalate_pending_listings()へ渡せる形）
      expired: max_age_hoursを超えても未解消のログ行 [(row, funnel, prefix)]（失敗として確定する）"""
    now = now or datetime.now(timezone.utc)
    rows = client.select("crawl_logs", {
        "select": "crawl_log_id,crawl_target_id,started_at,notes",
        "started_at": f"gte.{(now - timedelta(days=14)).isoformat()}",
        "or": "(notes.like.*p1_pending*,notes.like.*p1_listing_failed*,notes.like.*p1_routed_to_pass2*)",
        "order": "started_at.asc",
    })
    targets = {t["crawl_target_id"]: t for t in client.select("crawl_targets", {"select": "*"})}
    article_items, listing_items, expired = [], [], []
    # 予算内で全部は終わらない場合に備え、新しいログ行から先に処理する（古いものは期限切れで
    # 失敗として確定される。鮮度の高い記事を優先して取り込むため）。1行内の記事の並びは保つ
    for row in reversed(rows):
        prefix, funnel = _split_notes(row.get("notes"))
        pending = funnel.get("p1_pending") or []
        listing_open = bool(funnel.get("p1_listing_failed") or funnel.get("p1_routed_to_pass2")) \
            and not funnel.get("listing_retry")
        if not pending and not listing_open:
            continue
        started = _parse_datetime(row.get("started_at")) or now
        target = targets.get(row["crawl_target_id"])
        too_old = (now - started).total_seconds() / 3600 > max_age_hours
        if too_old or target is None:
            expired.append((row, funnel, prefix))
            continue
        lookback = target.get("lookback_days") or DEFAULT_LOOKBACK_DAYS
        for entry in pending:
            article_items.append({
                "target": target, "cand": _entry_to_cand(entry), "crawl_log_id": row["crawl_log_id"],
                "lookback": lookback, "is_bot_block": bool(entry.get("b")),
            })
        if listing_open:
            item = {"target": target, "crawl_log_id": row["crawl_log_id"], "lookback": lookback,
                    "started_at": started}
            if funnel.get("p1_routed_to_pass2") and target.get("crawl_method") in _ZYTE_METHOD_TIERS:
                item["tiers"] = _ZYTE_METHOD_TIERS[target["crawl_method"]]
            listing_items.append(item)
    return article_items, listing_items, expired


def _expire_backlog_row(client: SupabaseClient, row: dict, funnel: dict, prefix: str) -> None:
    """再試行できないまま期限切れになったログ行を、失敗として確定する
    （「更新なし」のまま取りこぼしが見えなくなるのを防ぐ）"""
    crawl_log_id = row["crawl_log_id"]
    pending_count = len(funnel.get("p1_pending") or []) + int(funnel.get("p1_pending_overflow") or 0)
    if funnel.get("p1_pending"):
        update_crawl_log_after_escalation(
            client, crawl_log_id, final_failures=pending_count,
            funnel_patch={"p1_pending": None, "pass2": "expired", "final_failed": pending_count})
    elif funnel.get("p1_listing_failed") or funnel.get("p1_routed_to_pass2"):
        _update_crawl_log_full(
            client, crawl_log_id, datetime.now(timezone.utc), "失敗", None, 0, 0, 0, 0,
            "一覧取得のPass2が期限内に実行されないまま期限切れ",
            notes=_compose_notes(prefix, {**funnel, "listing_retry": "expired"}))


# ─── バックログ実行の記録（ログファイル・JSONレポート・異常検出・前回比） ───────────────
BACKLOG_LOG_DIR = Path(__file__).parent / "logs" / "backlog"
_JST = timezone(timedelta(hours=9))
# 異常検出のしきい値
ANOMALY_MIN_TIER_ATTEMPTS = 10       # この件数以上試して成功0なら、段階（サービス・鍵・ネットワーク）の障害を疑う
ANOMALY_FAILURE_RATE = 0.5           # 最終失敗または一覧取得失敗の割合がこれ以上なら警告
ANOMALY_SLOWDOWN_FACTOR = 2.0        # 実測が仮置き(BACKLOG_*_SEC)の何倍以上遅ければ警告
ANOMALY_OVERRUN_FACTOR = 1.1         # 予算の何倍を超えて動いたら警告


class _Tee:
    """標準出力・標準エラーを、画面とログファイルの両方へ書く"""

    def __init__(self, stream, file):
        self._stream, self._file = stream, file

    def write(self, data):
        self._stream.write(data)
        try:
            self._file.write(data)
            self._file.flush()
        except ValueError:
            pass  # ファイルが閉じた後の書き込みは無視する
        return len(data)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def detect_anomalies(report: dict) -> list:
    """実行レポートから、確認が必要な点を日本語の文のリストで返す（空なら異常なし）"""
    out = []
    art = report.get("articles") or {}
    for tier, (attempted, ok, sec) in (art.get("tiers") or {}).items():
        if attempted >= ANOMALY_MIN_TIER_ATTEMPTS and ok == 0:
            out.append(f"{tier}が{attempted}件試して成功0件です（サービス障害・鍵・ネットワークの可能性）")
    zyte_403 = sum(v for k, v in (art.get("errors") or {}).items() if k.startswith("zyte|") and "403" in k)
    if zyte_403 >= 5:
        out.append(f"Zyteが403を{zyte_403}件返しています（アカウント停止・利用上限の可能性。"
                   "https://app.zyte.com/o/settings/billing-subscriptions を確認）")
    attempted_items = art.get("attempted_items", 0)
    if attempted_items and art.get("final_failed", 0) / attempted_items >= ANOMALY_FAILURE_RATE:
        out.append(f"記事の最終失敗が{art['final_failed']}/{attempted_items}件と半数以上です")
    if art.get("unfinished"):
        out.append(f"時間予算を使い切り、記事{art['unfinished']}件が次回へ繰り越されました（予算が足りない可能性）")
    if art.get("agent_cap_reached"):
        out.append(f"TinyFish Agentの上限（{report.get('agent_cap')}件）に達しました。残りは最終失敗として確定しています")
    listing = report.get("listing") or []
    done = [x for x in listing if x["outcome"] in ("ok", "failed")]
    if done and sum(1 for x in done if x["outcome"] == "failed") / len(done) >= ANOMALY_FAILURE_RATE:
        failed = sum(1 for x in done if x["outcome"] == "failed")
        out.append(f"一覧取得の再試行が{failed}/{len(done)}対象で失敗しています")
    skipped = sum(1 for x in listing if x["outcome"] == "skipped")
    if skipped:
        out.append(f"時間予算切れで、一覧取得{skipped}対象が次回へ繰り越されました")
    if report.get("expired"):
        out.append(f"期限切れ{report['expired']}ログ行を失敗として確定しました（再試行が間に合いませんでした）")
    budget = report.get("budget_sec") or 0
    if budget and report.get("elapsed_sec", 0) > budget * ANOMALY_OVERRUN_FACTOR:
        out.append(f"実行時間{report['elapsed_sec']:.0f}秒が予算{budget}秒を超えました（1件の処理が長すぎる可能性）")
    n_articles = attempted_items
    if n_articles and report.get("phase_sec", {}).get("articles", 0) / n_articles > \
            BACKLOG_ARTICLE_SEC * ANOMALY_SLOWDOWN_FACTOR:
        per = report["phase_sec"]["articles"] / n_articles
        out.append(f"記事1件あたり{per:.0f}秒で、仮置き({BACKLOG_ARTICLE_SEC}秒)の{ANOMALY_SLOWDOWN_FACTOR:.0f}倍以上です"
                   "（予算の自動算出を見直してください）")
    if done:
        avg = sum(x["sec"] for x in done) / len(done)
        if avg > BACKLOG_LISTING_SEC * ANOMALY_SLOWDOWN_FACTOR:
            out.append(f"一覧取得は1対象あたり平均{avg:.0f}秒で、仮置き({BACKLOG_LISTING_SEC}秒)の"
                       f"{ANOMALY_SLOWDOWN_FACTOR:.0f}倍以上です")
    if report.get("error"):
        out.append(f"実行中に例外が発生しました: {report['error']}")
    return out


def _previous_backlog_report(log_dir: Path, current_name: str):
    """直前の実行レポート(JSON)を読む。無ければNone"""
    try:
        files = sorted(p for p in log_dir.glob("*_backlog.json") if p.name != current_name)
        return json.loads(files[-1].read_text(encoding="utf-8")) if files else None
    except (OSError, ValueError):
        return None


def _render_backlog_summary(report: dict, previous: dict = None) -> str:
    art = report.get("articles") or {}
    listing = report.get("listing") or []
    ok = sum(1 for x in listing if x["outcome"] == "ok")
    failed = sum(1 for x in listing if x["outcome"] == "failed")
    skipped = sum(1 for x in listing if x["outcome"] == "skipped")
    before, after = report["before"], report.get("after") or {}
    lines = ["=" * 60, f"[backlog-summary] {report['started_at']} 〜 {report['finished_at']}",
             f"  所要 {report['elapsed_sec']:.0f}秒 / 予算 {report['budget_sec']}秒"
             f"（記事 {report['phase_sec']['articles']:.0f}秒、一覧取得 {report['phase_sec']['listing']:.0f}秒）"
             f" / 段階の順番 {'→'.join(report['tier_order'])} / Agent上限 {report['agent_cap']}件",
             f"  処理前: 記事{before['article_items']}件・一覧取得{before['listing_items']}対象・期限切れ{before['expired']}行"
             + (f" → 処理後(繰越): 記事{after['article_items']}件・一覧取得{after['listing_items']}対象"
                if after else ""),
             f"  記事: 救済{art.get('recovered', 0)}件 / 最終失敗{art.get('final_failed', 0)}件 / "
             f"未完了{art.get('unfinished', 0)}件（対象{art.get('attempted_items', 0)}件）",
             f"  一覧取得: 成功{ok} / 失敗{failed} / 繰越{skipped}"]
    for tier, (a, o, sec) in (art.get("tiers") or {}).items():
        lines.append(f"  [{tier}] 投入{a}→成功{o}"
                     + (f"（成功率{o / a:.0%}、平均{sec / a:.0f}秒/件）" if a else ""))
    if art.get("errors"):
        top = sorted(art["errors"].items(), key=lambda kv: -kv[1])[:5]
        lines.append("  失敗理由の上位: " + " / ".join(f"{k}×{v}" for k, v in top))
    if previous:
        pb = previous.get("before", {})
        lines.append(f"  前回比: 記事{pb.get('article_items', '?')}→{before['article_items']}件、"
                     f"一覧取得{pb.get('listing_items', '?')}→{before['listing_items']}対象、"
                     f"所要{previous.get('elapsed_sec', 0):.0f}→{report['elapsed_sec']:.0f}秒（前回 {previous.get('started_at')}）")
    anomalies = report.get("anomalies") or []
    lines.append("  異常: なし" if not anomalies else "  異常の可能性:")
    lines += [f"    ! {a}" for a in anomalies]
    lines.append("=" * 60)
    return "\n".join(lines)


def run_pass2_backlog(budget_sec: int = None, client: SupabaseClient = None,
                       proxies: dict = None, verify: bool = None, agent_cap: int = None,
                       log_dir: Path = None) -> dict:
    """未完了のPass2(記事単位の再試行待ち・一覧取得の再試行)を、時間予算つきで引き継ぐ。
    run_daily.pyが、crawlステップの直後に呼ぶ。終わらなかった分は次回へ繰り越す。
    順序: ①記事単位の再試行待ち(安価) → ②一覧取得の再試行(高価) → ③一覧から出た新しい記事
    budget_sec=None(既定)なら、件数から自動算出する(compute_backlog_budget)。
    agent_cap: このプロセスで使うTinyFish Agentの上限件数（既定BACKLOG_AGENT_CAP）

    実行の記録(2026-10-19): 画面に出る内容をlogs/backlog/<日時>_backlog.logへも保存し、
    件数・段階別の成功率と秒数・失敗理由・一覧取得の対象別結果・異常の可能性・前回比を
    logs/backlog/<日時>_backlog.json にまとめる（例外で止まった場合も書き出してから再送出する）"""
    log_dir = Path(log_dir) if log_dir is not None else BACKLOG_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_JST).strftime("%Y%m%d_%H%M%S")
    log_path, json_path = log_dir / f"{stamp}_backlog.log", log_dir / f"{stamp}_backlog.json"
    with open(log_path, "w", encoding="utf-8") as log_file:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(old_out, log_file), _Tee(old_err, log_file)
        try:
            return _run_pass2_backlog(budget_sec, client, proxies, verify, agent_cap, log_path, json_path)
        finally:
            sys.stdout, sys.stderr = old_out, old_err


def _run_pass2_backlog(budget_sec, client, proxies, verify, agent_cap, log_path: Path, json_path: Path) -> dict:
    global TINYFISH_AGENT_RUN_CAP, _tinyfish_agent_call_count
    TINYFISH_AGENT_RUN_CAP = BACKLOG_AGENT_CAP if agent_cap is None else agent_cap
    _tinyfish_agent_call_count = 0
    _tinyfish_agent_site_calls.clear()
    started_at = datetime.now(_JST)
    if client is None:
        config = load_config()
        client = SupabaseClient(config)
        proxies = make_proxies(config)
        verify = config.get("ssl", {}).get("verify", True)
    article_items, listing_items, expired = load_pass2_backlog(client)
    auto = budget_sec is None
    if auto:
        budget_sec = compute_backlog_budget(len(article_items), len(listing_items))
    deadline = time.monotonic() + budget_sec
    print(f"Pass2バックログ: 記事単位{len(article_items)}件 / 一覧取得{len(listing_items)}対象 / "
          f"期限切れ{len(expired)}ログ行（時間予算{budget_sec}秒{'・件数から自動算出' if auto else ''}、"
          f"段階の順番={'→'.join(ESCALATION_TIERS)}、Agent上限{TINYFISH_AGENT_RUN_CAP}件、"
          f"Agent実行上限{TINYFISH_AGENT_MAX_DURATION_SEC}秒"
          f"{('/' + str(TINYFISH_AGENT_MAX_STEPS) + '操作') if TINYFISH_AGENT_MAX_STEPS > 0 else ''}(サービス側)・"
          f"クライアント見張り{TINYFISH_AGENT_TIMEOUT}秒、Zyte={'有効' if ZYTE_ENABLED else '停止中'}）")
    print(f"ログ: {log_path}")
    for row, funnel, prefix in expired:
        _expire_backlog_row(client, row, funnel, prefix)
    result = {"article_items": len(article_items), "listing_items": len(listing_items), "expired": len(expired),
              "budget_sec": budget_sec}
    stats = {}
    error_text = None
    started = time.monotonic()
    articles_sec = 0.0
    try:
        if article_items:
            result["articles"] = escalate_pending(article_items, client, proxies, verify,
                                                  deadline=deadline, stats=stats)
        articles_sec = time.monotonic() - started
        if listing_items:
            new_articles = escalate_pending_listings(listing_items, client, proxies, verify,
                                                     deadline=deadline, stats=stats)
            if new_articles:
                result["listing_articles"] = escalate_pending(new_articles, client, proxies, verify,
                                                               deadline=deadline, stats=stats)
    except Exception as e:  # noqa: BLE001  レポートを書き出してから再送出する
        import traceback
        error_text = f"{type(e).__name__}: {e}"
        print(f"[backlog] 例外で中断: {error_text}\n{traceback.format_exc()}")
        raise
    finally:
        close_browser()
        total_sec = time.monotonic() - started
        # 処理後に残っている繰越を数える（読み取りのみ。失敗してもレポート出力は続ける）
        try:
            a_after, l_after, e_after = load_pass2_backlog(client)
            after = {"article_items": len(a_after), "listing_items": len(l_after), "expired": len(e_after)}
        except Exception:  # noqa: BLE001
            after = None
        report = {
            "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S JST"),
            "finished_at": datetime.now(_JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "budget_sec": budget_sec, "budget_auto": auto, "elapsed_sec": round(total_sec, 1),
            "phase_sec": {"articles": round(articles_sec, 1),
                          "listing": round(max(total_sec - articles_sec, 0), 1)},
            "agent_cap": TINYFISH_AGENT_RUN_CAP, "tier_order": list(ESCALATION_TIERS),
            "before": {"article_items": len(article_items), "listing_items": len(listing_items),
                       "expired": len(expired)},
            "after": after, "expired": len(expired),
            "articles": stats.get("articles") or {}, "listing": stats.get("listing") or [],
            "error": error_text,
        }
        report["anomalies"] = detect_anomalies(report)
        previous = _previous_backlog_report(json_path.parent, json_path.name)
        print(_render_backlog_summary(report, previous))
        try:
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[backlog] レポート: {json_path}")
        except OSError as e:
            print(f"[backlog] レポートの保存に失敗: {e}")
    result["elapsed_sec"] = total_sec
    result["report_path"] = str(json_path)
    result["anomalies"] = report["anomalies"]
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("limit", nargs="?", type=int, default=None)
    parser.add_argument("--pass2-backlog", action="store_true",
                         help="未完了のPass2(再試行待ちの記事・一覧取得)を引き継いで処理する（run_daily.pyのcrawl直後に実行）")
    parser.add_argument("--budget-sec", type=int, default=None,
                         help="--pass2-backlogの時間予算（秒）。未指定なら件数から自動算出。超えた分は次回へ繰り越す")
    parser.add_argument("--agent-cap", type=int, default=None,
                         help=f"--pass2-backlogで使うTinyFish Agentの上限件数（既定{BACKLOG_AGENT_CAP}）")
    parser.add_argument("--resume", action="store_true",
                         help="本日(UTC)既に試行済みのtargetをスキップする（run_daily.pyのチャンク実行用）")
    args = parser.parse_args()
    if args.pass2_backlog:
        run_pass2_backlog(budget_sec=args.budget_sec, agent_cap=args.agent_cap)
    else:
        main(limit=args.limit, skip_today_attempted=args.resume)
