"""Unit tests for OIDC token validation (A6, cut 2) — no network, no IdP.

Self-signed RSA test keys (cryptography via pyjwt[crypto]); the PyJWKClient
fetch is stubbed with a local JWKS document. The nine cases from the plan:
valid · expired · wrong audience · wrong issuer · tampered signature ·
unknown kid · missing department → 403 · bridge only with a configured map ·
map JSON error → startup error.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from fastapi import HTTPException

from conftest import make_settings
from rag_assistant.auth import OidcAuth, parse_demo_department_map

try:
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:  # pragma: no cover — pyjwt[crypto] guarantees availability
    raise

ISSUER = "http://localhost:5556/dex"
KID = "test-key"

# Module-level keys: generating RSA keys per test would dominate runtime.
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks_for(key, kid: str = KID) -> dict:
    pub = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    pub.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [pub]}


class StubJWKClient(jwt.PyJWKClient):
    """PyJWKClient with the HTTP fetch replaced by a local JWKS document."""

    def __init__(self, jwks: dict):
        super().__init__("http://jwks.invalid/keys", cache_keys=False)
        self._stub = jwks

    def fetch_data(self) -> dict:
        return self._stub


def make_token(
    *,
    key=KEY,
    kid: str = KID,
    issuer: str = ISSUER,
    audience: str = "rag-assistant",
    exp_delta: int = 3600,
    claims: dict | None = None,
) -> str:
    now = int(time.time())
    payload: dict = {
        "iss": issuer,
        "aud": audience,
        "sub": "sub-anna-7f3",
        "iat": now,
        "exp": now + exp_delta,
    }
    payload.update(claims or {})
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def make_auth(jwks: dict | None = None, **overrides) -> OidcAuth:
    settings = make_settings(
        auth_backend="oidc",
        oidc_issuer_url=ISSUER,
        **overrides,
    )
    return OidcAuth(settings, jwk_client=StubJWKClient(jwks if jwks is not None else jwks_for(KEY)))


# ── signature / claims validation ─────────────────────────────────────────────
async def test_valid_token_yields_claims_and_scope_from_sub():
    auth = make_auth()
    claims = await auth.validate_token(make_token(claims={"department": "it"}))
    assert claims is not None
    scope = auth.scope_from_claims(claims, "nordfels")
    # Data minimization: user_id is the pseudonymous sub, nothing else carried.
    assert (scope.tenant, scope.user_id, scope.department) == ("nordfels", "sub-anna-7f3", "it")


async def test_expired_token_is_rejected():
    # 1 hour past exp — far beyond the 30 s leeway.
    assert await make_auth().validate_token(make_token(exp_delta=-3600)) is None


async def test_wrong_audience_is_rejected():
    assert await make_auth().validate_token(make_token(audience="other-client")) is None


async def test_wrong_issuer_is_rejected():
    assert await make_auth().validate_token(make_token(issuer="http://evil.example/dex")) is None


async def test_tampered_signature_is_rejected():
    # Signed by a different key under the SAME kid — signature check must fail.
    assert await make_auth().validate_token(make_token(key=ATTACKER_KEY)) is None


async def test_unknown_kid_is_rejected():
    assert await make_auth().validate_token(make_token(kid="unknown-kid")) is None


# ── claims mapping (fail-closed) ──────────────────────────────────────────────
async def test_missing_department_claim_is_403_fail_closed():
    auth = make_auth()
    claims = await auth.validate_token(make_token())
    assert claims is not None
    with pytest.raises(HTTPException) as exc:
        auth.scope_from_claims(claims, "nordfels")
    assert exc.value.status_code == 403  # no default department, ever


async def test_demo_bridge_applies_only_with_a_configured_map():
    token = make_token(claims={"email": "anna@nordfels.example"})
    # Without a map: fail-closed 403 despite the email claim.
    auth = make_auth()
    claims = await auth.validate_token(token)
    assert claims is not None
    with pytest.raises(HTTPException):
        auth.scope_from_claims(claims, "nordfels")
    # With the map: department resolved via email lookup (email never stored).
    bridged = make_auth(oidc_demo_department_map='{"anna@nordfels.example": "it"}')
    scope = bridged.scope_from_claims(claims, "nordfels")
    assert (scope.user_id, scope.department) == ("sub-anna-7f3", "it")
    # An email outside the map stays fail-closed.
    with pytest.raises(HTTPException):
        bridged.scope_from_claims({**claims, "email": "mallory@nordfels.example"}, "nordfels")


async def test_department_claim_wins_over_the_bridge():
    auth = make_auth(oidc_demo_department_map='{"anna@nordfels.example": "hr"}')
    claims = await auth.validate_token(
        make_token(claims={"department": "it", "email": "anna@nordfels.example"})
    )
    assert claims is not None
    assert auth.scope_from_claims(claims, "nordfels").department == "it"


def test_map_json_error_is_a_startup_error():
    with pytest.raises(ValueError, match="OIDC_DEMO_DEPARTMENT_MAP"):
        make_auth(oidc_demo_department_map="{oops")


@pytest.mark.parametrize("raw", ['["a"]', '{"a": 1}', '{"a": {"b": "c"}}'])
def test_map_must_be_a_flat_string_object(raw):
    with pytest.raises(ValueError, match="JSON object"):
        parse_demo_department_map(raw)


def test_empty_map_is_no_bridge():
    assert parse_demo_department_map("") == {}
    assert parse_demo_department_map("  ") == {}


# ── backchannel rewrite (container↔browser issuer consistency) ────────────────
def test_backchannel_rewrite_touches_only_the_fetch_base():
    auth = make_auth(oidc_internal_issuer_url="http://dex:5556/dex")
    assert (
        auth._rewrite_backchannel("http://localhost:5556/dex/token") == "http://dex:5556/dex/token"
    )
    # Foreign URLs pass through untouched; iss validation stays on the PUBLIC issuer.
    assert auth._rewrite_backchannel("http://other.example/x") == "http://other.example/x"
    assert auth.issuer == ISSUER


def test_without_internal_url_nothing_is_rewritten():
    auth = make_auth()
    assert auth._rewrite_backchannel("http://localhost:5556/dex/token") == (
        "http://localhost:5556/dex/token"
    )
