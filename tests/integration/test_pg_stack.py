"""Integration proof against real Postgres + Redis (CI services / `make dev` infra).

Uses the 1024-dim FakeEmbedder — no model downloads; the SQL, RLS and cascade
behaviour under test are exactly what production uses.
"""

import hashlib
import os
import uuid

import pytest
import redis.asyncio as aioredis

from rag_assistant.cache import AnswerCache
from rag_assistant.cachekey import build_answer_cache_key
from rag_assistant.chunking import chunk_markdown
from rag_assistant.deletion import DeletionCascade
from rag_assistant.domain import DataClass, QueryScope
from rag_assistant.sessions import REDACTED, SessionStore
from rag_assistant.store.pg import PgBackend, PgRetriever, PgStore
from rag_assistant.testing.fakes import FakeEmbedder

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://rag_app:rag_app_pw@localhost:5432/rag")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

ANNA_IT = QueryScope(tenant="nordfels", user_id="anna", department="it")
BEN_HR = QueryScope(tenant="nordfels", user_id="ben", department="hr")

VPN_DOC = """# VPN-Handbuch

## Fehlerbehebung

Der Fehlercode NF-4102 bedeutet: Zertifikat abgelaufen. Lösung: Zertifikat im Portal erneuern.

## Einrichtung

Den VPN-Client aus dem Softwarecenter installieren und mit Firmenkonto anmelden.
"""

HR_DOC = """# Gehaltsbänder

## Übersicht

Die internen Gehaltsbänder sind vertraulich und nur für die Abteilung HR einsehbar.
"""


@pytest.fixture
async def stack():
    backend = PgBackend(DATABASE_URL)
    await backend.open()
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    embedder = FakeEmbedder(dim=1024)
    store = PgStore(backend)
    retriever = PgRetriever(backend)
    cache = AnswerCache(redis)
    sessions = SessionStore(redis)
    deleter = DeletionCascade(store, cache, sessions)
    yield store, retriever, cache, sessions, deleter, embedder
    await backend.close()
    await redis.aclose()


async def _ingest(store, embedder, scope, collection, doc_id, title, md, department):
    chunks = chunk_markdown(md, title)
    vectors = await embedder.embed([c.content for c in chunks])
    return await store.upsert_document(
        scope,
        collection,
        doc_id,
        title,
        hashlib.sha256(md.encode()).hexdigest(),
        department,
        chunks,
        vectors,
        1,
    )


async def test_full_deletion_cascade(stack):
    """Ingest → ask (cache + session cite the doc) → delete → NOTHING remains."""
    store, retriever, cache, sessions, deleter, embedder = stack
    doc_id = f"vpn-{uuid.uuid4().hex[:8]}"
    session_id = f"s-{uuid.uuid4().hex[:8]}"

    status = await _ingest(
        store, embedder, ANNA_IT, "handbuecher", doc_id, "VPN-Handbuch", VPN_DOC, "all"
    )
    assert status == "created"

    # retrieval finds the exact error code (lexical leg) — RLS context set
    qv = (await embedder.embed(["NF-4102"]))[0]
    results = await retriever.search(ANNA_IT, "handbuecher", "Was bedeutet NF-4102?", qv, top_k=10)
    assert results and any("NF-4102" in r.content for r in results)
    assert results[0].rrf_score > 0
    # lexical leg must fire on natural-language questions (OR semantics —
    # AND semantics silently killed this leg; found via the debug panel)
    assert any(r.lex_rank is not None for r in results)

    # cache an answer citing the doc; session logs it too
    corpus_v = await store.corpus_version(ANNA_IT, "handbuecher")
    key = build_answer_cache_key(ANNA_IT, DataClass.INTERNAL, "Was bedeutet NF-4102?", corpus_v, 1)
    await cache.set(key, {"answer": "Zertifikat abgelaufen [S1]."}, [doc_id])
    await sessions.append(
        "nordfels", "anna", session_id, "assistant", "Zertifikat abgelaufen [S1].", [doc_id]
    )
    await store.add_feedback(ANNA_IT, "handbuecher", 1, "Was bedeutet NF-4102?", "direct", [doc_id])
    assert await cache.get(key) is not None

    # ── the cascade ──
    report = await deleter.delete_document(ANNA_IT, doc_id)
    assert report.chunks_deleted > 0
    assert report.cache_entries_purged == 1
    assert report.session_messages_redacted == 1
    assert report.feedback_rows_deleted == 1

    # nothing left anywhere — no candidate from the deleted document
    # (environment-independent: other docs may legitimately match the query)
    post = await retriever.search(ANNA_IT, "handbuecher", "NF-4102", qv, top_k=10)
    assert all(r.doc_id != doc_id for r in post)
    assert await cache.get(key) is None
    hist = await sessions.history("nordfels", "anna", session_id)
    assert hist and hist[-1]["content"] == REDACTED
    # corpus_version bumped ⇒ even a re-computed key would differ
    assert await store.corpus_version(ANNA_IT, "handbuecher") == corpus_v + 1


async def test_rls_department_scoping(stack):
    store, retriever, _, _, deleter, embedder = stack
    doc_id = f"hr-{uuid.uuid4().hex[:8]}"
    await _ingest(store, embedder, BEN_HR, "hr", doc_id, "Gehaltsbänder", HR_DOC, "hr")

    qv = (await embedder.embed(["Gehaltsbänder vertraulich"]))[0]
    hr_hits = await retriever.search(BEN_HR, "hr", "Gehaltsbänder", qv, top_k=10)
    it_hits = await retriever.search(ANNA_IT, "hr", "Gehaltsbänder", qv, top_k=10)
    assert any(r.doc_id == doc_id for r in hr_hits), "HR user must see the HR document"
    assert all(r.doc_id != doc_id for r in it_hits), "IT user must NOT see the HR document"

    await deleter.delete_document(BEN_HR, doc_id)


async def test_delete_of_invisible_doc_is_noop_and_skips_cascade(stack):
    """Ben must not be able to delete (or cascade-purge) a document RLS hides
    from him. The store returns found=False and the cascade is skipped."""
    store, retriever, cache, sessions, deleter, embedder = stack
    doc_id = f"it-{uuid.uuid4().hex[:8]}"
    # Anna owns an IT-only document; Ben (HR) cannot see it.
    await _ingest(store, embedder, ANNA_IT, "handbuecher", doc_id, "VPN-Handbuch", VPN_DOC, "it")
    # plant a cache entry + session message as if they existed for this doc
    key = build_answer_cache_key(BEN_HR, DataClass.INTERNAL, "x", 1, 1)
    await cache.set(key, {"answer": "geheim [S1]."}, [doc_id])
    await sessions.append("nordfels", "anna", "s-it", "assistant", "geheim [S1].", [doc_id])
    try:
        report = await deleter.delete_document(BEN_HR, doc_id)
        assert report.found is False
        assert report.chunks_deleted == 0
        assert report.cache_entries_purged == 0
        assert report.session_messages_redacted == 0
        # Ben's delete left Anna's cache/session untouched
        assert await cache.get(key) is not None
        hist = await sessions.history("nordfels", "anna", "s-it")
        assert hist and hist[-1]["content"] != REDACTED
    finally:
        await deleter.delete_document(ANNA_IT, doc_id)  # Anna cleans up (she can see it)
        await cache.purge_by_doc(doc_id)


async def test_unset_context_sees_nothing_fail_closed(stack):
    """A request that forgot SET LOCAL matches no rows — RLS fails closed."""
    store, retriever, _, _, deleter, embedder = stack
    doc_id = f"fc-{uuid.uuid4().hex[:8]}"
    await _ingest(store, embedder, ANNA_IT, "handbuecher", doc_id, "VPN-Handbuch", VPN_DOC, "all")
    try:
        async with retriever.backend.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM chunks WHERE doc_id = %s", (doc_id,))
            assert (await cur.fetchone())[0] == 0
    finally:
        await deleter.delete_document(ANNA_IT, doc_id)


async def test_idempotent_ingest_and_atomic_regeneration(stack):
    store, retriever, _, _, deleter, embedder = stack
    doc_id = f"idem-{uuid.uuid4().hex[:8]}"

    s1 = await _ingest(
        store, embedder, ANNA_IT, "handbuecher", doc_id, "VPN-Handbuch", VPN_DOC, "all"
    )
    v1 = await store.corpus_version(ANNA_IT, "handbuecher")
    s2 = await _ingest(
        store, embedder, ANNA_IT, "handbuecher", doc_id, "VPN-Handbuch", VPN_DOC, "all"
    )
    assert (s1, s2) == ("created", "unchanged")
    assert await store.corpus_version(ANNA_IT, "handbuecher") == v1  # no-op didn't bump

    s3 = await _ingest(
        store,
        embedder,
        ANNA_IT,
        "handbuecher",
        doc_id,
        "VPN-Handbuch",
        VPN_DOC + "\n\n## Neu\n\nZusätzlicher Abschnitt über Tokenverlängerung.",
        "all",
    )
    assert s3 == "updated"
    qv = (await embedder.embed(["Tokenverlängerung"]))[0]
    hits = await retriever.search(ANNA_IT, "handbuecher", "Tokenverlängerung", qv, top_k=10)
    assert any("Tokenverlängerung" in r.content for r in hits)

    await deleter.delete_document(ANNA_IT, doc_id)
