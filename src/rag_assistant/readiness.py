"""Production readiness gate (A1) — pure, deterministic, fully unit-testable.

Two layers, both fail-closed:

1. `validate_config_values` runs in EVERY mode (first action in the lifespan,
   before any connection): an unknown value like DEPLOYMENT_MODE=prod must
   never silently run as demo.
2. `validate_production_readiness` runs only for deployment_mode=production
   and returns the full list of findings; the lifespan logs each one and
   refuses to start on a non-empty list.

Rule numbering follows the Block-A plan. R6 is intentionally absent: the
judge-cloud-override flag it would have checked was deliberately never built
in Phase 0 — the shipped behavior is STRICTER than planned, so there is
nothing to verify. The number is not reused.

With A6, every rule is satisfiable: a fully hardened configuration yields an
EMPTY finding list — `production` is startable as a software layer for the
first time (operator duties, Block C, remain). R3
(OIDC) is satisfied by auth_backend=oidc + a configured issuer; R10 (new
with A6) rejects the OIDC demo department bridge in production — demo states
stay out, same logic as R1. The tests prove (a) the refusal with the
complete finding list, (b) per-rule satisfiability, and (c) the
hardened-config ⇒ empty-list statement itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .auth import parse_demo_department_map
from .config import Registry, Settings
from .domain import DataClass
from .llm.openai_provider import parse_extra_body
from .llm.registry import ProviderRegistry
from .policy import allowed_provider_kinds

VALID_DEPLOYMENT_MODES = ("demo", "production")
VALID_AUTH_BACKENDS = ("static-key", "oidc")

# Documents the E1 invariant in code: free tiers pay with training rights and
# are banned for EVERY data class. Gemini was removed entirely in Phase 0;
# this denylist keeps the ban visible and machine-checked (rule R2).
FREE_TIER_DENYLIST: frozenset[str] = frozenset({"gemini"})

_VALID_PROVIDER_KINDS = ("cloud", "sovereign", "local")

# Demo credential defaults (rule R1 rejects them in production).
_DEMO_API_KEYS = ("demo-anna-it", "demo-ben-hr")

# Build truth, not a config flag: False would have created an "audit is on"
# appearance without any function behind it. A4 flipped this constant and
# wired the real checks (rule R7) — it stays so the pre-A4 state remains
# testable and the mechanism documented.
_AUDIT_AVAILABLE = True


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    remedy: str


def validate_config_values(settings: Settings) -> None:
    """Fail-closed value validation, active in every mode. Raises ValueError."""
    if settings.deployment_mode not in VALID_DEPLOYMENT_MODES:
        raise ValueError(
            f"unknown deployment_mode '{settings.deployment_mode}' "
            f"(valid: {', '.join(VALID_DEPLOYMENT_MODES)})"
        )
    if settings.auth_backend not in VALID_AUTH_BACKENDS:
        raise ValueError(
            f"unknown auth_backend '{settings.auth_backend}' "
            f"(valid: {', '.join(VALID_AUTH_BACKENDS)})"
        )
    # A6: the oidc backend needs its issuer — an empty issuer must fail HERE
    # (every mode), not as an opaque discovery error on the first login.
    if settings.auth_backend == "oidc" and not settings.oidc_issuer_url:
        raise ValueError("auth_backend 'oidc' requires OIDC_ISSUER_URL to be set")
    # A6: the demo department bridge map must be valid JSON (or empty) —
    # fail-closed in every mode, same posture as OPENAI_EXTRA_BODY below.
    parse_demo_department_map(settings.oidc_demo_department_map)
    # A2: endpoint request extras must be a valid JSON object (or empty) —
    # a typo must abort startup, not silently drop the think-control field.
    parse_extra_body(settings.openai_extra_body)


def validate_production_readiness(
    settings: Settings,
    registry: Registry,
    providers: ProviderRegistry,
    env: Mapping[str, str],
) -> list[Finding]:
    """All readiness rules over the fully-built (but unconnected) configuration.

    Pure function: `env` is a parameter (os.environ at the call site) and
    `providers` is the registry built deterministically from the settings.
    Returns findings sorted by (rule, message) — deterministic order.
    """
    findings: list[Finding] = []

    # R1 — demo credentials must be replaced.
    r1_violations: list[str] = []
    if not settings.api_key_anna or not settings.api_key_ben:
        r1_violations.append("an API key is empty")
    if settings.api_key_anna in _DEMO_API_KEYS or settings.api_key_ben in _DEMO_API_KEYS:
        r1_violations.append("an API key still has its demo default")
    if settings.api_key_anna == settings.api_key_ben:
        r1_violations.append("both API keys are identical")
    if r1_violations:
        findings.append(
            Finding(
                rule="R1",
                message="demo API keys in production: " + "; ".join(r1_violations),
                remedy="set API_KEY_ANNA / API_KEY_BEN to distinct non-default secrets",
            )
        )

    # R2 — every provider reachable via a chain has a known kind and is not a
    # banned free tier (scan covers chains AND the provider map).
    names = set(providers.providers)
    for chain in providers.chains.values():
        names.update(chain)
    for name in sorted(names):
        provider = providers.providers.get(name)
        if provider is None:
            findings.append(
                Finding(
                    rule="R2",
                    message=f"provider '{name}' appears in a chain but is not registered",
                    remedy="fix the provider registry wiring",
                )
            )
            continue
        if provider.kind not in _VALID_PROVIDER_KINDS:
            findings.append(
                Finding(
                    rule="R2",
                    message=f"provider '{name}' has unknown kind '{provider.kind}'",
                    remedy=f"use one of: {', '.join(_VALID_PROVIDER_KINDS)}",
                )
            )
        if name in FREE_TIER_DENYLIST:
            findings.append(
                Finding(
                    rule="R2",
                    message=f"provider '{name}' is a banned free-tier provider (E1)",
                    remedy="remove the provider — free tiers pay with training rights",
                )
            )

    # R3 — production requires OIDC (satisfiable since A6).
    if settings.auth_backend != "oidc":
        findings.append(
            Finding(
                rule="R3",
                message=f"auth_backend is '{settings.auth_backend}' — production requires 'oidc'",
                remedy="set AUTH_BACKEND=oidc and configure OIDC_ISSUER_URL for your IdP",
            )
        )

    # R4 — no fake inference backends. reranker 'off' is a documented latency
    # lever, not a fake-data problem, and stays allowed.
    if settings.embeddings_backend == "fake":
        findings.append(
            Finding(
                rule="R4",
                message="embeddings_backend is 'fake'",
                remedy="set EMBEDDINGS_BACKEND=local",
            )
        )
    if settings.reranker_backend == "fake":
        findings.append(
            Finding(
                rule="R4",
                message="reranker_backend is 'fake'",
                remedy="set RERANKER_BACKEND=local (or 'off', which is allowed)",
            )
        )

    # R5 — HF telemetry must be disabled (containers already do; a native
    # production start must too).
    if env.get("HF_HUB_DISABLE_TELEMETRY") != "1":
        findings.append(
            Finding(
                rule="R5",
                message="HF_HUB_DISABLE_TELEMETRY is not '1'",
                remedy="export HF_HUB_DISABLE_TELEMETRY=1 in the environment",
            )
        )

    # R7 — audit trail (A4): built. Production requires audit ENABLED and
    # fail-closed writes for confidential access — the E3 demand
    # "production ⇒ fail-closed" is enforced by THIS gate over plain settings
    # (same construction as R8/rate_limit_enabled), never by a runtime
    # deployment_mode branch (E2).
    if not _AUDIT_AVAILABLE:
        findings.append(
            Finding(
                rule="R7",
                message="audit trail is not built yet (A4)",
                remedy="ships with A4 — production stays blocked until then",
            )
        )
    else:
        if not settings.audit_enabled:
            findings.append(
                Finding(
                    rule="R7",
                    message="audit trail is disabled",
                    remedy="set AUDIT_ENABLED=true",
                )
            )
        if not settings.audit_fail_closed:
            findings.append(
                Finding(
                    rule="R7",
                    message="audit writes are not fail-closed for confidential access",
                    remedy="set AUDIT_FAIL_CLOSED=true",
                )
            )

    # R8 — rate limiting must be on in production.
    if not settings.rate_limit_enabled:
        findings.append(
            Finding(
                rule="R8",
                message="rate limiting is disabled",
                remedy="set RATE_LIMIT_ENABLED=true",
            )
        )

    # R9 — regression guard ABOVE the policy code floor (P0.3): no confidential
    # collection may ever see the cloud kind, even if policy.py were weakened.
    for name in sorted(registry.collections):
        cfg = registry.collections[name]
        if cfg.data_class != DataClass.CONFIDENTIAL:
            continue
        if "cloud" in allowed_provider_kinds(DataClass.CONFIDENTIAL, settings.rag_profile):
            findings.append(
                Finding(
                    rule="R9",
                    message=(
                        f"confidential collection '{name}' would be allowed to use "
                        "cloud providers — the data-class matrix has been weakened"
                    ),
                    remedy="restore the confidential floor in policy.allowed_provider_kinds",
                )
            )

    # R10 — the OIDC demo department bridge (email→department map) is
    # showcase comfort for dex static users and must never become a
    # production authorization mechanism: departments come from the
    # operator IdP's claim. Demo states stay out of production — the same
    # logic as R1/demo keys. Numbered R10 because R6 stays documented-absent.
    if settings.oidc_demo_department_map.strip():
        findings.append(
            Finding(
                rule="R10",
                message="OIDC demo department map is set — production requires the department claim",
                remedy="unset OIDC_DEMO_DEPARTMENT_MAP; configure the claim in the operator IdP",
            )
        )

    return sorted(findings, key=lambda f: (f.rule, f.message))
