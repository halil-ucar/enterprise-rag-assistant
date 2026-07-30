"""Policy-gated judge selection (closes the Phase-0 finding: the eval judge
used to be built directly on a cloud provider, bypassing the data-class gate,
so confidential contexts could leave the host during evaluation).

Rule (policy.judge_provider_kinds): the judge is bound by the SAME data-class
gate as generation — it reads the same answer + retrieved contexts.

Selection per data class, fail-closed:
  - public/internal → Anthropic cloud judge (OpenAI-compatible endpoint) when a
    key is configured, else the local judge. The cloud judge is a different
    model family than both generators (OpenAI cloud, Qwen local) — the
    judge≠generator rule holds structurally, not by luck.
  - confidential / offline profile → local judge only. Never a cloud kind, not
    even when a cloud key is present.
  - No permitted judge configured → None. The caller records the case as
    skipped — it must never fall back to a disallowed provider.
"""

from __future__ import annotations

from .config import Settings
from .domain import DataClass
from .llm.ollama_provider import OllamaProvider
from .llm.openai_provider import OpenAIProvider
from .policy import CLOUD, LOCAL, assert_provider_allowed, judge_provider_kinds
from .ports import LLMProvider


def select_judge_provider(data_class: DataClass, settings: Settings) -> LLMProvider | None:
    """Return the judge provider permitted for this data class, or None."""
    allowed = judge_provider_kinds(data_class, settings.rag_profile)
    provider: LLMProvider | None = None
    if CLOUD in allowed and settings.anthropic_api_key:
        provider = OpenAIProvider(
            "judge-cloud",
            settings.judge_cloud_model,
            settings,
            base_url=settings.judge_cloud_base_url,
            api_key=settings.anthropic_api_key,
            # The Anthropic compat endpoint 400s on response_format json_object
            # (proven by the first --judge smoke run: 17/17 cloud calls failed)
            # AND on temperature (deprecated on newer Claude models — proven by
            # the owner's follow-up probe). Both omitted; JSON comes from the
            # prompt, sampling runs at the model default.
            supports_json_object=False,
            supports_temperature=False,
        )
    elif LOCAL in allowed:
        provider = OllamaProvider(
            "judge-local",
            settings.ollama_judge_model,
            settings.ollama_url,
            timeout_s=settings.ollama_timeout_s,
            keep_alive=settings.ollama_keep_alive,
        )
    if provider is not None:
        # Belt-and-suspenders: the constructive selection above already respects
        # the matrix; this assert makes a future refactor fail loudly, not leak.
        assert_provider_allowed(provider.kind, data_class, settings.rag_profile)
    return provider
