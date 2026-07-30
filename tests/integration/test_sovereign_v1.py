"""Sovereign endpoint protocol proof (A2, @ollama — owner's Mac only).

The ONE OpenAI adapter speaks the OpenAI protocol against a real foreign
endpoint: Ollama's /v1 compatibility layer stands in for any sovereign
vLLM-class service. Which contract partner runs behind the URL in production
is an operator decision — the mechanism is what is proven here
(mechanism != integration).

Run with a live Ollama: `make test-sovereign`. Deliberately NOT in CI.
Deliberately NO latency assertions — CPU latency varies by orders of
magnitude (cold load vs warm model); findings are printed as text instead.
"""

from __future__ import annotations

import json
import os

import openai
import pytest

from rag_assistant.config import Settings
from rag_assistant.llm.openai_provider import OpenAIProvider
from rag_assistant.ports import LLMMessage

pytestmark = pytest.mark.ollama

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

MESSAGES = [
    LLMMessage(role="system", content="Antworte kurz auf Deutsch."),
    LLMMessage(role="user", content="Nenne die Hauptstadt von Deutschland."),
]


def make_provider(
    *, model: str = MODEL, base_url: str = "", timeout_s: float | None = None
) -> OpenAIProvider:
    # Explicitly empty cloud/Azure settings: the adapter must take the plain
    # OpenAI-client branch and speak ONLY to the given base_url.
    settings = Settings(_env_file=None, openai_api_key="", azure_openai_endpoint="")
    return OpenAIProvider(
        "sovereign-probe",
        model,
        settings,
        base_url=base_url or f"{OLLAMA_URL}/v1",
        api_key="ollama",
        kind="sovereign",
        timeout_s=timeout_s,
    )


async def test_complete_happy_path_reports_usage():
    result = await make_provider().complete(MESSAGES, max_tokens=200)
    assert result.text.strip()
    assert result.input_tokens >= 0
    assert result.output_tokens >= 0
    # finding (4): is usage filled over /v1?
    print(
        f"\n[finding 4] usage over /v1: input_tokens={result.input_tokens} "
        f"output_tokens={result.output_tokens}"
    )


async def test_stream_happy_path():
    chunks = [c async for c in make_provider().stream(MESSAGES, max_tokens=200)]
    assert len(chunks) >= 1
    assert "".join(chunks).strip()
    print(f"\n[finding] stream over /v1: {len(chunks)} chunks")


async def test_json_mode_returns_valid_json():
    msgs = [
        LLMMessage(
            role="user",
            content='Antworte als JSON-Objekt: {"stadt": "..."} für die Hauptstadt Deutschlands.',
        )
    ]
    try:
        result = await make_provider().complete(msgs, max_tokens=200, json_mode=True)
    except openai.BadRequestError as exc:
        # finding (2): documented limitation, not silently dropped
        print(f"\n[finding 2] response_format unsupported over /v1: {exc}")
        pytest.xfail("Ollama /v1 endpoint rejects response_format (finding for the PR text)")
    data = json.loads(result.text)
    assert isinstance(data, dict)
    print("\n[finding 2] response_format json_object IS supported over /v1")


async def test_unknown_model_raises_not_found():
    with pytest.raises(openai.NotFoundError):
        await make_provider(model="does-not-exist:0b").complete(MESSAGES, max_tokens=10)


async def test_connection_refused_raises_api_connection_error():
    # port 9 (discard) — nothing listens there; proves the error surface
    with pytest.raises(openai.APIConnectionError):
        await make_provider(base_url="http://127.0.0.1:9/v1").complete(MESSAGES, max_tokens=10)


async def test_timeout_is_passed_through_end_to_end():
    # 1 ms can never survive a real generation — proves the new timeout_s
    # reaches the SDK (APITimeoutError, a subclass of APIConnectionError).
    with pytest.raises(openai.APITimeoutError):
        await make_provider(timeout_s=0.001).complete(MESSAGES, max_tokens=10)


async def test_think_behaviour_over_v1_is_reported():
    """finding (1): does qwen3 emit chain-of-thought over /v1 (no `think`
    field there, unlike the native /api/chat adapter)? Both outcomes are
    valid findings — print, never assert on latency or content style."""
    result = await make_provider().complete(MESSAGES, max_tokens=800)
    assert result.text.strip()
    marker = "<think>" in result.text
    print(
        f"\n[finding 1] '<think>' marker in /v1 response: {marker} "
        f"(response length {len(result.text)} chars)"
    )
