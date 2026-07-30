"""Audit trail integration proofs against real Postgres + Redis (A4).

Four proofs that only the real database can give:
  1. Append-only is a GRANT property: rag_app can INSERT into audit_events
     but neither UPDATE nor DELETE — immutability lives in the DB, not in
     app-code discipline.
  2. Surrogate death: a query event's doc_refs resolve against
     documents.audit_ref before the deletion cascade and to nothing after,
     while the content-free receipt survives with counts but no slug.
  3. Retention: purge_expired honors the interval; the SQL-side 30-day
     GREATEST floor makes purge(0) leave young rows alone.
  4. Marker sweep: a marker planted in a real /query request appears in NO
     audit_events column.

Verification READS use AUDIT_CHECK_DSN (rag_owner): rag_app has no SELECT on
audit_events — that restriction IS the proof, not an obstacle. Tests that
need to read skip without the variable.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import httpx
import psycopg
import pytest

import rag_assistant.api as api_mod
from rag_assistant.audit import build_query_event
from rag_assistant.cache import AnswerCache
from rag_assistant.chunking import chunk_markdown
from rag_assistant.config import Settings
from rag_assistant.deletion import DeletionCascade
from rag_assistant.domain import QueryScope
from rag_assistant.llm.registry import ProviderRegistry
from rag_assistant.sessions import SessionStore
from rag_assistant.store.audit import PgAuditLog
from rag_assistant.store.pg import PgBackend, PgStore
from rag_assistant.testing.fakes import FakeEmbedder, FakeLLM

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://rag_app:rag_app_pw@localhost:5432/rag")
AUDIT_CHECK_DSN = os.environ.get("AUDIT_CHECK_DSN", "")

requires_owner_read = pytest.mark.skipif(
    not AUDIT_CHECK_DSN,
    reason="AUDIT_CHECK_DSN (rag_owner) not set — audit verification reads impossible",
)

ANNA_IT = QueryScope(tenant="nordfels", user_id="anna", department="it")

VPN_DOC = """# VPN-Handbuch

## Fehlerbehebung

Der Fehlercode NF-4102 bedeutet: Zertifikat abgelaufen.
"""


@pytest.fixture
async def backend():
    b = PgBackend(DATABASE_URL)
    await b.open()
    yield b
    await b.close()


@pytest.fixture
async def owner_conn():
    conn = await psycopg.AsyncConnection.connect(AUDIT_CHECK_DSN, autocommit=True)
    yield conn
    await conn.close()


async def _ingest(store: PgStore, embedder: FakeEmbedder, doc_id: str) -> None:
    chunks = chunk_markdown(VPN_DOC, "VPN-Handbuch")
    vectors = await embedder.embed([c.content for c in chunks])
    await store.upsert_document(
        ANNA_IT,
        "handbuecher",
        doc_id,
        "VPN-Handbuch",
        hashlib.sha256(VPN_DOC.encode()).hexdigest(),
        "all",
        chunks,
        vectors,
        1,
    )


# ── 1. append-only by grants ──────────────────────────────────────────────────
async def test_rag_app_can_insert_but_never_update_or_delete(backend):
    async with backend.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO audit_events (event_type, decision) VALUES ('query', 'failed')"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with backend.pool.connection() as conn:
            await conn.execute("UPDATE audit_events SET decision = 'denied'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with backend.pool.connection() as conn:
            await conn.execute("DELETE FROM audit_events")


async def test_rag_app_cannot_even_read_the_log(backend):
    """No SELECT grant: reading is structurally impossible for the app role —
    the owner DSN in the tests below exists precisely because of this."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with backend.pool.connection() as conn:
            await conn.execute("SELECT count(*) FROM audit_events")


# ── 2. surrogate death ────────────────────────────────────────────────────────
@requires_owner_read
async def test_surrogate_resolves_before_cascade_and_dies_with_it(backend, owner_conn):
    store, audit = PgStore(backend), PgAuditLog(backend)
    embedder = FakeEmbedder(dim=1024)
    doc_id = f"audit-vpn-{uuid.uuid4().hex[:8]}"
    request_id = uuid.uuid4().hex
    await _ingest(store, embedder, doc_id)

    refs = await store.audit_refs_for(ANNA_IT, [doc_id])
    assert len(refs) == 1
    await audit.write(
        build_query_event(ANNA_IT, "handbuecher", "internal", tuple(refs), True, request_id)
    )

    join_sql = (
        "SELECT count(*) FROM audit_events a JOIN documents d"
        "  ON d.audit_ref = ANY(a.doc_refs) WHERE a.request_id = %s"
    )
    cur = await owner_conn.execute(join_sql, (request_id,))
    assert (await cur.fetchone())[0] == 1  # resolvable while the document lives

    import redis.asyncio as aioredis

    redis = aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True
    )
    deleter = DeletionCascade(store, AnswerCache(redis), SessionStore(redis), audit=audit)
    report = await deleter.delete_document(ANNA_IT, doc_id)
    await redis.aclose()
    assert report.found is True and report.audit_ref == refs[0]

    cur = await owner_conn.execute(join_sql, (request_id,))
    assert (await cur.fetchone())[0] == 0  # the reference is dead

    # the receipt survives: counts yes, slug no
    cur = await owner_conn.execute(
        "SELECT chunks_deleted, audit_events::text FROM audit_events"
        " WHERE event_type = 'delete' AND %s = ANY(doc_refs)",
        (refs[0],),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] > 0
    assert doc_id not in row[1]


# ── 3. retention with the 30-day floor ────────────────────────────────────────
@requires_owner_read
async def test_retention_purges_old_rows_and_the_floor_protects_young_ones(backend, owner_conn):
    audit = PgAuditLog(backend)
    old_marker, fresh_marker = uuid.uuid4().hex, uuid.uuid4().hex
    # The INSERT grant allows an explicit ts — and rag_app cannot UPDATE it
    # afterwards, which is exactly the append-only property under test.
    async with backend.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO audit_events (ts, event_type, decision, request_id)"
            " VALUES (now() - interval '400 days', 'query', 'failed', %s)",
            (old_marker,),
        )
        await conn.execute(
            "INSERT INTO audit_events (event_type, decision, request_id) VALUES"
            " ('query', 'failed', %s)",
            (fresh_marker,),
        )

    assert await audit.purge_expired(365) >= 1

    async def _count(marker: str) -> int:
        cur = await owner_conn.execute(
            "SELECT count(*) FROM audit_events WHERE request_id = %s", (marker,)
        )
        return (await cur.fetchone())[0]

    assert await _count(old_marker) == 0  # expired row is gone
    assert await _count(fresh_marker) == 1  # fresh row survived

    # Defense in depth: even purge(0) cannot touch rows younger than 30 days.
    await audit.purge_expired(0)
    assert await _count(fresh_marker) == 1


# ── 4. marker sweep through the real /query path ──────────────────────────────
@requires_owner_read
async def test_query_marker_appears_in_no_audit_column(monkeypatch, owner_conn):
    """Full app (lifespan, real Postgres/Redis, fake embedder), scripted LLM:
    ask a question carrying a unique marker, then sweep every audit_events
    column for it. The access event itself must exist — with surrogate refs."""
    marker = f"MARKER{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(api_mod, "get_settings", lambda: Settings())
    async with api_mod.lifespan(api_mod.app):
        state = api_mod.state
        doc_id = f"audit-sweep-{uuid.uuid4().hex[:8]}"
        await _ingest(state.store, FakeEmbedder(dim=1024), doc_id)

        fake = FakeLLM("ollama")
        fake.queue(
            f'{{"standalone_query": "{marker} vpn fehler", "route": "direct"}}',
            "Antwort [S1].",
        )
        registry = ProviderRegistry(state.settings, providers={"ollama": fake})
        state.providers = registry
        state.pipeline.registry = registry

        transport = httpx.ASGITransport(app=api_mod.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0
        ) as c:
            r = await c.post(
                "/query",
                json={"question": f"Was bedeutet {marker}?"},
                headers={"X-API-Key": state.settings.api_key_anna},
            )
            assert r.status_code == 200
            assert "token" in r.text
        await asyncio.sleep(0.2)  # let the fire-and-forget audit task land

        cur = await owner_conn.execute(
            "SELECT count(*) FROM audit_events a WHERE a::text ILIKE '%%' || %s || '%%'",
            (marker,),
        )
        assert (await cur.fetchone())[0] == 0  # the marker reached NO column

        cur = await owner_conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type = 'query'"
            "  AND decision = 'context_served' AND user_id = 'anna'"
            "  AND doc_refs <> '{}' AND ts > now() - interval '1 minute'"
        )
        assert (await cur.fetchone())[0] >= 1  # ...but the access event exists

        await state.deleter.delete_document(ANNA_IT, doc_id)
