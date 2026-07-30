"""Registry: policy enforcement + fallback chain + circuit breaker.

Uses injected fakes — no network, no keys.
"""

import time

import pytest

from rag_assistant.config import Settings
from rag_assistant.domain import DataClass
from rag_assistant.llm.registry import FAILURE_THRESHOLD, ProviderRegistry
from rag_assistant.ports import LLMMessage
from rag_assistant.testing.fakes import FakeLLM


class FailingLLM(FakeLLM):
    async def complete(self, messages, **kwargs):  # type: ignore[override]
        raise RuntimeError("provider down")


def _registry(profile: str = "default") -> ProviderRegistry:
    settings = Settings(rag_profile=profile, _env_file=None)
    cloud_mini = FakeLLM("openai-mini")
    cloud_mini.kind = "cloud"
    cloud_strong = FakeLLM("openai-strong")
    cloud_strong.kind = "cloud"
    local = FakeLLM("ollama")  # kind=local
    return ProviderRegistry(
        settings,
        providers={"openai-mini": cloud_mini, "openai-strong": cloud_strong, "ollama": local},
    )


MSG = [LLMMessage(role="user", content="hi")]


async def test_confidential_never_reaches_cloud():
    reg = _registry()
    chain = reg.chain_for("mini", DataClass.CONFIDENTIAL)
    assert [p.name for p in chain] == ["ollama"]
    result, provider = await reg.complete("mini", DataClass.CONFIDENTIAL, MSG)
    assert provider.name == "ollama"


async def test_internal_prefers_cloud():
    reg = _registry()
    _, provider = await reg.complete("mini", DataClass.INTERNAL, MSG)
    assert provider.name == "openai-mini"


async def test_offline_profile_forces_local():
    reg = _registry(profile="offline")
    _, provider = await reg.complete("strong", DataClass.PUBLIC, MSG)
    assert provider.name == "ollama"


async def test_fallback_chain_on_provider_failure():
    reg = _registry()
    failing = FailingLLM("openai-mini")
    failing.kind = "cloud"
    reg.providers["openai-mini"] = failing
    _, provider = await reg.complete("mini", DataClass.INTERNAL, MSG)
    assert provider.name == "ollama"  # no second cloud provider → falls through to local


async def test_confidential_local_failure_raises_never_degrades():
    """Fail-closed under failure: a broken local path must ERROR, not go cloud."""
    reg = _registry()
    broken_local = FailingLLM("ollama")  # kind=local
    reg.providers["ollama"] = broken_local
    with pytest.raises(RuntimeError):
        await reg.complete("mini", DataClass.CONFIDENTIAL, MSG)


async def test_confidential_recovers_after_circuit_opens():
    """The confidential chain has ONE provider. Two failures open its circuit —
    but once the provider works again, complete() must probe it instead of
    failing cold for the whole cooldown (single-provider blackout bug)."""
    reg = _registry()
    reg.providers["ollama"] = FailingLLM("ollama")  # kind=local
    for _ in range(FAILURE_THRESHOLD):
        with pytest.raises(RuntimeError):
            await reg.complete("mini", DataClass.CONFIDENTIAL, MSG)
    assert not reg._available("ollama")  # circuit is open

    # provider recovers; the very next call must still try it (not fail cold)
    healthy = FakeLLM("ollama")  # kind=local
    reg.providers["ollama"] = healthy
    result, provider = await reg.complete("mini", DataClass.CONFIDENTIAL, MSG)
    assert provider.name == "ollama"
    assert reg._available("ollama")  # success closed the circuit again


async def test_success_closes_open_circuit():
    reg = _registry()
    reg._open_until["openai-mini"] = time.monotonic() + 999
    reg._mark_success("openai-mini")
    assert reg._available("openai-mini")


def test_ollama_read_timeout_is_settings_driven():
    """A cold qwen3 on CPU must load into RAM before the first byte; the read
    timeout is configurable via .env so the confidential path does not time out."""
    reg = ProviderRegistry(Settings(_env_file=None, ollama_timeout_s=333.0))
    assert reg.providers["ollama"].timeout_s == 333.0


def test_ollama_read_timeout_defaults_generous():
    reg = ProviderRegistry(Settings(_env_file=None))
    assert reg.providers["ollama"].timeout_s == 600.0


def test_ollama_keep_alive_passthrough_and_default():
    """keep_alive keeps qwen3 resident across the long CPU eval; default empty
    leaves Ollama's own ~5min behavior untouched (no production change)."""
    assert ProviderRegistry(Settings(_env_file=None)).providers["ollama"].keep_alive == ""
    reg = ProviderRegistry(Settings(_env_file=None, ollama_keep_alive="30m"))
    assert reg.providers["ollama"].keep_alive == "30m"


async def test_circuit_breaker_opens_after_repeated_failures():
    reg = _registry()
    failing = FailingLLM("openai-mini")
    failing.kind = "cloud"
    reg.providers["openai-mini"] = failing
    await reg.complete("mini", DataClass.INTERNAL, MSG)  # fail #1 → fallback
    await reg.complete("mini", DataClass.INTERNAL, MSG)  # fail #2 → circuit opens
    assert not reg._available("openai-mini")
    # subsequent picks skip the open circuit entirely (streaming path)
    assert reg.pick("mini", DataClass.INTERNAL).name == "ollama"
