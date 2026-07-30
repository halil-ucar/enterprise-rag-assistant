"""Anthropic OpenAI-compat quirk: the endpoint rejects response_format type
json_object (400: "Input should be 'json_schema'") — found by the first real
--judge smoke run, where all 17 cloud judge calls failed. The judge-cloud
provider must therefore omit response_format; its prompts enforce JSON via
few-shot examples and callers parse tolerantly (parse_json_loose).

No network: the SDK client is replaced with a recording stub.
"""

from types import SimpleNamespace

from rag_assistant.config import Settings
from rag_assistant.domain import DataClass
from rag_assistant.judge_select import select_judge_provider
from rag_assistant.llm.openai_provider import OpenAIProvider
from rag_assistant.ports import LLMMessage


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _recording_client(captured: dict) -> SimpleNamespace:
    async def create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content='{"statements": []}')
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_json_mode_omits_response_format_when_unsupported():
    provider = OpenAIProvider(
        "judge-cloud",
        "claude-sonnet-5",
        _settings(),
        base_url="https://api.anthropic.com/v1/",
        api_key="k",
        supports_json_object=False,
    )
    captured: dict = {}
    provider._client = _recording_client(captured)  # type: ignore[assignment]
    await provider.complete([LLMMessage(role="user", content="x")], json_mode=True)
    assert "response_format" not in captured


async def test_temperature_omitted_when_unsupported():
    """Newer Claude models 400 on any sampling parameter — the flag drops
    `temperature` from the request entirely (model samples at its default)."""
    provider = OpenAIProvider(
        "judge-cloud",
        "claude-sonnet-5",
        _settings(),
        base_url="https://api.anthropic.com/v1/",
        api_key="k",
        supports_temperature=False,
    )
    captured: dict = {}
    provider._client = _recording_client(captured)  # type: ignore[assignment]
    await provider.complete([LLMMessage(role="user", content="x")])
    assert "temperature" not in captured


async def test_temperature_sent_by_default():
    provider = OpenAIProvider("openai-mini", "m", _settings(openai_api_key="k"))
    captured: dict = {}
    provider._client = _recording_client(captured)  # type: ignore[assignment]
    await provider.complete([LLMMessage(role="user", content="x")], temperature=0.0)
    assert captured.get("temperature") == 0.0


async def test_json_mode_sends_response_format_by_default():
    provider = OpenAIProvider("openai-mini", "m", _settings(openai_api_key="k"))
    captured: dict = {}
    provider._client = _recording_client(captured)  # type: ignore[assignment]
    await provider.complete([LLMMessage(role="user", content="x")], json_mode=True)
    assert captured.get("response_format") == {"type": "json_object"}


def test_cloud_judge_is_built_without_json_object_format():
    provider = select_judge_provider(DataClass.INTERNAL, _settings(anthropic_api_key="k"))
    assert isinstance(provider, OpenAIProvider)
    assert provider.supports_json_object is False
    assert provider.supports_temperature is False
