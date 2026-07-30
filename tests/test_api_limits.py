"""Endpoint-level unit tests for rate limiting + size caps (A3).

No services: the app state is assembled by hand (fake rate-limit store, fake
feedback store) and requests go through httpx's ASGI transport without the
lifespan. Error texts must stay neutral — never echo request content.
"""

from __future__ import annotations

import httpx
import pytest

import rag_assistant.api as api_mod
from conftest import make_registry, make_settings
from rag_assistant.obs import Metrics
from rag_assistant.ratelimit import RedisRateLimiter
from rag_assistant.testing.fakes import FakeAuditLog, FakeRateLimitStore

ANNA = {"X-API-Key": "demo-anna-it"}


class FakeFeedbackStore:
    async def add_feedback(self, *args, **kwargs) -> None:
        return None


@pytest.fixture
def demo_state(monkeypatch):
    def _setup(**settings_overrides):
        s = make_settings(
            deployment_mode="demo",
            api_key_anna="demo-anna-it",
            api_key_ben="demo-ben-hr",
            **settings_overrides,
        )
        monkeypatch.setattr(api_mod.state, "settings", s, raising=False)
        monkeypatch.setattr(api_mod.state, "registry", make_registry(), raising=False)
        monkeypatch.setattr(
            api_mod.state, "ratelimiter", RedisRateLimiter(FakeRateLimitStore()), raising=False
        )
        monkeypatch.setattr(api_mod.state, "store", FakeFeedbackStore(), raising=False)
        # Fresh per-test instances (A4/A5): the middleware and hooks expect them.
        monkeypatch.setattr(api_mod.state, "metrics", Metrics(), raising=False)
        monkeypatch.setattr(api_mod.state, "audit", FakeAuditLog(), raising=False)
        return s

    return _setup


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_mod.app), base_url="http://test")


# ── size caps ─────────────────────────────────────────────────────────────────
async def test_oversized_question_is_rejected_with_413(demo_state):
    demo_state(max_question_chars=50)
    secret_question = "Streng geheime Frage " + "x" * 60
    async with client() as c:
        r = await c.post("/query", json={"question": secret_question}, headers=ANNA)
    assert r.status_code == 413
    assert "50 character limit" in r.json()["detail"]
    # neutral error: no request content in the response
    assert "geheim" not in r.text


@pytest.mark.parametrize("field", ["collection", "session_id"])
async def test_overlong_identifier_fields_are_rejected_with_422(demo_state, field):
    demo_state()
    async with client() as c:
        r = await c.post("/query", json={"question": "ok", field: "x" * 201}, headers=ANNA)
    assert r.status_code == 422


async def test_oversized_ingest_content_is_rejected_with_413(demo_state):
    demo_state(max_ingest_bytes=100)
    async with client() as c:
        r = await c.post(
            "/ingest",
            json={"doc_id": "d1", "title": "t", "content_text": "A" * 101},
            headers=ANNA,
        )
    assert r.status_code == 413
    assert "100 byte limit" in r.json()["detail"]
    assert "AAA" not in r.text


async def test_overlong_ingest_collection_is_rejected_with_422(demo_state):
    demo_state()
    async with client() as c:
        r = await c.post(
            "/ingest",
            json={"doc_id": "d1", "title": "t", "content_text": "ok", "collection": "x" * 201},
            headers=ANNA,
        )
    assert r.status_code == 422


# ── rate limiting through the dependency ──────────────────────────────────────
async def test_third_request_hits_429_with_retry_after(demo_state):
    demo_state(rate_limit_query_burst=2, rate_limit_query_per_min=1)
    async with client() as c:
        for _ in range(2):
            r = await c.post(
                "/feedback",
                json={"rating": 1, "condensed_question": "q"},
                headers=ANNA,
            )
            assert r.status_code == 200
        r = await c.post("/feedback", json={"rating": 1, "condensed_question": "q"}, headers=ANNA)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    # neutral detail, no content
    assert r.json()["detail"] == "rate limit exceeded — retry later"


async def test_rate_limit_is_per_identity(demo_state):
    demo_state(rate_limit_query_burst=1, rate_limit_query_per_min=1)
    async with client() as c:
        r1 = await c.post("/feedback", json={"rating": 1, "condensed_question": "q"}, headers=ANNA)
        r2 = await c.post("/feedback", json={"rating": 1, "condensed_question": "q"}, headers=ANNA)
        r3 = await c.post(
            "/feedback",
            json={"rating": 1, "condensed_question": "q"},
            headers={"X-API-Key": "demo-ben-hr"},
        )
    assert (r1.status_code, r2.status_code, r3.status_code) == (200, 429, 200)


async def test_disabled_rate_limiting_passes_everything(demo_state):
    demo_state(rate_limit_enabled=False, rate_limit_query_burst=1, rate_limit_query_per_min=1)
    async with client() as c:
        for _ in range(4):
            r = await c.post(
                "/feedback", json={"rating": 1, "condensed_question": "q"}, headers=ANNA
            )
            assert r.status_code == 200


async def test_fail_open_at_the_endpoint(demo_state, caplog, monkeypatch):
    """A broken limiter store must not take the API down (fail-open, ERROR log)."""
    import logging

    demo_state(rate_limit_query_burst=1, rate_limit_query_per_min=1)

    class BrokenStore:
        async def get(self, key: str) -> str | None:
            raise ConnectionError("redis down")

        async def set(self, key: str, value: str, *, ex: int) -> None:
            raise ConnectionError("redis down")

    monkeypatch.setattr(api_mod.state, "ratelimiter", RedisRateLimiter(BrokenStore()))
    with caplog.at_level(logging.ERROR, logger="rag_assistant.ratelimit"):
        async with client() as c:
            for _ in range(3):
                r = await c.post(
                    "/feedback", json={"rating": 1, "condensed_question": "q"}, headers=ANNA
                )
                assert r.status_code == 200
    assert any("failing open" in r.message for r in caplog.records)
