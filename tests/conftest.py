"""Shared test helpers (A1/A3): explicit Settings + a minimal registry.

Everything rule-relevant is passed explicitly so tests stay deterministic
regardless of ambient environment variables or a local .env file.
"""

from __future__ import annotations

from rag_assistant.config import Registry, Settings
from rag_assistant.domain import CollectionCfg, DataClass

HARDENED_ENV = {"HF_HUB_DISABLE_TELEMETRY": "1"}


def make_settings(**overrides) -> Settings:
    values: dict = {
        "deployment_mode": "production",
        "auth_backend": "static-key",
        "api_key_anna": "prod-key-anna-7f3",
        "api_key_ben": "prod-key-ben-9c1",
        "embeddings_backend": "local",
        "reranker_backend": "local",
        "rate_limit_enabled": True,
        # R7 (A4): the hardened baseline enables the audit trail fail-closed;
        # individual tests override to prove each finding.
        "audit_enabled": True,
        "audit_fail_closed": True,
        "rag_profile": "default",
        "openai_api_key": "",
        "azure_openai_endpoint": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_registry() -> Registry:
    return Registry(
        default_tenant="nordfels",
        default_collection="handbuecher",
        collections={
            "handbuecher": CollectionCfg(
                name="handbuecher", tenant="nordfels", data_class=DataClass.INTERNAL
            ),
            "hr": CollectionCfg(name="hr", tenant="nordfels", data_class=DataClass.CONFIDENTIAL),
        },
    )
