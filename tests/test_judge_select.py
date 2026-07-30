"""Judge-Policy-Gate: the eval judge obeys the SAME data-class matrix as
generation (Phase-0 fix — the judge used to bypass the policy entirely).

No network, no keys: only constructor wiring and kinds are asserted.
"""

from rag_assistant.config import Settings
from rag_assistant.domain import DataClass
from rag_assistant.judge_select import select_judge_provider
from rag_assistant.policy import judge_provider_kinds


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_confidential_is_judged_locally_even_with_cloud_key():
    """The core of the gate: a configured cloud key must never widen what
    evaluation may see — confidential stays with the local judge."""
    provider = select_judge_provider(DataClass.CONFIDENTIAL, _settings(anthropic_api_key="k"))
    assert provider is not None
    assert provider.kind == "local"
    assert provider.name == "judge-local"


def test_public_and_internal_use_cloud_judge_when_key_present():
    settings = _settings(anthropic_api_key="k")
    for dc in (DataClass.PUBLIC, DataClass.INTERNAL):
        provider = select_judge_provider(dc, settings)
        assert provider is not None
        assert provider.kind == "cloud"
        assert provider.model == settings.judge_cloud_model


def test_without_cloud_key_everything_is_judged_locally():
    settings = _settings()
    for dc in DataClass:
        provider = select_judge_provider(dc, settings)
        assert provider is not None
        assert provider.kind == "local"
        assert provider.model == settings.ollama_judge_model


def test_offline_profile_forces_local_judge_for_all_classes():
    settings = _settings(rag_profile="offline", anthropic_api_key="k")
    for dc in DataClass:
        provider = select_judge_provider(dc, settings)
        assert provider is not None
        assert provider.kind == "local"


def test_selected_kind_is_always_within_the_policy_matrix():
    """Property over all combinations: the factory can never hand out a kind
    the policy matrix forbids for that data class + profile."""
    for profile in ("default", "offline"):
        for key in ("", "k"):
            settings = _settings(rag_profile=profile, anthropic_api_key=key)
            for dc in DataClass:
                provider = select_judge_provider(dc, settings)
                assert provider is not None
                assert provider.kind in judge_provider_kinds(dc, profile)
