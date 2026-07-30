"""Unit proof for the A2 adapter additions (mock client, no network).

timeout_s and extra_body must be passed through; the Azure branch and the
default request shape stay byte-identical to before.
"""

from __future__ import annotations

import pytest

import rag_assistant.llm.openai_provider as op_mod
from conftest import make_settings
from rag_assistant.llm.openai_provider import OpenAIProvider, parse_extra_body
from rag_assistant.llm.registry import ProviderRegistry
from rag_assistant.ports import LLMMessage

MSGS = [LLMMessage(role="user", content="hi")]


class _Choice:
    def __init__(self):
        self.message = type("M", (), {"content": "ok"})()


class _Resp:
    def __init__(self):
        self.choices = [_Choice()]
        self.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 2})()


class _StreamChunk:
    def __init__(self):
        delta = type("D", (), {"content": "hi"})()
        self.choices = [type("C", (), {"delta": delta})()]


class RecordingCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):

            async def agen():
                yield _StreamChunk()

            return agen()
        return _Resp()


def make_provider(**overrides) -> tuple[OpenAIProvider, RecordingCompletions]:
    # dummy key: the SDK client refuses to construct without one; the fake
    # completions object below guarantees no request ever leaves the test
    provider = OpenAIProvider("t", "model-x", make_settings(), api_key="test-key", **overrides)
    rec = RecordingCompletions()
    provider._client = type("FakeClient", (), {"chat": type("Chat", (), {"completions": rec})()})()  # type: ignore[assignment]
    return provider, rec


class RecordingClientFactory:
    """Stands in for AsyncOpenAI/AsyncAzureOpenAI to capture constructor kwargs."""

    def __init__(self):
        self.kwargs: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        return type("FakeSdkClient", (), {})()


# ── request-level passthrough ────────────────────────────────────────────────
async def test_default_request_shape_is_unchanged():
    provider, rec = make_provider()
    await provider.complete(MSGS)
    [call] = rec.calls
    assert "extra_body" not in call
    assert call["max_completion_tokens"] == 1024
    [_] = [c async for c in provider.stream(MSGS)]
    assert "extra_body" not in rec.calls[1]


async def test_extra_body_is_passed_in_complete_and_stream():
    body = {"chat_template_kwargs": {"enable_thinking": False}}
    provider, rec = make_provider(extra_body=body)
    await provider.complete(MSGS)
    assert rec.calls[0]["extra_body"] == body
    [_] = [c async for c in provider.stream(MSGS)]
    assert rec.calls[1]["extra_body"] == body


# ── constructor-level passthrough ────────────────────────────────────────────
def test_timeout_reaches_the_sdk_client(monkeypatch):
    factory = RecordingClientFactory()
    monkeypatch.setattr(op_mod, "AsyncOpenAI", factory)
    OpenAIProvider("t", "m", make_settings(), timeout_s=7.5)
    assert factory.kwargs[0]["timeout"] == 7.5


def test_no_timeout_keeps_the_sdk_default(monkeypatch):
    factory = RecordingClientFactory()
    monkeypatch.setattr(op_mod, "AsyncOpenAI", factory)
    OpenAIProvider("t", "m", make_settings())
    assert "timeout" not in factory.kwargs[0]


def test_azure_branch_is_untouched(monkeypatch):
    azure_factory = RecordingClientFactory()
    plain_factory = RecordingClientFactory()
    monkeypatch.setattr(op_mod, "AsyncAzureOpenAI", azure_factory)
    monkeypatch.setattr(op_mod, "AsyncOpenAI", plain_factory)
    settings = make_settings(
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key="k",
        azure_openai_api_version="2024-06-01",
    )
    OpenAIProvider("t", "m", settings, timeout_s=7.5)
    assert plain_factory.kwargs == []
    assert azure_factory.kwargs == [
        {
            "azure_endpoint": "https://example.openai.azure.com",
            "api_key": "k",
            "api_version": "2024-06-01",
        }
    ]


# ── OPENAI_EXTRA_BODY parsing (fail-closed) ──────────────────────────────────
def test_parse_extra_body_empty_is_none():
    assert parse_extra_body("") is None
    assert parse_extra_body("   ") is None


def test_parse_extra_body_valid_object():
    assert parse_extra_body('{"a": {"b": false}}') == {"a": {"b": False}}


def test_parse_extra_body_invalid_json_fails_closed():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_extra_body("{not json")


def test_parse_extra_body_non_object_fails_closed():
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_extra_body("[1, 2]")


def test_registry_wires_extra_body_into_the_openai_providers():
    s = make_settings(openai_api_key="sk-test-not-real", openai_extra_body='{"x": 1}')
    registry = ProviderRegistry(s)
    assert registry.providers["openai-mini"]._extra_body == {"x": 1}
    assert registry.providers["openai-strong"]._extra_body == {"x": 1}
