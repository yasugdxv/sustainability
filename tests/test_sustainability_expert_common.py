import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402

import sustainability_expert_common as common  # noqa: E402

DUMMY_API_KEY = "sk-DUMMY_SECRET_abcdef123456"
DUMMY_PROMPT_MARKER = "DUMMY_PROMPT_MARKER_dcba9876"

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}

_REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")


def _connection_error():
    return openai.APIConnectionError(
        message=f"Connection error. api_key={DUMMY_API_KEY} prompt={DUMMY_PROMPT_MARKER}",
        request=_REQUEST,
    )


def _rate_limit_error():
    resp = httpx.Response(429, request=_REQUEST, headers={"x-request-id": "req-abc-123"})
    return openai.RateLimitError(
        message=f"Rate limited. api_key={DUMMY_API_KEY} prompt={DUMMY_PROMPT_MARKER}",
        response=resp, body=None,
    )


def _mock_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                            completion_tokens_details=MagicMock(reasoning_tokens=0))
    return resp


def _formatted_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# ─── manual_json段階での例外変換 ───────────────────────────────────
@pytest.mark.parametrize("make_error", [_connection_error, _rate_limit_error])
def test_manual_json_stage_converts_api_errors_to_expert_llm_error(make_error, capsys):
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),  # 1回目(構造化出力)は既存通りExceptionで握りつぶされる
        make_error(),  # 2回目(手動JSON抽出)で対象例外が発生
    ]

    with pytest.raises(common.ExpertLLMError) as exc_info:
        common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")

    exc = exc_info.value
    assert str(exc) == "LLM呼び出しに失敗しました"
    assert exc.__cause__ is None

    tb_text = _formatted_traceback(exc)
    assert DUMMY_API_KEY not in tb_text
    assert DUMMY_PROMPT_MARKER not in tb_text

    out = capsys.readouterr().out
    assert "stage=manual_json" in out
    assert f"error_type={type(make_error()).__name__}" in out
    assert DUMMY_API_KEY not in out
    assert DUMMY_PROMPT_MARKER not in out


def test_manual_json_stage_prints_only_safe_fields(capsys):
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),
        _rate_limit_error(),
    ]

    with pytest.raises(common.ExpertLLMError):
        common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")

    out = capsys.readouterr().out.strip()
    assert out == (
        "LLM call failed: stage=manual_json "
        "error_type=RateLimitError "
        "status=429 "
        "request_id=req-abc-123"
    )


# ─── correction_retry段階での例外変換 ──────────────────────────────
@pytest.mark.parametrize("make_error", [_connection_error, _rate_limit_error])
def test_correction_retry_stage_converts_api_errors_to_expert_llm_error(make_error, capsys):
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),  # 1回目: 構造化出力(握りつぶし)
        _mock_response("not a json object"),  # 2回目: 手動JSON抽出は成功するがSchema検証に失敗
        make_error(),  # 3回目: 訂正再実行で対象例外が発生
    ]

    with pytest.raises(common.ExpertLLMError) as exc_info:
        common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")

    exc = exc_info.value
    assert str(exc) == "LLM呼び出しに失敗しました"
    assert exc.__cause__ is None

    tb_text = _formatted_traceback(exc)
    assert DUMMY_API_KEY not in tb_text
    assert DUMMY_PROMPT_MARKER not in tb_text

    out = capsys.readouterr().out
    assert "stage=correction_retry" in out
    assert DUMMY_API_KEY not in out
    assert DUMMY_PROMPT_MARKER not in out


def test_correction_retry_stage_prints_only_safe_fields(capsys):
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),
        _mock_response("not a json object"),
        _connection_error(),
    ]

    with pytest.raises(common.ExpertLLMError):
        common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")

    out = capsys.readouterr().out.strip()
    assert out == (
        "LLM call failed: stage=correction_retry "
        "error_type=APIConnectionError "
        "status=None "
        "request_id=None"
    )


# ─── 対象外の例外は変換されず伝播すること(過剰変換の防止) ──────────────
def test_non_target_exception_propagates_unchanged_at_manual_json_stage():
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),
        TypeError("想定外のプログラムバグ"),
    ]

    with pytest.raises(TypeError, match="想定外のプログラムバグ"):
        common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")


def test_non_target_exception_propagates_unchanged_at_correction_retry_stage():
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),
        _mock_response("not a json object"),
        TypeError("想定外のプログラムバグ"),
    ]

    with pytest.raises(TypeError, match="想定外のプログラムバグ"):
        common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")


# ─── 正常系・既存フォールバックの非回帰 ─────────────────────────────
def test_structured_output_success_unchanged():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_response('{"ok": true}')

    result = common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")

    assert result["mode"] == "structured_output"
    assert result["data"] == {"ok": True}


def test_manual_json_success_unchanged():
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),
        _mock_response('{"ok": true}'),
    ]

    result = common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")

    assert result["mode"] == "manual_json"
    assert result["data"] == {"ok": True}


def test_correction_retry_success_unchanged():
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),
        _mock_response("not a json object"),
        _mock_response('{"ok": true}'),
    ]

    result = common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")

    assert result["mode"] == "manual_json_retry"
    assert result["data"] == {"ok": True}


def test_both_manual_json_attempts_fail_schema_still_raises_expert_llm_error():
    """既存の「2回ともSchema検証失敗」経路(今回変更していない箇所)が無変更であること"""
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("structured output not supported"),
        _mock_response("not a json object"),
        _mock_response("still not a json object"),
    ]

    with pytest.raises(common.ExpertLLMError, match="Schema検証が2回とも失敗しました"):
        common.call_llm_structured(client, "gpt-4o", "system", "user", SCHEMA, "TestSchema")
