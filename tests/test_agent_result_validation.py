"""TinyFish Agentの返答の検証のテスト（2026-10-20追加）

背景: Agentは、ページが読めなかったときも status=COMPLETED で、「取得できなかった」という説明文を
返してくる（実測: {"result": "I was unable to extract the content from ..."}）。従来はこれを記事の本文として
扱い、consilium.europa.euの記事2件で、Agentの失敗メッセージが記事として保存されていた。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

import article_crawler as ac  # noqa: E402

BODY = "これは記事の本文です。" * 30                    # 十分な長さの本文


# ─── 成功の返答 ───
def test_structured_success_returns_title_and_body():
    text = ac._parse_agent_result({"status": "success", "title": "見出し", "body": BODY})
    assert text.startswith("見出し") and BODY in text


def test_structured_success_given_as_a_json_string_and_with_code_fence():
    raw = json.dumps({"status": "success", "title": "見出し", "body": BODY}, ensure_ascii=False)
    assert BODY in ac._parse_agent_result(raw)
    assert BODY in ac._parse_agent_result("```json\n" + raw + "\n```")


def test_nested_result_key_is_unwrapped():
    """実測: Agentの返答は {"result": "<文字列>"} の入れ子で来ることがある"""
    raw = json.dumps({"status": "success", "title": "見出し", "body": BODY}, ensure_ascii=False)
    assert BODY in ac._parse_agent_result({"result": raw})


def test_plain_article_text_without_failure_wording_is_still_accepted():
    plain = "The European Commission announced a new package of measures today. " * 12
    assert ac._parse_agent_result(plain).startswith("The European Commission")


# ─── 失敗の返答（記事として保存してはいけない） ───
def test_structured_failure_raises_with_the_reason():
    with pytest.raises(RuntimeError, match="目標未達.*CAPTCHA"):
        ac._parse_agent_result({"status": "failure", "reason": "CAPTCHAで先へ進めなかった"})


@pytest.mark.parametrize("text", [
    # 実測(10/19〜20): 失敗の説明文が、記事本文として保存されていた
    "I was unable to extract the content from the specific disclosure page at https://www.7andi.com/ir/disclose/r"
    + "x" * 300,
    "Extracted the main article title and body text from the page. The page is blocked by a bot protection screen "
    '("Checking your browser before accessing a G' + "x" * 300,
    "The page was blocked by a Cloudflare \"Checking your browser\" verification screen that prevented d" + "x" * 300,
    "Access denied. " + "x" * 400,
    "Please solve the CAPTCHA to continue. " + "x" * 400,
])
def test_failure_explanations_are_rejected_even_when_long(text):
    with pytest.raises(RuntimeError, match="取得できなかった旨"):
        ac._parse_agent_result({"result": text})
    with pytest.raises(RuntimeError, match="取得できなかった旨"):
        ac._parse_agent_result(text)


def test_short_answers_are_rejected():
    with pytest.raises(RuntimeError, match="短すぎます"):
        ac._parse_agent_result("OK")
    with pytest.raises(RuntimeError, match="短すぎます"):
        ac._parse_agent_result({"status": "success", "title": "t", "body": "短い"})


def test_empty_body_is_rejected():
    with pytest.raises(RuntimeError, match="本文が空"):
        ac._parse_agent_result({"status": "success", "title": "t", "body": ""})


def test_failure_wording_deep_inside_a_long_article_is_not_a_false_positive():
    """ニュース本文に「blocked」等が出てくるのは普通。判定は先頭付近だけに限る（記事の誤判定を避ける）"""
    body = ("The minister confirmed the plan. " * 20) + "Protesters blocked the road and I cannot say more. " + "x" * 400
    assert ac._parse_agent_result(body).startswith("The minister")


# ─── 呼び出し全体で、失敗の説明文が記事にならない ───
class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


def test_agent_call_with_failure_explanation_raises_instead_of_returning_an_article(monkeypatch):
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    failure = {"result": "I was unable to extract the content from the page. " + "y" * 300}
    lines = ["data: " + json.dumps({"type": "COMPLETE", "status": "COMPLETED", "result": failure})]
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _FakeStream(lines))

    with pytest.raises(RuntimeError, match="取得できなかった旨"):
        ac._call_tinyfish_agent("https://example.com", {}, True)


def test_the_instruction_asks_for_a_structured_reply_with_an_explicit_failure_form():
    goal = ac.TINYFISH_AGENT_GOAL
    assert '"status": "success"' in goal and '"status": "failure"' in goal and "JSON" in goal
    assert "bot-check" in goal or "CAPTCHA" in goal


def test_sse_stream_is_decoded_as_utf_8_so_japanese_text_is_not_garbled(monkeypatch):
    """Content-Typeにcharsetが無いと、requestsはISO-8859-1と誤判定して、日本語が文字化けする。
    SSEはUTF-8（仕様）なので、読む前にutf-8を指定する"""
    monkeypatch.setattr(ac, "_tinyfish_api_key_cache", "dummy")
    holder = {}

    class Stream(_FakeStream):
        encoding = "ISO-8859-1"                          # requestsの誤判定を再現

        def iter_lines(self, decode_unicode=True):
            holder["encoding_when_reading"] = self.encoding
            return iter(["data: " + json.dumps({"type": "COMPLETE", "status": "COMPLETED",
                         "result": {"status": "success", "title": "決算", "body": BODY}}, ensure_ascii=False)])

    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: Stream([]))

    html, _, _ = ac._call_tinyfish_agent("https://example.com", {}, True)

    assert holder["encoding_when_reading"] == "utf-8" and "決算" in html
