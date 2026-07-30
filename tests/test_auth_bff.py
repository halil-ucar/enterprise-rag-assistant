"""Endpoint-level unit tests for the BFF flow, auth sessions and CSRF (A6, cut 3).

No services: app state is assembled by hand (fake Redis, stubbed JWKS fetch,
stubbed token exchange) and requests go through httpx's ASGI transport
without the lifespan. Under test:

- login: state + PKCE S256, single-use flow keys with TTL,
- callback: BFF core proof (no token in ANY browser response; HttpOnly +
  SameSite=Lax cookie), state replay → neutral 400,
- session bootstrap, logout (deletes exactly the own session, CSRF-required),
- CSRF on cookie-authenticated unsafe methods; Bearer exempt,
- deletion concept: session TTL, /me/data sweep hits only the caller,
  report field auth_sessions_deleted.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import rag_assistant.api as api_mod
from conftest import make_settings
from rag_assistant.auth import AuthSessionStore, OidcAuth
from rag_assistant.config import Registry
from rag_assistant.deletion import DeletionCascade
from rag_assistant.domain import CollectionCfg, DataClass
from rag_assistant.obs import Metrics
from rag_assistant.testing.fakes import FakeAuditLog
from test_auth_oidc import ISSUER, KEY, StubJWKClient, jwks_for, make_token


class FakeRedis:
    """The Redis subset the auth flow touches: set(ex)/get/delete/scan_iter."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.data.pop(key, None) is not None else 0

    async def scan_iter(self, match=None, count=100):
        for key in list(self.data):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


class FakeQueryStore:
    async def add_feedback(self, *args, **kwargs) -> None:
        return None

    async def list_documents(self, scope):
        return []

    async def delete_user_feedback(self, scope, user_id) -> int:
        return 1


class SpySessions:
    async def delete_user_sessions(self, tenant, user_id) -> int:
        return 0


def _registry() -> Registry:
    return Registry(
        default_tenant="nordfels",
        default_collection="handbuecher",
        collections={
            "handbuecher": CollectionCfg(
                name="handbuecher", tenant="nordfels", data_class=DataClass.INTERNAL
            ),
        },
    )


DISCOVERY = {
    "authorization_endpoint": ISSUER + "/auth",
    "token_endpoint": ISSUER + "/token",
    "jwks_uri": ISSUER + "/keys",
}


@pytest.fixture
def oidc_state(monkeypatch):
    def _setup(**overrides):
        values: dict = {
            "deployment_mode": "demo",
            "auth_backend": "oidc",
            "oidc_issuer_url": ISSUER,
            "rate_limit_enabled": False,
        }
        values.update(overrides)
        s = make_settings(**values)
        redis = FakeRedis()
        auth = OidcAuth(s, jwk_client=StubJWKClient(jwks_for(KEY)))
        auth._discovery = dict(DISCOVERY)  # no network: discovery pre-cached
        sessions = AuthSessionStore(redis, ttl_s=s.auth_session_ttl_s)
        auth.sessions = sessions
        deleter = DeletionCascade(
            FakeQueryStore(), None, SpySessions(), audit=None, auth_sessions=sessions
        )
        monkeypatch.setattr(api_mod.state, "settings", s, raising=False)
        monkeypatch.setattr(api_mod.state, "registry", _registry(), raising=False)
        monkeypatch.setattr(api_mod.state, "auth", auth, raising=False)
        monkeypatch.setattr(api_mod.state, "auth_sessions", sessions, raising=False)
        monkeypatch.setattr(api_mod.state, "redis", redis, raising=False)
        monkeypatch.setattr(api_mod.state, "audit", FakeAuditLog(), raising=False)
        monkeypatch.setattr(api_mod.state, "metrics", Metrics(), raising=False)
        monkeypatch.setattr(api_mod.state, "store", FakeQueryStore(), raising=False)
        monkeypatch.setattr(api_mod.state, "deleter", deleter, raising=False)
        return s, redis, auth, sessions

    return _setup


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_mod.app), base_url="http://test")


async def _login_flow(c: httpx.AsyncClient, redis: FakeRedis) -> tuple[str, str]:
    """Run /auth/login; returns (flow_state, code_verifier) from Redis."""
    r = await c.get("/auth/login")
    assert r.status_code == 302
    query = parse_qs(urlsplit(r.headers["location"]).query)
    flow_state = query["state"][0]
    verifier = json.loads(redis.data[f"authflow:{flow_state}"])["code_verifier"]
    return flow_state, verifier


def _stub_exchange(auth: OidcAuth, id_token: str) -> list[dict]:
    calls: list[dict] = []

    async def fake_exchange(code, code_verifier, redirect_uri):
        calls.append({"code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri})
        return id_token

    auth.exchange_code = fake_exchange  # type: ignore[method-assign]
    return calls


async def _establish_session(c, redis, auth, department="it") -> tuple[str, str]:
    """Full login round-trip; returns (cookie_value, csrf)."""
    flow_state, _ = await _login_flow(c, redis)
    _stub_exchange(auth, make_token(claims={"department": department}))
    r = await c.get("/auth/callback", params={"code": "abc", "state": flow_state})
    assert r.status_code == 302
    cookie = c.cookies.get("rag_session")
    assert cookie
    rs = await c.get("/auth/session")
    return cookie, rs.json()["csrf"]


# ── login ─────────────────────────────────────────────────────────────────────
async def test_login_redirects_with_state_and_pkce_s256(oidc_state):
    _, redis, _, _ = oidc_state()
    async with client() as c:
        r = await c.get("/auth/login")
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert r.headers["location"].startswith(DISCOVERY["authorization_endpoint"])
    q = parse_qs(url.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["rag-assistant"]
    assert q["redirect_uri"] == ["http://localhost:8000/auth/callback"]
    assert q["code_challenge_method"] == ["S256"]
    flow_state = q["state"][0]
    stored = json.loads(redis.data[f"authflow:{flow_state}"])
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(stored["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert q["code_challenge"] == [expected]
    assert redis.ttls[f"authflow:{flow_state}"] == 600


async def test_login_is_404_in_static_key_mode(oidc_state):
    # Assemble oidc state, then swap the adapter back: the routes must keep
    # the static-key surface byte-identical.
    from rag_assistant.auth import StaticKeyAuth

    oidc_state()
    api_mod.state.auth = StaticKeyAuth()
    async with client() as c:
        assert (await c.get("/auth/login")).status_code == 404
        assert (await c.get("/auth/session")).status_code == 404
        assert (await c.post("/auth/logout")).status_code == 404


# ── callback (BFF core proof) ─────────────────────────────────────────────────
async def test_callback_sets_httponly_cookie_and_leaks_no_token(oidc_state):
    _, redis, auth, sessions = oidc_state()
    id_token = make_token(claims={"department": "it"})
    async with client() as c:
        flow_state, verifier = await _login_flow(c, redis)
        calls = _stub_exchange(auth, id_token)
        r = await c.get("/auth/callback", params={"code": "code-1", "state": flow_state})
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # confidential client + PKCE verifier went to the backchannel
    assert calls == [
        {
            "code": "code-1",
            "code_verifier": verifier,
            "redirect_uri": "http://localhost:8000/auth/callback",
        }
    ]
    # BFF core proof: the id_token appears NOWHERE in the browser response.
    dump = r.text + json.dumps(dict(r.headers))
    assert id_token not in dump
    set_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie and "Path=/" in set_cookie
    assert "Secure" not in set_cookie  # session_cookie_secure=False (demo default)
    # the session exists, user-bound, with the department from the claim
    [key] = [k for k in redis.data if k.startswith("authsession:")]
    assert key.startswith("authsession:nordfels:sub-anna-7f3:")
    assert json.loads(redis.data[key])["department"] == "it"


async def test_callback_secure_flag_follows_the_setting(oidc_state):
    _, redis, auth, _ = oidc_state(session_cookie_secure=True)
    async with client() as c:
        flow_state, _ = await _login_flow(c, redis)
        _stub_exchange(auth, make_token(claims={"department": "it"}))
        r = await c.get("/auth/callback", params={"code": "x", "state": flow_state})
    assert "Secure" in r.headers["set-cookie"]


async def test_state_is_single_use_replay_is_neutral_400(oidc_state):
    _, redis, auth, _ = oidc_state()
    async with client() as c:
        flow_state, _ = await _login_flow(c, redis)
        _stub_exchange(auth, make_token(claims={"department": "it"}))
        first = await c.get("/auth/callback", params={"code": "x", "state": flow_state})
        replay = await c.get("/auth/callback", params={"code": "x", "state": flow_state})
    assert first.status_code == 302
    assert replay.status_code == 400
    assert "state" not in replay.json()["detail"]  # neutral text


async def test_unknown_state_is_neutral_400(oidc_state):
    oidc_state()
    async with client() as c:
        r = await c.get("/auth/callback", params={"code": "x", "state": "forged"})
    assert r.status_code == 400


async def test_callback_without_department_claim_is_403(oidc_state):
    _, redis, auth, _ = oidc_state()
    async with client() as c:
        flow_state, _ = await _login_flow(c, redis)
        _stub_exchange(auth, make_token())  # no department, no bridge map
        r = await c.get("/auth/callback", params={"code": "x", "state": flow_state})
    assert r.status_code == 403
    assert not any(k.startswith("authsession:") for k in redis.data)


# ── session bootstrap + logout ────────────────────────────────────────────────
async def test_session_bootstrap_returns_identity_and_csrf(oidc_state):
    _, redis, auth, _ = oidc_state()
    async with client() as c:
        assert (await c.get("/auth/session")).status_code == 401
        await _establish_session(c, redis, auth)
        r = await c.get("/auth/session")
    body = r.json()
    assert body["user_id"] == "sub-anna-7f3"
    assert body["department"] == "it"
    assert len(body["csrf"]) > 30


async def test_session_ttl_is_the_deletion_concept_backstop(oidc_state):
    s, redis, auth, _ = oidc_state()
    async with client() as c:
        await _establish_session(c, redis, auth)
    [key] = [k for k in redis.data if k.startswith("authsession:")]
    assert redis.ttls[key] == s.auth_session_ttl_s == 28800  # 8 h default


async def test_logout_requires_csrf_and_deletes_only_the_own_session(oidc_state):
    _, redis, auth, sessions = oidc_state()
    # a second user's session must survive the first user's logout
    await sessions.create("nordfels", "sub-ben-9c1", "hr")
    async with client() as c:
        _, csrf = await _establish_session(c, redis, auth)
        no_token = await c.post("/auth/logout")
        assert no_token.status_code == 403
        wrong = await c.post("/auth/logout", headers={"X-CSRF-Token": "forged"})
        assert wrong.status_code == 403
        assert any(k.startswith("authsession:nordfels:sub-anna-7f3:") for k in redis.data)
        ok = await c.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 204
    assert 'rag_session=""' in ok.headers["set-cookie"]
    assert not any(k.startswith("authsession:nordfels:sub-anna-7f3:") for k in redis.data)
    assert any(k.startswith("authsession:nordfels:sub-ben-9c1:") for k in redis.data)


# ── CSRF on the API surface ───────────────────────────────────────────────────
FEEDBACK = {"rating": 1, "condensed_question": "q"}


async def test_cookie_post_without_csrf_token_is_403(oidc_state):
    _, redis, auth, _ = oidc_state()
    async with client() as c:
        await _establish_session(c, redis, auth)
        r = await c.post("/feedback", json=FEEDBACK)
    assert r.status_code == 403


async def test_cookie_post_with_wrong_csrf_token_is_403(oidc_state):
    _, redis, auth, _ = oidc_state()
    async with client() as c:
        await _establish_session(c, redis, auth)
        r = await c.post("/feedback", json=FEEDBACK, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403


async def test_cookie_post_with_correct_csrf_token_passes(oidc_state):
    _, redis, auth, _ = oidc_state()
    async with client() as c:
        _, csrf = await _establish_session(c, redis, auth)
        r = await c.post("/feedback", json=FEEDBACK, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200


async def test_cookie_get_needs_no_csrf_token(oidc_state):
    _, redis, auth, _ = oidc_state()
    async with client() as c:
        await _establish_session(c, redis, auth)
        r = await c.get("/documents")
    assert r.status_code == 200


async def test_bearer_is_csrf_exempt(oidc_state):
    # Header auth is CSRF-immune: browsers never attach Authorization
    # cross-site — the E4 exemption, asserted end-to-end.
    oidc_state()
    token = make_token(claims={"department": "it"})
    async with client() as c:
        r = await c.post("/feedback", json=FEEDBACK, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_invalid_bearer_is_401(oidc_state):
    oidc_state()
    async with client() as c:
        r = await c.get("/documents", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


# ── deletion concept: /me/data sweep ──────────────────────────────────────────
async def test_me_data_sweeps_only_the_callers_auth_sessions(oidc_state):
    _, redis, auth, sessions = oidc_state()
    await sessions.create("nordfels", "sub-ben-9c1", "hr")
    async with client() as c:
        _, csrf = await _establish_session(c, redis, auth)
        await sessions.create("nordfels", "sub-anna-7f3", "it")  # a second device
        r = await c.delete("/me/data", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    report = r.json()
    assert report["auth_sessions_deleted"] == 2  # both of Anna's, nobody else's
    assert not any(k.startswith("authsession:nordfels:sub-anna-7f3:") for k in redis.data)
    assert any(k.startswith("authsession:nordfels:sub-ben-9c1:") for k in redis.data)
