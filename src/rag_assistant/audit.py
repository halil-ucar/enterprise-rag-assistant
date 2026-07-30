"""Audit trail event model (A4) — access METADATA only, never content.

The event is a frozen dataclass whose field inventory is unit-pinned: every
future field addition forces a deliberate review against the "no content
field" rule. event_type/decision/data_class are closed sets, kept identical
to the CHECK constraints in db/init/04_audit.sql (a unit test parses the SQL
file and pins the equality — code/schema drift fails fast).

Builders are pure functions, one per event type. Deliberate signatures:
- build_ingest_event carries NO doc_id/title (speaking values stay out);
- build_rate_limited does NOT store the route class (the event type is enough);
- build_auth_failure has NO parameter at all — the presented key structurally
  cannot reach this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .domain import QueryScope


class AuditWriteError(Exception):
    """A CRITICAL audit write failed (fail-closed): the confidential access
    event could not be persisted, so the stream must abort before any token."""


EVENT_TYPES: frozenset[str] = frozenset(
    {"query", "ingest", "delete", "policy_denial", "rate_limited", "auth_failure"}
)
DECISIONS: frozenset[str] = frozenset(
    {"context_served", "no_context", "accepted", "executed", "denied", "limited", "failed"}
)
DATA_CLASSES: frozenset[str] = frozenset({"public", "internal", "confidential"})

_COUNT_KEYS = ("chunks_deleted", "cache_purged", "sessions_redacted", "feedback_deleted")


@dataclass(frozen=True)
class AuditEvent:
    tenant: str | None
    user_id: str | None
    department: str | None
    event_type: str
    collection: str | None
    data_class: str | None
    decision: str
    doc_refs: tuple[str, ...] = ()
    request_id: str | None = None
    chunks_deleted: int | None = None
    cache_purged: int | None = None
    sessions_redacted: int | None = None
    feedback_deleted: int | None = None

    def __post_init__(self) -> None:
        # Closed sets, validated on EVERY construction — no silent admission of
        # values the DB CHECK constraints would reject anyway.
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown audit event_type '{self.event_type}'")
        if self.decision not in DECISIONS:
            raise ValueError(f"unknown audit decision '{self.decision}'")
        if self.data_class is not None and self.data_class not in DATA_CLASSES:
            raise ValueError(f"unknown audit data_class '{self.data_class}'")


def build_query_event(
    scope: QueryScope,
    collection: str,
    data_class: str,
    doc_refs: tuple[str, ...],
    served: bool,
    request_id: str | None,
) -> AuditEvent:
    """Access record for /query: WHO got WHICH documents (as surrogates) into
    the answer context — or an honest 'no_context'. Answer QUALITY is
    telemetry (trace/eval), not an access fact, and stays out of the audit."""
    return AuditEvent(
        tenant=scope.tenant,
        user_id=scope.user_id,
        department=scope.department,
        event_type="query",
        collection=collection,
        data_class=data_class,
        decision="context_served" if served else "no_context",
        doc_refs=tuple(doc_refs),
        request_id=request_id,
    )


def build_ingest_event(scope: QueryScope, collection: str, request_id: str | None) -> AuditEvent:
    """Accepted ingest — deliberately WITHOUT doc_id/title (speaking values)."""
    return AuditEvent(
        tenant=scope.tenant,
        user_id=scope.user_id,
        department=scope.department,
        event_type="ingest",
        collection=collection,
        data_class=None,
        decision="accepted",
        request_id=request_id,
    )


def build_delete_receipt(
    scope: QueryScope,
    audit_ref: str | None,
    counts: Mapping[str, int | None],
    request_id: str | None,
) -> AuditEvent:
    """Content-free deletion receipt for the document cascade: the surrogate
    plus counts. After the cascade the surrogate resolves to nothing — the
    receipt stays evidential, the reference is dead (E3)."""
    unknown = set(counts) - set(_COUNT_KEYS)
    if unknown:
        raise ValueError(f"unknown deletion count keys: {sorted(unknown)}")
    return AuditEvent(
        tenant=scope.tenant,
        user_id=scope.user_id,
        department=scope.department,
        event_type="delete",
        collection=None,
        data_class=None,
        decision="executed",
        doc_refs=(audit_ref,) if audit_ref else (),
        request_id=request_id,
        chunks_deleted=counts.get("chunks_deleted"),
        cache_purged=counts.get("cache_purged"),
        sessions_redacted=counts.get("sessions_redacted"),
        feedback_deleted=counts.get("feedback_deleted"),
    )


def build_user_delete_receipt(
    scope: QueryScope, feedback_deleted: int, sessions_deleted: int
) -> AuditEvent:
    """Receipt for self-service deletion (DELETE /me/data). Column semantics:
    sessions_redacted here counts REMOVED sessions (the schema has one count
    column for session effects; the document cascade redacts, self-service
    deletes)."""
    return AuditEvent(
        tenant=scope.tenant,
        user_id=scope.user_id,
        department=scope.department,
        event_type="delete",
        collection=None,
        data_class=None,
        decision="executed",
        feedback_deleted=feedback_deleted,
        sessions_redacted=sessions_deleted,
    )


def build_policy_denial(scope: QueryScope, collection: str, data_class: str) -> AuditEvent:
    return AuditEvent(
        tenant=scope.tenant,
        user_id=scope.user_id,
        department=scope.department,
        event_type="policy_denial",
        collection=collection,
        data_class=data_class,
        decision="denied",
    )


def build_rate_limited(scope: QueryScope, route_class: str) -> AuditEvent:
    """route_class is accepted for call-site symmetry but NOT stored — the
    event type is enough (no extra columns for transient limiter detail)."""
    del route_class
    return AuditEvent(
        tenant=scope.tenant,
        user_id=scope.user_id,
        department=scope.department,
        event_type="rate_limited",
        collection=None,
        data_class=None,
        decision="limited",
    )


def build_auth_failure() -> AuditEvent:
    """No parameters BY DESIGN: an auth failure has no valid scope, and the
    presented key must never be able to reach the audit trail."""
    return AuditEvent(
        tenant=None,
        user_id=None,
        department=None,
        event_type="auth_failure",
        collection=None,
        data_class=None,
        decision="denied",
    )
