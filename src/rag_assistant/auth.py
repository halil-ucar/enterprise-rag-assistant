"""Auth port (A6): pluggable authentication adapters behind ONE dependency.

`api.require_scope` stays the single auth dependency; it delegates to the
adapter selected ONCE in the lifespan from the `auth_backend` SETTING —
runtime behavior never branches on deployment_mode (E2). Adapters return a
QueryScope or None; the caller owns the 401 and its audit event
(`build_auth_failure()` is parameterless BY DESIGN — no presented credential
can ever reach the audit trail).

StaticKeyAuth carries the demo auth verbatim (formerly security.py): two
static API keys mapping to two fixed demo users make the RLS story
demonstrable over the API and in the UI. The QueryScope contract is
identical for every adapter — RLS and the cache key never change.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Protocol

import httpx
import jwt
import redis.asyncio as aioredis
from fastapi import HTTPException, Request

from .config import Settings
from .domain import QueryScope

# BFF session cookie: HttpOnly + SameSite=Lax; the browser never sees a token.
SESSION_COOKIE = "rag_session"

# Cookie-authenticated UNSAFE methods require the CSRF header (E4). Header
# auth (X-API-Key, Bearer) is CSRF-immune — browsers never attach those
# headers cross-site — so those paths are exempt by construction.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def parse_session_cookie(value: str | None) -> tuple[str, str] | None:
    """Cookie value '{user_id}:{sid}' → (user_id, sid). The auth-session key
    is user-bound (sessions.py pattern — exact-prefix deletion), so the
    cookie must carry the user id to reconstruct the key without a
    per-request SCAN. Both parts are opaque/pseudonymous (user_id = sub, and
    /auth/session returns it to the UI anyway); the sid alone is the secret.
    sid is token_urlsafe and never contains ':' — rpartition is exact."""
    if not value:
        return None
    user_id, sep, sid = value.rpartition(":")
    if not sep or not user_id or not sid:
        return None
    return user_id, sid


# Explicit algorithm allowlist: asymmetric signatures only. The alg-none
# class (and any HMAC downgrade against a public key) is structurally
# excluded — pyjwt rejects everything outside this list before verifying.
OIDC_ALGORITHMS = ["RS256", "ES256"]

# exp/nbf clock-skew tolerance (seconds) between IdP and API host.
OIDC_LEEWAY_S = 30


def parse_demo_department_map(raw: str) -> dict[str, str]:
    """Fail-closed parse of OIDC_DEMO_DEPARTMENT_MAP: a JSON object mapping
    email → department, empty = no bridge. Same posture as
    OPENAI_EXTRA_BODY (parse errors abort startup, never a silent skip);
    readiness.validate_config_values calls this in every mode."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OIDC_DEMO_DEPARTMENT_MAP is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("OIDC_DEMO_DEPARTMENT_MAP must be a JSON object of email→department")
    return value


class AuthAdapter(Protocol):
    """Request → scope. None means 'not authenticated'; adapters never raise
    for bad credentials — the 401 (and its audit event) belongs to the one
    require_scope dependency in api.py. An AUTHENTICATED principal that is
    not authorizable (verified token, no department claim) is the adapter's
    403 — fail-closed, never a default department."""

    unauthorized_detail: str

    async def authenticate(
        self, request: Request, settings: Settings, tenant: str
    ) -> QueryScope | None: ...


class StaticKeyAuth:
    """API-key → user mapping (demo auth). Header X-API-Key, mapping from
    Settings — the resolve_scope logic, unchanged."""

    unauthorized_detail = "invalid or missing X-API-Key"

    async def authenticate(
        self, request: Request, settings: Settings, tenant: str
    ) -> QueryScope | None:
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return None
        mapping = {
            settings.api_key_anna: ("anna", "it"),
            settings.api_key_ben: ("ben", "hr"),
        }
        if api_key not in mapping:
            return None
        user_id, department = mapping[api_key]
        return QueryScope(tenant=tenant, user_id=user_id, department=department)


class AuthSessionStore:
    """BFF auth sessions: opaque ids in Redis (E5) — pattern: SessionStore.

    Keys are bound to the USER (`authsession:{tenant}:{user_id}:{sid}`) so
    user-data deletion is an exact user-prefix sweep — the same proven pattern as
    sessions.SessionStore.delete_user_sessions. The value carries only what
    a request needs (department, csrf, created); nothing token-shaped is
    ever stored. TTL is the deletion-concept deadline (8 h default); logout
    and DELETE /me/data remove sessions immediately.
    """

    def __init__(self, redis: aioredis.Redis, ttl_s: int = 28800):
        self.redis = redis
        self.ttl_s = ttl_s

    @staticmethod
    def _key(tenant: str, user_id: str, sid: str) -> str:
        return f"authsession:{tenant}:{user_id}:{sid}"

    async def create(self, tenant: str, user_id: str, department: str) -> tuple[str, str]:
        """Returns (sid, csrf), both fresh 256-bit urlsafe tokens."""
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        value = json.dumps(
            {"department": department, "csrf": csrf, "created": int(time.time())},
            ensure_ascii=False,
        )
        await self.redis.set(self._key(tenant, user_id, sid), value, ex=self.ttl_s)
        return sid, csrf

    async def get(self, tenant: str, user_id: str, sid: str) -> dict | None:
        raw = await self.redis.get(self._key(tenant, user_id, sid))
        return json.loads(raw) if raw else None

    async def delete(self, tenant: str, user_id: str, sid: str) -> int:
        return int(await self.redis.delete(self._key(tenant, user_id, sid)))

    async def delete_user_auth_sessions(self, tenant: str, user_id: str) -> int:
        """Delete every auth session of ONE user (logout-everywhere sweep for
        DELETE /me/data). Exact user prefix — same pattern as sessions.py."""
        pattern = f"authsession:{tenant}:{user_id}:*"
        deleted = 0
        async for key in self.redis.scan_iter(match=pattern, count=100):
            deleted += int(await self.redis.delete(key))
        return deleted


class OidcAuth:
    """OIDC token validation (A6). IdP-agnostic: only Discovery + JWKS —
    no vendor SDK, no IdP-specific code path.

    Issuer consistency (the classic container↔browser pitfall): the iss claim
    carries the PUBLIC issuer URL (browser-facing, e.g.
    http://localhost:5556/dex) and is validated against exactly that — NEVER
    relaxed. Containers reach the IdP over `oidc_internal_issuer_url`
    (e.g. http://dex:5556/dex): backchannel URLs from the discovery document
    (token endpoint, JWKS URI) are rewritten by prefix replacement
    issuer→internal; the authorization endpoint stays public because the
    BROWSER is redirected there, not the API container.

    The PyJWKClient blocks (urllib) only on a JWKS cache miss — calls go
    through asyncio.to_thread. Discovery is fetched lazily and cached: an
    unreachable IdP surfaces as a clean login/auth error, never as a startup
    coupling (/ready deliberately has no IdP check — documented extension).
    """

    unauthorized_detail = "not authenticated"

    def __init__(self, settings: Settings, jwk_client: jwt.PyJWKClient | None = None):
        self.issuer = settings.oidc_issuer_url
        self.internal_base = settings.oidc_internal_issuer_url or settings.oidc_issuer_url
        self.client_id = settings.oidc_client_id
        self.client_secret = settings.oidc_client_secret
        self.audience = settings.oidc_audience or settings.oidc_client_id
        self.claim_department = settings.oidc_claim_department
        # Fail-closed at construction (lifespan): a typo in the map JSON is a
        # startup error, not a silently absent demo bridge.
        self.demo_department_map = parse_demo_department_map(settings.oidc_demo_department_map)
        # Wired in the lifespan (needs the Redis connection); None = the
        # cookie path is inactive (token-validation unit tests).
        self.sessions: AuthSessionStore | None = None
        self._jwk_client = jwk_client
        self._discovery: dict | None = None

    def _rewrite_backchannel(self, url: str) -> str:
        # Prefix replacement public issuer → internal base, fetch path only.
        if self.internal_base != self.issuer and url.startswith(self.issuer):
            return self.internal_base + url.removeprefix(self.issuer)
        return url

    async def discovery(self) -> dict:
        """Fetch + cache the discovery document over the backchannel base.
        token_endpoint/jwks_uri are rewritten to the internal base (the API
        fetches them); authorization_endpoint stays public (the browser is
        redirected there). The iss VALIDATION stays on the public issuer."""
        if self._discovery is None:
            url = self.internal_base.rstrip("/") + "/.well-known/openid-configuration"
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                doc = r.json()
            for key in ("token_endpoint", "jwks_uri"):
                if isinstance(doc.get(key), str):
                    doc[key] = self._rewrite_backchannel(doc[key])
            self._discovery = doc
        return self._discovery

    async def _get_jwk_client(self) -> jwt.PyJWKClient:
        if self._jwk_client is None:
            doc = await self.discovery()
            self._jwk_client = jwt.PyJWKClient(doc["jwks_uri"])
        return self._jwk_client

    def _decode(self, jwk_client: jwt.PyJWKClient, token: str) -> dict:
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=OIDC_ALGORITHMS,
            issuer=self.issuer,
            audience=self.audience,
            leeway=OIDC_LEEWAY_S,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )

    async def validate_token(self, token: str) -> dict | None:
        """Verify signature/issuer/audience/exp/nbf; None on ANY failure —
        the caller owns the 401. Nothing about the token reaches logs."""
        jwk_client = await self._get_jwk_client()
        try:
            return await asyncio.to_thread(self._decode, jwk_client, token)
        except jwt.PyJWTError:
            return None

    def scope_from_claims(self, claims: dict, tenant: str) -> QueryScope:
        """Data minimization: user_id = `sub` (stable pseudonymous id —
        deliberately NOT email/name; nothing else is carried over).
        Missing/empty department → 403 fail-closed, NO default department.
        Demo bridge (R10 forbids it in production): only when the claim is
        missing AND the map is configured, email→department comes from the
        map — the email is read for the lookup, never stored."""
        department = str(claims.get(self.claim_department) or "")
        if not department and self.demo_department_map:
            department = self.demo_department_map.get(str(claims.get("email") or ""), "")
        if not department:
            raise HTTPException(status_code=403, detail="no department claim — access denied")
        return QueryScope(tenant=tenant, user_id=str(claims["sub"]), department=department)

    async def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> str:
        """Authorization-code → id_token over the BACKCHANNEL token endpoint
        (confidential client: client_secret + PKCE code_verifier). Returns
        the raw id_token — validation is the caller's next step; the token
        never appears in any response, log or exception text."""
        doc = await self.discovery()
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                doc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code_verifier": code_verifier,
                },
            )
            r.raise_for_status()
            payload = r.json()
        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ValueError("token response carries no id_token")
        return id_token

    async def authenticate(
        self, request: Request, settings: Settings, tenant: str
    ) -> QueryScope | None:
        # Header auth first: Bearer tokens (API clients) are CSRF-immune —
        # browsers never attach the header cross-site — so no CSRF check here.
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            claims = await self.validate_token(authorization.removeprefix("Bearer "))
            if claims is None:
                return None
            return self.scope_from_claims(claims, tenant)
        # Cookie path (BFF session). The cookie automatically travels with
        # cross-site requests — CSRF protection is MANDATORY here (E4):
        # SameSite=Lax as layer 1, the per-session token as layer 2.
        parsed = parse_session_cookie(request.cookies.get(SESSION_COOKIE))
        if parsed is not None and self.sessions is not None:
            user_id, sid = parsed
            session = await self.sessions.get(tenant, user_id, sid)
            if session is not None:
                if request.method not in _SAFE_METHODS:
                    token = request.headers.get("X-CSRF-Token", "")
                    if not token or not secrets.compare_digest(token, session["csrf"]):
                        # Neutral text: no hint whether cookie or token failed.
                        raise HTTPException(status_code=403, detail="request rejected")
                return QueryScope(tenant=tenant, user_id=user_id, department=session["department"])
        return None
