"""OpenAI provider — Azure OpenAI is the SAME adapter behind config flags.

"EU residency is a config flag, not a rebuild": when the Azure settings are
filled, the official SDK's AzureOpenAI client is used with deployments;
otherwise plain OpenAI. Nothing else in the codebase changes.

`base_url`/`api_key` overrides point the same adapter at any OpenAI-compatible
endpoint (Anthropic's /v1 compatibility layer for the eval judge, a sovereign
EU inference service, a vLLM box). `kind` follows the endpoint, not the SDK:
an OpenAI-compatible sovereign endpoint is kind='sovereign' for the policy.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from openai import AsyncAzureOpenAI, AsyncOpenAI

from ..config import Settings
from ..ports import LLMMessage, LLMResult


def parse_extra_body(raw: str) -> dict | None:
    """Parse the OPENAI_EXTRA_BODY setting: a JSON object string, empty = None.

    Fail-closed config validation — invalid JSON or a non-object is a startup
    error (readiness.validate_config_values calls this), never a silent skip.
    """
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OPENAI_EXTRA_BODY is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("OPENAI_EXTRA_BODY must be a JSON object")
    return value


class OpenAIProvider:
    def __init__(
        self,
        name: str,
        model: str,
        settings: Settings,
        *,
        base_url: str = "",
        api_key: str = "",
        kind: str = "cloud",
        supports_json_object: bool = True,
        supports_temperature: bool = True,
        timeout_s: float | None = None,
        extra_body: dict | None = None,
    ):
        self.name = name
        self.kind = kind
        # Anthropic's OpenAI-compatible endpoint rejects the classic JSON mode
        # (400: "response_format.type: Input should be 'json_schema'"). With
        # False, json_mode relies on the prompt alone — callers prompt with
        # JSON few-shots and parse tolerantly (parse_json_loose).
        self.supports_json_object = supports_json_object
        # Newer Claude models deprecate sampling parameters outright
        # (400: "`temperature` is deprecated for this model"). With False the
        # parameter is omitted and the model samples at its default — accepted
        # judge variance (judge scores are a report metric, never a CI gate).
        self.supports_temperature = supports_temperature
        # Endpoint-specific request extras (A2): think-control differs per
        # server family (Ollama: native `think`; vLLM-class: e.g.
        # `chat_template_kwargs.enable_thinking`) — the adapter must be able to
        # send such fields without a code change. None = no field in requests.
        self._extra_body = extra_body
        if settings.azure_openai_endpoint and not base_url:
            self._client: AsyncOpenAI = AsyncAzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key,
                api_version=settings.azure_openai_api_version,
            )
        else:
            client_kwargs: dict[str, Any] = {}
            # Sovereign CPU/vLLM endpoints are slow — the SDK default timeout
            # must be steerable per instance (the Ollama adapter has the same).
            # Only pass the kwarg when set, so the default stays byte-identical.
            if timeout_s is not None:
                client_kwargs["timeout"] = timeout_s
            self._client = AsyncOpenAI(
                api_key=api_key or settings.openai_api_key,
                base_url=(base_url or settings.openai_base_url) or None,
                **client_kwargs,
            )
        self.model = model

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResult:
        kwargs: dict = {}
        if json_mode and self.supports_json_object:
            kwargs["response_format"] = {"type": "json_object"}
        if self.supports_temperature:
            kwargs["temperature"] = temperature
        if self._extra_body is not None:
            kwargs["extra_body"] = self._extra_body
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=cast(Any, [{"role": m.role, "content": m.content} for m in messages]),
            # newer models (GPT-5+) reject the legacy `max_tokens`; the forward-
            # compatible name is accepted by the 4o family too.
            max_completion_tokens=max_tokens,
            **kwargs,
        )
        usage = resp.usage
        return LLMResult(
            text=resp.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    async def _stream_gen(
        self, messages: Sequence[LLMMessage], temperature: float, max_tokens: int
    ) -> AsyncIterator[str]:
        kwargs: dict = {}
        if self.supports_temperature:
            kwargs["temperature"] = temperature
        if self._extra_body is not None:
            kwargs["extra_body"] = self._extra_body
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=cast(Any, [{"role": m.role, "content": m.content} for m in messages]),
            max_completion_tokens=max_tokens,
            stream=True,
            **kwargs,
        )
        async for chunk in cast(Any, stream):
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        return self._stream_gen(messages, temperature, max_tokens)
