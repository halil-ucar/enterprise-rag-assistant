"""Provider registry: model tiering + data-class enforcement + fallback chain.

- Tiers: "mini" (default generator + pre-call), "strong" (agentic path), "local".
  In RAG the knowledge comes from retrieval, not model size — mini is the workhorse.
- Policy gate: assert_provider_allowed runs before EVERY call. Confidential
  requests never see a cloud provider — also not via fallback (fail-closed:
  if the local path is down, the request errors instead of degrading to cloud).
- Circuit-breaker-lite: N consecutive failures → skip the provider for M seconds.
  Three lines of state, not a library.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence

from ..config import Settings
from ..domain import DataClass
from ..obs import Metrics
from ..policy import allowed_provider_kinds, assert_provider_allowed
from ..ports import LLMMessage, LLMProvider, LLMResult
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider, parse_extra_body

log = logging.getLogger(__name__)

FAILURE_THRESHOLD = 2
COOLDOWN_S = 120.0


class ProviderRegistry:
    def __init__(
        self,
        settings: Settings,
        providers: dict[str, LLMProvider] | None = None,
        metrics: Metrics | None = None,
    ):
        self.settings = settings
        # Optional counters (A5): None = no metrics (existing tests untouched).
        self.metrics = metrics
        self.providers: dict[str, LLMProvider] = providers if providers is not None else {}
        if providers is None:
            self._build_default(settings)
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        # tier → ordered fallback chain of provider names. No free-tier provider
        # anywhere: free tiers pay with training rights, which the project bans
        # for every data class (E1). The fallback MECHANISM is exercised through
        # fake providers in tests, not through a live second cloud vendor.
        self.chains: dict[str, list[str]] = {
            "mini": [n for n in ("openai-mini", "ollama") if n in self.providers],
            "strong": [
                n for n in ("openai-strong", "openai-mini", "ollama") if n in self.providers
            ],
            "local": [n for n in ("ollama",) if n in self.providers],
        }

    def _build_default(self, s: Settings) -> None:
        if s.openai_api_key or s.azure_openai_endpoint:
            mini = s.azure_openai_deployment_mini or s.openai_model_mini
            strong = s.azure_openai_deployment_strong or s.openai_model_strong
            extra_body = parse_extra_body(s.openai_extra_body)
            self.providers["openai-mini"] = OpenAIProvider(
                "openai-mini", mini, s, extra_body=extra_body
            )
            self.providers["openai-strong"] = OpenAIProvider(
                "openai-strong", strong, s, extra_body=extra_body
            )
        self.providers["ollama"] = OllamaProvider(
            "ollama",
            s.ollama_model,
            s.ollama_url,
            timeout_s=s.ollama_timeout_s,
            keep_alive=s.ollama_keep_alive,
        )

    # ── circuit breaker ───────────────────────────────────────────────────────
    def _available(self, name: str) -> bool:
        return time.monotonic() >= self._open_until.get(name, 0.0)

    def _mark_failure(self, name: str) -> None:
        self._fails[name] = self._fails.get(name, 0) + 1
        if self._fails[name] >= FAILURE_THRESHOLD:
            self._open_until[name] = time.monotonic() + COOLDOWN_S
            self._fails[name] = 0
            log.warning("provider %s: circuit open for %.0fs", name, COOLDOWN_S)

    def _mark_success(self, name: str) -> None:
        self._fails[name] = 0
        self._open_until.pop(name, None)  # a working provider closes its circuit immediately

    # ── selection ─────────────────────────────────────────────────────────────
    def chain_for(self, tier: str, data_class: DataClass) -> list[LLMProvider]:
        """Fallback chain filtered by the data-class policy. May be empty only
        for misconfiguration — confidential always has the local provider."""
        allowed = allowed_provider_kinds(data_class, self.settings.rag_profile)
        return [
            self.providers[name]
            for name in self.chains.get(tier, [])
            if self.providers[name].kind in allowed
        ]

    def pick(self, tier: str, data_class: DataClass) -> LLMProvider:
        """First available provider of the policy-filtered chain (for streaming,
        where mid-stream fallback is not possible)."""
        chain = self.chain_for(tier, data_class)
        for provider in chain:
            if self._available(provider.name):
                assert_provider_allowed(provider.kind, data_class, self.settings.rag_profile)
                return provider
        if chain:  # all circuits open — try the first anyway rather than fail cold
            return chain[0]
        raise PermissionError(
            f"no provider satisfies tier={tier} data_class={data_class.value} "
            f"profile={self.settings.rag_profile}"
        )

    async def complete(
        self,
        tier: str,
        data_class: DataClass,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> tuple[LLMResult, LLMProvider]:
        """Complete with fallback along the policy-filtered chain."""
        chain = self.chain_for(tier, data_class)
        if not chain:
            raise PermissionError(
                f"no provider satisfies tier={tier} data_class={data_class.value}"
            )
        candidates = [p for p in chain if self._available(p.name)]
        if not candidates:
            # Every circuit is open — probe the chain anyway instead of failing
            # cold for the rest of the cooldown (mirrors pick()). This matters
            # most on the confidential chain, which has exactly ONE provider:
            # two Ollama errors would otherwise blackout the whole data class
            # for COOLDOWN_S even after Ollama recovered.
            candidates = list(chain)
        last_error: Exception | None = None
        for i, provider in enumerate(candidates):
            assert_provider_allowed(provider.kind, data_class, self.settings.rag_profile)
            try:
                result = await provider.complete(
                    messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
                )
                self._mark_success(provider.name)
                self._count(provider, "ok")
                return result, provider
            except Exception as exc:  # noqa: BLE001 — any provider error triggers fallback
                last_error = exc
                self._mark_failure(provider.name)
                # outcome semantics (A5): a failure with another chain link left
                # is a 'fallback'; the last link's failure is an 'error'.
                self._count(provider, "fallback" if i < len(candidates) - 1 else "error")
                log.warning("provider %s failed (%s), trying next in chain", provider.name, exc)
        raise RuntimeError(f"all providers failed for tier={tier}") from last_error

    def _count(self, provider: LLMProvider, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.provider_calls.labels(
                provider=provider.name, kind=provider.kind, outcome=outcome
            ).inc()

    def stream(
        self,
        tier: str,
        data_class: DataClass,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> tuple[AsyncIterator[str], LLMProvider]:
        provider = self.pick(tier, data_class)
        # Streaming counts the SELECTED provider as 'ok' (no mid-stream
        # fallback exists); a failing stream surfaces in the request path.
        self._count(provider, "ok")
        return (
            provider.stream(messages, temperature=temperature, max_tokens=max_tokens),
            provider,
        )
