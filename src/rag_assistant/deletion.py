"""Deletion cascade — 'deleted' means EVERYWHERE.

Covers, in order:
  1. chunks + vectors (FK cascade; the vector still encodes the content)
  2. feedback rows citing the document
  3. cached answers that cited it (plus corpus_version bump ⇒ all stale keys die)
  4. session messages whose citation list contains it (deterministic via anchors)

The deletion log records ids/counts — never content.
"""

from __future__ import annotations

import logging

from .audit import AuditEvent, build_delete_receipt, build_user_delete_receipt
from .auth import AuthSessionStore
from .cache import AnswerCache
from .domain import DeletionReport, QueryScope, UserDataDeletionReport
from .ports import AuditLog
from .sessions import SessionStore
from .store.pg import PgStore

log = logging.getLogger(__name__)


class DeletionCascade:
    def __init__(
        self,
        store: PgStore,
        cache: AnswerCache,
        sessions: SessionStore,
        audit: AuditLog | None = None,
        auth_sessions: AuthSessionStore | None = None,
    ):
        self.store = store
        self.cache = cache
        self.sessions = sessions
        self.audit = audit
        self.auth_sessions = auth_sessions

    async def _write_receipt(self, event: AuditEvent) -> None:
        """A deletion NEVER fails on its receipt (deletion takes precedence
        over the audit duty — deliberate trade-off, documented in ARCHITECTURE.md).
        The failure is logged; the app's central write path counts it."""
        if self.audit is None:
            return
        try:
            await self.audit.write(event)
        except Exception as exc:  # noqa: BLE001 — receipt failure must never abort deletion
            log.error("deletion receipt write failed: %s", type(exc).__name__)

    async def delete_user_data(self, scope: QueryScope) -> UserDataDeletionReport:
        """SELF-SERVICE deletion: delete the requesting user's own trail —
        their feedback rows and sessions. Deliberately takes no foreign
        user_id: deleting on someone else's behalf is an operator process
        (deletion concept, docs/ARCHITECTURE.md), not an API surface. Corpus
        SUBJECTS (people the documents are about) are handled by the document
        cascade below. The answer cache needs no per-user purge: its key is
        permission-scoped (tenant + department) and never carries a user
        identifier (cachekey.py)."""
        report = UserDataDeletionReport(user_id=scope.user_id)
        report.feedback_rows_deleted = await self.store.delete_user_feedback(scope, scope.user_id)
        report.sessions_deleted = await self.sessions.delete_user_sessions(
            scope.tenant, scope.user_id
        )
        if self.auth_sessions is not None:
            # A6: the caller's BFF auth sessions die with their trail —
            # a logout-everywhere sweep over the exact user prefix.
            report.auth_sessions_deleted = await self.auth_sessions.delete_user_auth_sessions(
                scope.tenant, scope.user_id
            )
        log.info(
            "delete-user-data user_id=%s feedback=%d sessions=%d auth_sessions=%d",
            scope.user_id,
            report.feedback_rows_deleted,
            report.sessions_deleted,
            report.auth_sessions_deleted,
        )
        await self._write_receipt(
            build_user_delete_receipt(scope, report.feedback_rows_deleted, report.sessions_deleted)
        )
        return report

    async def delete_document(self, scope: QueryScope, doc_id: str) -> DeletionReport:
        report = await self.store.delete_document(scope, doc_id)
        if not report.found:
            # RLS hid the document (or it does not exist). Cache and sessions are
            # NOT RLS-protected — running the cascade here would let any user
            # purge cached answers and redact session messages for documents
            # they are not allowed to see (doc_ids are guessable slugs).
            log.info("delete-doc doc_id=%s not visible to scope — cascade skipped", doc_id)
            return report
        report.cache_entries_purged = await self.cache.purge_by_doc(doc_id)
        report.session_messages_redacted = await self.sessions.redact_doc(doc_id)
        log.info(
            "delete-doc doc_id=%s chunks=%d cache=%d sessions=%d feedback=%d",
            doc_id,
            report.chunks_deleted,
            report.cache_entries_purged,
            report.session_messages_redacted,
            report.feedback_rows_deleted,
        )
        # Content-free deletion receipt (A4/E3): the surrogate in doc_refs now
        # resolves to nothing — the receipt stays evidential, the reference is dead.
        await self._write_receipt(
            build_delete_receipt(
                scope,
                report.audit_ref,
                {
                    "chunks_deleted": report.chunks_deleted,
                    "cache_purged": report.cache_entries_purged,
                    "sessions_redacted": report.session_messages_redacted,
                    "feedback_deleted": report.feedback_rows_deleted,
                },
                None,
            )
        )
        return report
