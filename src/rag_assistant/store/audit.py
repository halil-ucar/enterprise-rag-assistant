"""Postgres audit log adapter (A4).

Writes go through a PLAIN pool connection, deliberately OUTSIDE scoped_tx:
audit_events has no RLS — grants are the protection model (rag_app can only
INSERT; reading requires rag_audit_reader or rag_owner) — and auth_failure
events have no valid scope to set. purge_expired calls the SECURITY DEFINER
function, which enforces a 30-day floor server-side (defense in depth).
"""

from __future__ import annotations

from ..audit import AuditEvent
from .pg import PgBackend


class PgAuditLog:
    def __init__(self, backend: PgBackend):
        self.backend = backend

    async def write(self, event: AuditEvent) -> None:
        async with self.backend.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO audit_events
                    (tenant, user_id, department, event_type, collection, data_class,
                     decision, doc_refs, request_id,
                     chunks_deleted, cache_purged, sessions_redacted, feedback_deleted)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s, %s, %s, %s)
                """,
                (
                    event.tenant,
                    event.user_id,
                    event.department,
                    event.event_type,
                    event.collection,
                    event.data_class,
                    event.decision,
                    list(event.doc_refs),
                    event.request_id,
                    event.chunks_deleted,
                    event.cache_purged,
                    event.sessions_redacted,
                    event.feedback_deleted,
                ),
            )

    async def purge_expired(self, retention_days: int) -> int:
        async with self.backend.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT purge_expired_audit(make_interval(days => %s))",
                (retention_days,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0
