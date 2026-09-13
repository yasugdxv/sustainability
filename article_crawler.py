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
import hashlib
import io
import json
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
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
SUPPORTED_METHODS = ("RSS", "HTML", "ブラウザ操作")

# PDF/.docxは本文抽出対応（下記DOCUMENT_EXTENSIONS）。それ以外の添付系拡張子は
# 記事本文として扱わない（.doc(旧形式)は対応ライブラリが無いため対象外）
DOCUMENT_EXTENSIONS = (".pdf", ".docx")
NON_ARTICLE_EXTENSIONS = (
    ".xlsx", ".xls", ".csv", ".doc", ".pptx", ".ppt",
    ".zip", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mp3",
)
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
        all_rows = []
        offset = 0
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
def _parse_datetime(value):
    """文字列をdatetime(UTC)へ変換する。失敗時はNone"""
    if not value:
        return None
    try:
        dt = dateutil_parser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _within_lookback(pub_dt, lookback_days: int) -> bool:
    """公開日が対象期間内か判定する。日付不明(None)は対象期間外として除外する"""
    if pub_dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return pub_dt >= cutoff


# trafilaturaは本文中のコピーライト年・設立年等の無関係な日付を、記事の公開日と
# 誤って抽出することがある（例: 明確な日付が無いページで2000-01-01を抽出）。
# 誤った古い日付のまま_within_lookbackに渡すと「本当は新しい記事」が正しい理由とは
# 違う形で除外されてしまうため、明らかに古すぎる日付は「不明」(None)として扱う
# （_within_lookbackの仕様上、不明な日付は対象期間外として除外される）。
_MIN_PLAUSIBLE_ARTICLE_YEAR = 2020


def _parse_content_date(value):
    """trafilaturaが本文から抽出した日付をパースする。実装上の理由は
    _MIN_PLAUSIBLE_ARTICLE_YEAR のコメントを参照"""
    dt = _parse_datetime(value)
    if dt is not None and dt.year < _MIN_PLAUSIBLE_ARTICLE_YEAR:
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
    title = (getattr(meta, "title", None) or "").strip() if meta else ""
    text = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    return {
        "title": title,
        "text": text,
        "published_at": getattr(meta, "creation_date", None) if meta else None,
        "updated_at": getattr(meta, "modification_date", None) if meta else None,
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


def _fetch_page(url: str, proxies: dict, verify: bool, use_browser: bool):
    """通常HTTPまたはブラウザでページを取得する。(html, final_url, status_code)を返す"""
    if use_browser:
        return _fetch_with_browser(url, proxies, verify)
    resp = requests.get(url, proxies=proxies, verify=verify,
                         timeout=REQUEST_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    return _decoded_html(resp), resp.url, resp.status_code


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
def extract_article(url: str, proxies: dict, verify: bool, use_browser: bool = False) -> dict:
    """記事URLから本文・タイトル・公開日を抽出する。PDF/.docxは専用抽出、
    それ以外はtrafilaturaでHTMLから抽出する。取得結果のタイトル・本文が
    文字化けして見える場合、ENCODING_CORRUPTION_MAX_RETRIES回まで再取得を試みる
    （プロキシ瞬断による破損を、リトライで正常な応答に置き換えるため）"""
    for attempt in range(ENCODING_CORRUPTION_MAX_RETRIES + 1):
        result = _extract_article_once(url, proxies, verify, use_browser)
        if not result.get("ok"):
            return result
        if not (_looks_encoding_corrupted(result.get("title", "")) or
                _looks_encoding_corrupted(result.get("text", "")[:500])):
            return result
        if attempt < ENCODING_CORRUPTION_MAX_RETRIES:
            time.sleep(ENCODING_CORRUPTION_RETRY_DELAY_SEC)
    result["encoding_suspect"] = True  # 全リトライ後も文字化けが疑われる（呼び出し元の判断に委ねる）
    return result


def _extract_article_once(url: str, proxies: dict, verify: bool, use_browser: bool = False) -> dict:
    doc_kind = _detect_document_kind(url)
    if doc_kind:
        use_browser = False  # 文書ファイルはブラウザ経由で扱わない（PDFビューア化を避ける）

    try:
        if use_browser:
            html, final_url, status_code = _fetch_with_browser(url, proxies, verify)
            content_type, raw_content = "", None
        else:
            resp = requests.get(url, proxies=proxies, verify=verify,
                                 timeout=REQUEST_TIMEOUT, headers=_HEADERS)
            resp.raise_for_status()
            html, final_url, status_code = _decoded_html(resp), resp.url, resp.status_code
            content_type, raw_content = resp.headers.get("Content-Type", ""), resp.content
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "http_status": None}

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
def list_rss_candidates(target: dict, proxies: dict, verify: bool) -> list:
    """RSSフィードから候補記事(URL・仮タイトル・公開日)を一覧する"""
    resp = requests.get(target["target_url"], proxies=proxies, verify=verify,
                         timeout=REQUEST_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    candidates = []
    for entry in feed.entries[:50]:
        link = entry.get("link", "")
        title = entry.get("title", "").strip()
        if not link or not title:
            continue
        # <link>が相対パスのRSSフィード（例: 防衛省, ファーストリテイリング）向けに
        # フィード自身のURL（リダイレクト後のresp.url）を基準に絶対URLへ解決する
        link = urljoin(resp.url, link)
        pp = entry.get("published_parsed") or entry.get("updated_parsed")
        pub_dt = datetime(*pp[:6], tzinfo=timezone.utc) if pp else None
        candidates.append({"url": link, "title": title, "published_at": pub_dt})
    return candidates, resp.status_code


# ─── XMLサイトマップ ─────────────────────────────────────────────
def list_sitemap_candidates(target: dict, proxies: dict, verify: bool) -> list:
    """XMLサイトマップ(<url><loc>...</loc><lastmod>...</lastmod></url>)から候補記事を
    一覧する。<a href>を汎用収集するlist_html_candidates()とは構造が異なる
    （例: Paboco等、更新頻度は低いがJS一覧の代わりにsitemap.xmlで全ページを把握できるサイト向け）"""
    url = target["target_url"]
    domain = target.get("domain") or urlparse(url).netloc
    include_paths = [p.strip() for p in (target.get("include_paths") or "").split(",") if p.strip()]
    exclude_paths = [p.strip() for p in (target.get("exclude_paths") or "").split(",") if p.strip()]

    resp = requests.get(url, proxies=proxies, verify=verify, timeout=REQUEST_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()

    candidates = []
    for block in re.findall(r"<url>(.*?)</url>", resp.text, re.S):
        loc_match = re.search(r"<loc>(.*?)</loc>", block)
        if not loc_match:
            continue
        loc = loc_match.group(1).strip()
        parsed = urlparse(loc)
        if not parsed.scheme.startswith("http") or domain not in parsed.netloc:
            continue
        path = parsed.path
        if path.lower().endswith(NON_ARTICLE_EXTENSIONS):
            continue
        if include_paths and not any(path.startswith(p) for p in include_paths):
            continue
        if exclude_paths and any(path.startswith(p) for p in exclude_paths):
            continue

        pub_dt = None
        lastmod_match = re.search(r"<lastmod>(.*?)</lastmod>", block)
        if lastmod_match:
            try:
                pub_dt = dateutil_parser.parse(lastmod_match.group(1).strip())
                pub_dt = pub_dt.astimezone(timezone.utc) if pub_dt.tzinfo else pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pub_dt = None

        candidates.append({"url": loc, "title": None, "published_at": pub_dt})
        if len(candidates) >= MAX_HTML_LINKS_PER_TARGET:
            break
    return candidates, resp.status_code


# ─── HTML（汎用） ─────────────────────────────────────────────────
def list_html_candidates(target: dict, proxies: dict, verify: bool, use_browser: bool = False) -> list:
    """HTML一覧ページから候補記事リンクを抽出する（<a>を汎用的に収集し、
    include_paths/exclude_paths・ドメイン一致で絞り込む）"""
    url = target["target_url"]
    domain = target.get("domain") or urlparse(url).netloc
    include_paths = [p.strip() for p in (target.get("include_paths") or "").split(",") if p.strip()]
    exclude_paths = [p.strip() for p in (target.get("exclude_paths") or "").split(",") if p.strip()]

    html, final_url, status_code = _fetch_page(url, proxies, verify, use_browser)

    tree = _safe_lxml_parse(html)
    seen = set()
    candidates = []
    for href in tree.xpath("//a/@href"):
        full = urljoin(final_url, href)
        parsed = urlparse(full)
        if not parsed.scheme.startswith("http"):
            continue
        if domain not in parsed.netloc:
            continue
        path = parsed.path
        # WWF(panda.org)のような旧CMSは記事識別子自体をクエリに埋め込む
        # （例: /?15703966/slug）ため、パスが空でクエリが数字始まりのものは
        # 通常のinclude_paths/exclude_paths判定（パスベース）を素通りさせて
        # 無条件で記事候補として扱う（このパターン以外はパスが空でも記事ではないため対象外）
        is_query_id_article = (path.rstrip("/") or "/") == "/" and re.match(r"^\d+/", parsed.query)
        if not is_query_id_article:
            if path.lower().endswith(NON_ARTICLE_EXTENSIONS):
                continue
            if include_paths and not any(path.startswith(p) for p in include_paths):
                continue
            if exclude_paths and any(path.startswith(p) for p in exclude_paths):
                continue
        normalized_path = path.rstrip("/") or "/"
        if is_query_id_article:
            normalized = f"{parsed.scheme}://{parsed.netloc}/?{parsed.query}"
        else:
            normalized = f"{parsed.scheme}://{parsed.netloc}{normalized_path}"
        if normalized in seen or normalized == url.rstrip("/"):
            continue
        seen.add(normalized)
        candidates.append({"url": normalized, "title": None, "published_at": None})
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
                  document_info: dict = None) -> tuple:
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
                    notes: str = None):
    # LinkedIn等、bot対策として非標準のHTTPステータス(例:999)を返すサイトがあり、
    # crawl_logs.http_statusのCHECK制約(100〜599)に違反してINSERT自体が失敗し
    # クロールログが1件も残らない不具合があったため、範囲外の値はNULLとして保存する
    if not (isinstance(http_status, int) and 100 <= http_status <= 599):
        http_status = None
    client.insert("crawl_logs", [{
        "crawl_log_id": str(uuid.uuid4()),
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


# ─── 1クロール先の処理 ───────────────────────────────────────────
def process_target(target: dict, client: SupabaseClient, proxies: dict, verify: bool) -> dict:
    method = target["crawl_method"]
    endpoint_type = target.get("endpoint_type") or ""
    lookback = target.get("lookback_days") or DEFAULT_LOOKBACK_DAYS
    started_at = datetime.now(timezone.utc)

    error_message = None
    http_status = None
    candidates = []
    # 単一ページ監視型（サイトトップ等）はリンク収集をせず、URL自体を1件として扱う。
    # 他の記事と同じくlookback_daysでフィルタする
    # （日付が抽出できない場合はNoneとして扱われ、_within_lookbackにより除外される）。
    single_page_mode = False

    # 'ブラウザ操作'はHTMLと同じ収集ロジックをPlaywright経由で行う（bot対策サイト向け）
    use_browser = (method == "ブラウザ操作")

    try:
        if method == "RSS":
            candidates, http_status = list_rss_candidates(target, proxies, verify)
        elif method in ("HTML", "ブラウザ操作"):
            if endpoint_type == "サイトマップ":
                candidates, http_status = list_sitemap_candidates(target, proxies, verify)
            elif endpoint_type in LISTING_ENDPOINT_TYPES:
                candidates, http_status = list_html_candidates(target, proxies, verify, use_browser=use_browser)
            else:
                # SINGLE_PAGE_ENDPOINT_TYPES、および未知のendpoint_typeは安全側で単一ページ扱い
                single_page_mode = True
                candidates = [{"url": target["target_url"], "title": None, "published_at": None}]
        else:
            raise ValueError(f"未対応のcrawl_method: {method}")
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"

    items_detected = 0
    new_items = 0
    updated_items = 0
    extraction_failures = 0
    bot_block_detections = 0

    if error_message is None:
        for cand in candidates:
            extracted = extract_article(cand["url"], proxies, verify, use_browser=use_browser)
            time.sleep(ARTICLE_FETCH_SLEEP)

            if single_page_mode:
                http_status = extracted.get("http_status")

            if not extracted["ok"]:
                extraction_failures += 1
                if (extracted.get("error") or "").startswith("Bot対策"):
                    bot_block_detections += 1
                continue

            # RSSは一覧取得の時点で候補ごとの正確な日付(published_parsed)を持っている
            # ため、それを優先する。HTML一覧ページ等、候補側に日付が無いものだけ
            # 本文からのtrafilatura抽出日付(誤抽出対策済み)にフォールバックする。
            pub_dt = cand.get("published_at") or extracted["published_at"]
            if not _within_lookback(pub_dt, lookback):
                continue

            title = extracted["title"] or cand.get("title") or ""

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

    if error_message:
        run_result = "失敗"
    elif extraction_failures > 0 and (new_items or updated_items):
        run_result = "一部成功"
    elif new_items or updated_items:
        run_result = "成功"
    elif extraction_failures > 0:
        # 候補は見つかったが全件で本文抽出に失敗した状態。「純粋に新着が無かった」
        # (更新なし)とは区別する（サイレントな抽出失敗を見逃さないため）
        run_result = "抽出失敗"
    else:
        run_result = "更新なし"

    crawl_notes = f"Bot対策検知{bot_block_detections}件" if bot_block_detections else None
    save_crawl_log(client, target, started_at, finished_at, run_result, http_status,
                    items_detected, new_items, updated_items, extraction_failures, error_message,
                    notes=crawl_notes)

    return {
        "run_result": run_result,
        "items_detected": items_detected,
        "new_items": new_items,
        "updated_items": updated_items,
        "extraction_failures": extraction_failures,
        "error_message": error_message,
        "bot_block_detections": bot_block_detections,
    }


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
def main(limit: int = None, methods=SUPPORTED_METHODS):
    config = load_config()
    proxies = make_proxies(config)
    verify = config.get("ssl", {}).get("verify", True)
    client = SupabaseClient(config)

    targets = client.select("crawl_targets", {
        "select": "*",
        "crawl_method": f"in.({','.join(methods)})",
    })
    if limit:
        targets = targets[:limit]

    print(f"クロール対象: {len(targets)}件（{'/'.join(methods)}）")
    totals = {"成功": 0, "一部成功": 0, "失敗": 0, "更新なし": 0, "抽出失敗": 0}
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
            print(f"{summary['run_result']}"
                  f"（検出{summary['items_detected']}件 新規{summary['new_items']}件"
                  f" 更新{summary['updated_items']}件 抽出失敗{summary['extraction_failures']}件）")
    finally:
        close_browser()

    print("─" * 40)
    print("集計:", totals)

    downgrade_bot_blocked_targets(client, targets)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=n)
