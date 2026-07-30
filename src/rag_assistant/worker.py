"""arq ingestion worker: parse → chunk → embed → upsert (idempotent).

Async queue + worker is the enterprise ingestion contract: POST /ingest
returns a job id immediately; this worker does the heavy lifting with
retries. The contract survives scaling — only broker/worker count changes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from arq.connections import RedisSettings
from arq.cron import cron

from .chunking import chunk_markdown
from .config import get_registry, get_settings
from .domain import QueryScope
from .embeddings import build_embedder
from .obs import setup_logging
from .parsing import parse_to_markdown
from .store.audit import PgAuditLog
from .store.pg import PgBackend, PgStore

log = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_json)
    backend = PgBackend(settings.database_url, rrf_k=settings.rrf_k)
    await backend.open()
    ctx["backend"] = backend
    ctx["store"] = PgStore(backend)
    ctx["audit"] = PgAuditLog(backend)
    ctx["embedder"] = build_embedder(settings)  # loaded ONCE per worker process
    ctx["registry"] = get_registry()
    log.info("worker ready (embedder=%s)", type(ctx["embedder"]).__name__)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["backend"].close()


async def ingest_document(
    ctx: dict[str, Any],
    *,
    doc_id: str,
    title: str,
    collection: str,
    department: str,
    content_b64: str,
    fmt: str,
    tenant: str,
    user_id: str,
) -> dict[str, Any]:
    """Returns a small JSON-able report (job status endpoint surfaces it)."""
    data = base64.b64decode(content_b64)
    content_hash = hashlib.sha256(data).hexdigest()
    scope = QueryScope(tenant=tenant, user_id=user_id, department=department)

    # Skip parsing/chunking/embedding entirely when the document is unchanged —
    # embedding is the CPU-expensive step and a re-seed of an unchanged corpus
    # (make refresh) would otherwise pay it for every document.
    if await ctx["store"].content_hash(scope, doc_id) == content_hash:
        return {"doc_id": doc_id, "status": "unchanged", "chunks": 0}

    markdown = parse_to_markdown(data, fmt)
    chunks = chunk_markdown(markdown, title)
    if not chunks:
        return {"doc_id": doc_id, "status": "empty", "chunks": 0}

    embedder = ctx["embedder"]
    vectors = await embedder.embed([c.content for c in chunks])

    registry = ctx["registry"]
    col = registry.get(collection)
    embedding_version = col.embedding_version if col else 1
    status = await ctx["store"].upsert_document(
        scope,
        collection,
        doc_id,
        title,
        content_hash,
        department,
        chunks,
        vectors,
        embedding_version,
    )
    log.info("ingested doc_id=%s status=%s chunks=%d", doc_id, status, len(chunks))
    return {"doc_id": doc_id, "status": status, "chunks": len(chunks)}


async def purge_expired_feedback(ctx: dict[str, Any]) -> dict[str, Any]:
    """Daily retention enforcement (deletion concept): purge feedback rows
    older than FEEDBACK_RETENTION_DAYS. Logs counts only, never content."""
    purged = await ctx["store"].purge_expired_feedback(get_settings().feedback_retention_days)
    if purged:
        log.info("retention: purged %d expired feedback row(s)", purged)
    return {"purged": purged}


async def purge_expired_audit(ctx: dict[str, Any]) -> dict[str, Any]:
    """Daily audit retention (A4): purge audit events older than
    AUDIT_RETENTION_DAYS. The SQL function enforces a 30-day floor regardless
    of the configured value. Logs counts only, never content."""
    purged = await ctx["audit"].purge_expired(get_settings().audit_retention_days)
    if purged:
        log.info("retention: purged %d expired audit event(s)", purged)
    return {"purged": purged}


class WorkerSettings:
    functions = [ingest_document]
    # Off-peak daily runs; the exact minutes are irrelevant, they just avoid :00 herds.
    cron_jobs = [
        cron(purge_expired_feedback, hour=3, minute=17),
        cron(purge_expired_audit, hour=3, minute=19),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = 3
    job_timeout = 600
