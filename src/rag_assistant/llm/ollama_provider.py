"""Ollama provider — the LOCAL path: confidential collections + offline demo.

Talks to a natively running Ollama (containers on macOS have no Metal;
compose maps OLLAMA_URL to host.docker.internal).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

import httpx

from ..ports import LLMMessage, LLMResult


class OllamaProvider:
    kind = "local"

    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        timeout_s: float = 600.0,
        keep_alive: str = "",
    ):
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.keep_alive = keep_alive

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            # reasoning models (qwen3, deepseek-r1, …) otherwise emit a long
            # chain-of-thought first; for grounded RAG the answer is in the
            # retrieved context, so skip it — far fewer tokens, much faster on CPU.
            "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        return LLMResult(
            text=data.get("message", {}).get("content", ""),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
        )

    async def _stream_gen(
        self, messages: Sequence[LLMMessage], temperature: float, max_tokens: int
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "think": False,  # skip chain-of-thought (see complete()) — faster on CPU
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        # No overall read timeout (generation on CPU can take minutes), but a
        # finite CONNECT timeout so a black-holed host fails fast instead of
        # hanging the request forever.
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp,
        ):
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    # Ollama streams errors as a JSON line, not an HTTP status —
                    # surface it so the registry counts a failure instead of
                    # silently yielding an empty answer.
                    raise httpx.HTTPError(f"ollama error: {data['error']}")
                delta = data.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if data.get("done"):
                    break

    def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        return self._stream_gen(messages, temperature, max_tokens)
