"""Smoke starts against real Postgres + Redis (CI services).

Demo mode proves the A1 gate changed nothing for the default mode. The A6
production smoke proves the headline of the phase: a hardened config passes
the gate and the full lifespan comes up — the gate checks CONFIG FACTS, not
IdP reachability (discovery is lazy; an unreachable IdP surfaces at login,
documented). CI runs without the ml extra, so the inference builders are
stubbed with the same fakes the unit layer uses — model loading is not what
this smoke proves.
"""

from __future__ import annotations

import httpx
import pytest

import rag_assistant.api as api_mod
from rag_assistant.config import Settings
from rag_assistant.testing.fakes import FakeEmbedder

pytestmark = pytest.mark.integration


async def test_demo_mode_starts_and_serves_health(monkeypatch):
    # Env-driven like production startup (CI provides DATABASE_URL/REDIS_URL
    # and fake inference backends); only the cache is bypassed.
    monkeypatch.setattr(api_mod, "get_settings", Settings)
    async with api_mod.lifespan(api_mod.app):
        transport = httpx.ASGITransport(app=api_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = (await client.get("/health")).json()
            assert health["status"] == "ok"
            assert health["deployment_mode"] == "demo"
            ready = await client.get("/ready")
            assert ready.status_code == 200
            assert ready.json()["status"] == "ok"


async def test_production_with_hardened_config_starts(monkeypatch):
    """A6: production is startable — with a hardened config the gate returns
    zero findings and the lifespan opens real Postgres/Redis connections.
    embeddings_backend must be 'local' to satisfy R4; the builder is stubbed
    (no ml extra in CI), reranker 'off' is gate-allowed as shipped."""
    hardened = {
        "DEPLOYMENT_MODE": "production",
        "AUTH_BACKEND": "oidc",
        "OIDC_ISSUER_URL": "http://localhost:5556/dex",
        "OIDC_DEMO_DEPARTMENT_MAP": "",
        "API_KEY_ANNA": "prod-key-anna-7f3",
        "API_KEY_BEN": "prod-key-ben-9c1",
        "EMBEDDINGS_BACKEND": "local",
        "RERANKER_BACKEND": "off",
        "AUDIT_ENABLED": "true",
        "AUDIT_FAIL_CLOSED": "true",
        "RATE_LIMIT_ENABLED": "true",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }
    for name, value in hardened.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(api_mod, "get_settings", Settings)
    monkeypatch.setattr(api_mod, "build_embedder", lambda s: FakeEmbedder(dim=1024))
    async with api_mod.lifespan(api_mod.app):
        transport = httpx.ASGITransport(app=api_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = (await client.get("/health")).json()
            assert health["deployment_mode"] == "production"
            ready = await client.get("/ready")
            assert ready.status_code == 200
            assert ready.json()["status"] == "ok"
