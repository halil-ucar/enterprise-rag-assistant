"""Unit tests for the production readiness gate (A1).

Every rule is tested individually (positive + negative), plus the demo-default
combination. All inputs are explicit — Settings fields, registry, providers and
env are constructed in the test, so the function stays deterministic regardless
of ambient environment variables.
"""

from __future__ import annotations

import pytest

import rag_assistant.readiness as readiness
from conftest import HARDENED_ENV, make_registry, make_settings
from rag_assistant.config import Settings
from rag_assistant.llm.registry import ProviderRegistry
from rag_assistant.readiness import (
    validate_config_values,
    validate_production_readiness,
)
from rag_assistant.testing.fakes import FakeLLM


def rules_for(settings: Settings, *, providers: ProviderRegistry | None = None, env=None) -> list:
    findings = validate_production_readiness(
        settings,
        make_registry(),
        providers if providers is not None else ProviderRegistry(settings),
        HARDENED_ENV if env is None else env,
    )
    return [f.rule for f in findings]


# ── baseline ───────────────────────────────────────────────────────────────────
def test_hardened_static_key_config_leaves_only_r3():
    """With A6, R3 is satisfiable — but a hardened config still on static-key
    auth keeps exactly R3 open (documented adjustment: this test expected
    R3 as 'open by design' before A6)."""
    assert rules_for(make_settings()) == ["R3"]


def test_hardened_config_yields_an_empty_finding_list():
    """THE proof of this phase: a fully hardened config (non-default keys,
    auth_backend=oidc + issuer, real backends, HF telemetry opt-out,
    fail-closed audit, rate limiting, EMPTY demo map) ⇒ zero findings.
    'production is startable' is a green test, not a promise — as a
    software layer; operator duties (Block C) remain."""
    hardened = make_settings(
        auth_backend="oidc",
        oidc_issuer_url="https://idp.example.com/realms/nordfels",
    )
    assert rules_for(hardened) == []


# ── R1: demo credentials ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key_anna": ""},
        {"api_key_ben": ""},
        {"api_key_anna": "demo-anna-it"},
        {"api_key_ben": "demo-ben-hr"},
        {"api_key_anna": "same-key", "api_key_ben": "same-key"},
    ],
)
def test_r1_flags_demo_empty_or_identical_keys(overrides):
    assert "R1" in rules_for(make_settings(**overrides))


def test_r1_passes_with_distinct_real_keys():
    assert "R1" not in rules_for(make_settings())


# ── R2: provider kinds + free-tier denylist ───────────────────────────────────
def test_r2_passes_for_the_default_provider_set():
    assert "R2" not in rules_for(make_settings())


def test_r2_flags_unknown_provider_kind():
    s = make_settings()
    weird = FakeLLM("weird")
    weird.kind = "free"
    providers = ProviderRegistry(s, providers={"weird": weird})
    findings = validate_production_readiness(s, make_registry(), providers, HARDENED_ENV)
    assert any(f.rule == "R2" and "unknown kind" in f.message for f in findings)


def test_r2_flags_denylisted_free_tier_provider():
    s = make_settings()
    providers = ProviderRegistry(s, providers={"gemini": FakeLLM("gemini")})
    findings = validate_production_readiness(s, make_registry(), providers, HARDENED_ENV)
    assert any(f.rule == "R2" and "free-tier" in f.message for f in findings)


# ── R3: auth backend ──────────────────────────────────────────────────────────
def test_r3_flags_static_key_auth():
    assert "R3" in rules_for(make_settings())


def test_r3_is_satisfied_by_oidc():
    assert "R3" not in rules_for(make_settings(auth_backend="oidc"))


# ── R4: fake inference backends ───────────────────────────────────────────────
def test_r4_flags_fake_backends():
    assert "R4" in rules_for(make_settings(embeddings_backend="fake"))
    assert "R4" in rules_for(make_settings(reranker_backend="fake"))


def test_r4_allows_reranker_off():
    """'off' is a documented latency lever, not a fake-data problem."""
    assert "R4" not in rules_for(make_settings(reranker_backend="off"))


# ── R5: HF telemetry ──────────────────────────────────────────────────────────
def test_r5_flags_missing_telemetry_optout():
    assert "R5" in rules_for(make_settings(), env={})
    assert "R5" in rules_for(make_settings(), env={"HF_HUB_DISABLE_TELEMETRY": "0"})


def test_r5_passes_when_telemetry_disabled():
    assert "R5" not in rules_for(make_settings())


# ── R7: audit trail (A4 — satisfiable since Phase 2) ──────────────────────────
def test_r7_passes_when_audit_is_enabled_and_fail_closed():
    assert "R7" not in rules_for(make_settings())


def test_r7_flags_disabled_audit():
    findings = validate_production_readiness(
        make_settings(audit_enabled=False),
        make_registry(),
        ProviderRegistry(make_settings(audit_enabled=False)),
        HARDENED_ENV,
    )
    assert any(f.rule == "R7" and "disabled" in f.message for f in findings)


def test_r7_flags_missing_fail_closed():
    """The E3 demand 'production ⇒ fail-closed audit for confidential' is
    enforced by the GATE over a plain setting — no runtime mode branch (E2)."""
    findings = validate_production_readiness(
        make_settings(audit_fail_closed=False),
        make_registry(),
        ProviderRegistry(make_settings(audit_fail_closed=False)),
        HARDENED_ENV,
    )
    assert any(f.rule == "R7" and "fail-closed" in f.message for f in findings)


def test_r7_reports_the_pre_a4_state_if_the_build_truth_is_off(monkeypatch):
    monkeypatch.setattr(readiness, "_AUDIT_AVAILABLE", False)
    findings = validate_production_readiness(
        make_settings(), make_registry(), ProviderRegistry(make_settings()), HARDENED_ENV
    )
    assert any(f.rule == "R7" and "not built yet" in f.message for f in findings)


# ── R10: OIDC demo department bridge (A6 — demo states stay out) ──────────────
def test_r10_flags_a_configured_demo_map():
    findings = validate_production_readiness(
        make_settings(oidc_demo_department_map='{"anna@nordfels.example": "it"}'),
        make_registry(),
        ProviderRegistry(make_settings()),
        HARDENED_ENV,
    )
    r10 = [f for f in findings if f.rule == "R10"]
    assert r10 and "department claim" in r10[0].message


def test_r10_passes_with_an_empty_map():
    assert "R10" not in rules_for(make_settings())


# ── R8: rate limiting (acceptance #3: production without limits refuses) ──────
def test_r8_flags_disabled_rate_limiting():
    assert "R8" in rules_for(make_settings(rate_limit_enabled=False))


def test_r8_passes_when_enabled():
    assert "R8" not in rules_for(make_settings(rate_limit_enabled=True))


# ── R9: regression guard above the policy code floor ──────────────────────────
def test_r9_passes_with_the_intact_data_class_matrix():
    assert "R9" not in rules_for(make_settings())


def test_r9_fires_if_the_matrix_is_ever_weakened(monkeypatch):
    monkeypatch.setattr(readiness, "allowed_provider_kinds", lambda dc, profile: ("cloud", "local"))
    findings = validate_production_readiness(
        make_settings(), make_registry(), ProviderRegistry(make_settings()), HARDENED_ENV
    )
    r9 = [f for f in findings if f.rule == "R9"]
    assert r9 and "'hr'" in r9[0].message


# ── demo-default combination ──────────────────────────────────────────────────
def test_demo_default_config_in_production_yields_the_full_finding_list():
    """Expected findings for the demo state under production (A6 adds the
    demo bridge map to the demo state — and R10 to the list):

    | rule | violated because                                        |
    |------|---------------------------------------------------------|
    | R1   | both API keys still on their demo defaults              |
    | R10  | OIDC demo department bridge configured (demo comfort)   |
    | R3   | auth_backend static-key (production requires oidc)      |
    | R5   | HF telemetry opt-out not set (bare env)                 |
    | R7   | audit writes not fail-closed (shipped default is false) |
    """
    demo = make_settings(
        api_key_anna="demo-anna-it",
        api_key_ben="demo-ben-hr",
        audit_fail_closed=False,
        oidc_demo_department_map='{"anna@nordfels.example": "it", "ben@nordfels.example": "hr"}',
    )
    assert rules_for(demo, env={}) == ["R1", "R10", "R3", "R5", "R7"]


def test_findings_are_sorted_deterministically():
    demo = make_settings(
        api_key_anna="demo-anna-it",
        api_key_ben="demo-ben-hr",
        embeddings_backend="fake",
        reranker_backend="fake",
        rate_limit_enabled=False,
        audit_fail_closed=False,
    )
    findings = validate_production_readiness(demo, make_registry(), ProviderRegistry(demo), env={})
    assert [f.rule for f in findings] == sorted(f.rule for f in findings)
    assert [f.rule for f in findings] == ["R1", "R3", "R4", "R4", "R5", "R7", "R8"]


# ── value validation (every mode, fail-closed) ────────────────────────────────
def test_valid_mode_values_pass():
    validate_config_values(make_settings(deployment_mode="demo"))
    validate_config_values(make_settings(deployment_mode="production"))


def test_unknown_deployment_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown deployment_mode 'prod'"):
        validate_config_values(make_settings(deployment_mode="prod"))


def test_unknown_auth_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown auth_backend 'sso'"):
        validate_config_values(make_settings(auth_backend="sso", deployment_mode="demo"))


def test_oidc_without_issuer_is_rejected_in_every_mode():
    # Replaces the pre-A6 'not implemented yet' refusal: oidc is buildable,
    # but an empty issuer must fail at startup, not on the first login.
    for mode in ("demo", "production"):
        with pytest.raises(ValueError, match="requires OIDC_ISSUER_URL"):
            validate_config_values(make_settings(auth_backend="oidc", deployment_mode=mode))


def test_oidc_with_issuer_passes_value_validation():
    validate_config_values(
        make_settings(
            auth_backend="oidc",
            oidc_issuer_url="http://localhost:5556/dex",
            deployment_mode="demo",
        )
    )


def test_invalid_demo_map_is_rejected_at_startup():
    with pytest.raises(ValueError, match="OIDC_DEMO_DEPARTMENT_MAP"):
        validate_config_values(
            make_settings(oidc_demo_department_map="{oops", deployment_mode="demo")
        )


def test_invalid_openai_extra_body_is_rejected_at_startup():
    with pytest.raises(ValueError, match="OPENAI_EXTRA_BODY"):
        validate_config_values(make_settings(openai_extra_body="{oops", deployment_mode="demo"))
