"""Deletion-cascade authorization guard (A2).

The cache and session stores are NOT RLS-protected — they sit outside the
database. If the DB delete found nothing (RLS hid the document, or it does not
exist), the cascade MUST NOT purge cache entries or redact session messages:
doc_ids are guessable slugs, so that would let any user tamper with data they
cannot see. These tests use in-memory fakes (no Postgres/Redis needed).
"""

import logging

from rag_assistant.deletion import DeletionCascade
from rag_assistant.domain import DeletionReport, QueryScope
from rag_assistant.testing.fakes import FailingAuditLog, FakeAuditLog

SCOPE = QueryScope(tenant="nordfels", user_id="ben", department="hr")


class FakeStore:
    def __init__(self, report: DeletionReport):
        self._report = report
        self.user_feedback_deleted: str | None = None

    async def delete_document(self, scope, doc_id):
        return self._report

    async def delete_user_feedback(self, scope, user_id):
        self.user_feedback_deleted = user_id
        return 4


class SpyCache:
    def __init__(self):
        self.purged: list[str] = []

    async def purge_by_doc(self, doc_id: str) -> int:
        self.purged.append(doc_id)
        return 3


class SpySessions:
    def __init__(self):
        self.redacted: list[str] = []
        self.user_sessions_deleted: tuple[str, str] | None = None

    async def redact_doc(self, doc_id: str) -> int:
        self.redacted.append(doc_id)
        return 2

    async def delete_user_sessions(self, tenant: str, user_id: str) -> int:
        self.user_sessions_deleted = (tenant, user_id)
        return 2


class SpyAuthSessions:
    def __init__(self):
        self.swept: tuple[str, str] | None = None

    async def delete_user_auth_sessions(self, tenant: str, user_id: str) -> int:
        self.swept = (tenant, user_id)
        return 3


async def test_user_self_service_is_bound_to_the_callers_scope():
    """delete_user_data always passes scope.user_id down — the API surface has
    no way to name a foreign user. The permission-scoped answer cache carries
    no user identifier, so it is deliberately NOT purged."""
    cache, sessions = SpyCache(), SpySessions()
    store = FakeStore(DeletionReport(doc_id="x"))
    deleter = DeletionCascade(store, cache, sessions)

    report = await deleter.delete_user_data(SCOPE)

    assert report.user_id == "ben"
    assert report.feedback_rows_deleted == 4
    assert report.sessions_deleted == 2
    assert report.auth_sessions_deleted == 0  # no auth-session store wired
    assert store.user_feedback_deleted == "ben"
    assert sessions.user_sessions_deleted == ("nordfels", "ben")
    assert cache.purged == []


async def test_user_self_service_sweeps_the_callers_auth_sessions():
    """A6: the caller's BFF auth sessions die with their trail — the sweep is
    scope-bound exactly like feedback and chat sessions."""
    auth_sessions = SpyAuthSessions()
    deleter = DeletionCascade(
        FakeStore(DeletionReport(doc_id="x")),
        SpyCache(),
        SpySessions(),
        auth_sessions=auth_sessions,
    )

    report = await deleter.delete_user_data(SCOPE)

    assert report.auth_sessions_deleted == 3
    assert auth_sessions.swept == ("nordfels", "ben")


async def test_cascade_skipped_when_document_not_visible():
    """RLS hid the doc → store returns found=False → cache/sessions untouched."""
    cache, sessions = SpyCache(), SpySessions()
    deleter = DeletionCascade(FakeStore(DeletionReport(doc_id="secret")), cache, sessions)

    report = await deleter.delete_document(SCOPE, "secret")

    assert report.found is False
    assert report.cache_entries_purged == 0
    assert report.session_messages_redacted == 0
    assert cache.purged == []  # NOT called — this is the authz guard
    assert sessions.redacted == []


async def test_cascade_runs_when_document_found():
    cache, sessions = SpyCache(), SpySessions()
    found = DeletionReport(
        doc_id="vpn-handbuch", found=True, chunks_deleted=5, feedback_rows_deleted=1
    )
    deleter = DeletionCascade(FakeStore(found), cache, sessions)

    report = await deleter.delete_document(SCOPE, "vpn-handbuch")

    assert report.found is True
    assert report.chunks_deleted == 5
    assert report.cache_entries_purged == 3
    assert report.session_messages_redacted == 2
    assert cache.purged == ["vpn-handbuch"]
    assert sessions.redacted == ["vpn-handbuch"]


# ── deletion receipts (A4) ────────────────────────────────────────────────────
def _found_report() -> DeletionReport:
    return DeletionReport(
        doc_id="vpn-handbuch",
        found=True,
        chunks_deleted=5,
        feedback_rows_deleted=1,
        audit_ref="surrogate-ref",
    )


async def test_document_cascade_writes_a_content_free_receipt():
    audit = FakeAuditLog()
    deleter = DeletionCascade(FakeStore(_found_report()), SpyCache(), SpySessions(), audit=audit)

    await deleter.delete_document(SCOPE, "vpn-handbuch")

    [event] = audit.events
    assert (event.event_type, event.decision) == ("delete", "executed")
    assert event.doc_refs == ("surrogate-ref",)  # the surrogate, never the slug
    assert (event.chunks_deleted, event.cache_purged) == (5, 3)
    assert (event.sessions_redacted, event.feedback_deleted) == (2, 1)
    assert "vpn-handbuch" not in (event.collection or "") + "".join(event.doc_refs)


async def test_user_self_service_writes_a_receipt():
    audit = FakeAuditLog()
    deleter = DeletionCascade(FakeStore(_found_report()), SpyCache(), SpySessions(), audit=audit)

    await deleter.delete_user_data(SCOPE)

    [event] = audit.events
    assert (event.event_type, event.decision) == ("delete", "executed")
    assert event.feedback_deleted == 4
    assert event.sessions_redacted == 2  # column counts REMOVED sessions here
    assert event.doc_refs == ()


async def test_no_receipt_when_the_document_was_not_visible():
    audit = FakeAuditLog()
    deleter = DeletionCascade(
        FakeStore(DeletionReport(doc_id="secret")), SpyCache(), SpySessions(), audit=audit
    )

    await deleter.delete_document(SCOPE, "secret")

    assert audit.events == []  # nothing was deleted → nothing to attest


async def test_receipt_failure_never_aborts_the_deletion(caplog):
    """Deletion precedence: a broken audit backend must not block the deletion."""
    deleter = DeletionCascade(
        FakeStore(_found_report()), SpyCache(), SpySessions(), audit=FailingAuditLog()
    )

    with caplog.at_level(logging.ERROR, logger="rag_assistant.deletion"):
        report = await deleter.delete_document(SCOPE, "vpn-handbuch")

    assert report.found is True and report.chunks_deleted == 5
    assert any("receipt write failed" in r.message for r in caplog.records)
