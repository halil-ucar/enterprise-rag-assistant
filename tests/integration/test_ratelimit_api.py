"""End-to-end rate limit proof against real Redis (CI services).

Small limits, full lifespan: the third /query request must be rejected with
429 + Retry-After while /health stays reachable in parallel (free route class).
"""

from __future__ import annotations

import asyncio
import os

import httpx
import psycopg
import pytest

import rag_assistant.api as api_mod
from rag_assistant.config import Settings

pytestmark = pytest.mark.integration


async def test_third_query_is_rate_limited_and_health_stays_free(monkeypatch):
    # Env-driven settings (CI: real Postgres/Redis, fake inference backends)
    # with limits shrunk so the third request exceeds the burst.
    monkeypatch.setattr(
        api_mod,
        "get_settings",
        lambda: Settings(rate_limit_query_burst=2, rate_limit_query_per_min=1),
    )
    async with api_mod.lifespan(api_mod.app):
        transport = httpx.ASGITransport(app=api_mod.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as c:
            headers = {"X-API-Key": api_mod.state.settings.api_key_anna}
            # Requests 1-2 pass the limiter (the SSE stream may then carry a
            # structured error frame — no live LLM in CI — which is fine here:
            # under test is the 429 path, not generation).
            for _ in range(2):
                r = await c.post("/query", json={"question": "Was ist NF-4102?"}, headers=headers)
                assert r.status_code == 200
            r = await c.post("/query", json={"question": "Was ist NF-4102?"}, headers=headers)
            assert r.status_code == 429
            assert int(r.headers["Retry-After"]) >= 1
            # the free route class stays reachable while the query bucket is empty
            health = await c.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

        # A4: the 429 leaves a rate_limited audit event. Verified via the owner
        # DSN — rag_app itself cannot SELECT audit_events, which is the point.
        # The audit write is fire-and-forget by design (best-effort audit), so
        # poll with a deadline instead of racing it with a fixed sleep.
        dsn = os.environ.get("AUDIT_CHECK_DSN")
        if dsn:
            aconn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
            try:
                count = 0
                for _ in range(50):
                    cur = await aconn.execute(
                        "SELECT count(*) FROM audit_events WHERE event_type = 'rate_limited'"
                        "  AND decision = 'limited' AND user_id = 'anna'"
                        "  AND ts > now() - interval '1 minute'"
                    )
                    count = (await cur.fetchone())[0]
                    if count >= 1:
                        break
                    await asyncio.sleep(0.1)
                assert count >= 1
            finally:
                await aconn.close()
