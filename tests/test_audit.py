"""Unit tests for the audit event model (A4.2).

The point of these tests is CONTAINMENT, not coverage: the field inventory is
pinned (any new field forces a deliberate review against the "no content
field" rule), the closed value sets are pinned against the SQL CHECK
constraints (code/schema drift fails as a unit test), and the auth_failure
builder is proven to have no way of receiving the presented credential.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from rag_assistant.audit import (
    DECISIONS,
    EVENT_TYPES,
    AuditEvent,
    build_auth_failure,
    build_delete_receipt,
    build_ingest_event,
    build_policy_denial,
    build_query_event,
    build_rate_limited,
    build_user_delete_receipt,
)
from rag_assistant.domain import QueryScope

SCOPE = QueryScope(tenant="nordfels", user_id="anna", department="it")

AUDIT_SQL = Path(__file__).resolve().parent.parent / "db" / "init" / "04_audit.sql"


def sql_check_set(column: str) -> frozenset[str]:
    """Extract the closed CHECK set for a column from db/init/04_audit.sql."""
    text = AUDIT_SQL.read_text(encoding="utf-8")
    match = re.search(rf"CHECK \({column} IN\s*\(([^)]*)\)", text)
    assert match, f"no CHECK set for column '{column}' found in {AUDIT_SQL.name}"
    return frozenset(re.findall(r"'([a-z_]+)'", match.group(1)))


# ── field inventory pin ───────────────────────────────────────────────────────
def test_field_inventory_is_pinned():
    """Negative list 'no content field': adding ANY field to AuditEvent must
    fail here first and get a deliberate review."""
    assert set(AuditEvent.__dataclass_fields__) == {
        "tenant",
        "user_id",
        "department",
        "event_type",
        "collection",
        "data_class",
        "decision",
        "doc_refs",
        "request_id",
        "chunks_deleted",
        "cache_purged",
        "sessions_redacted",
        "feedback_deleted",
    }


# ── closed sets, identical to the SQL CHECK constraints ───────────────────────
def test_event_types_match_the_sql_check_set():
    assert sql_check_set("event_type") == EVENT_TYPES


def test_decisions_match_the_sql_check_set():
    assert sql_check_set("decision") == DECISIONS


# ── validation: no silent admission ───────────────────────────────────────────
def test_unknown_event_type_is_rejected():
    with pytest.raises(ValueError, match="event_type"):
        AuditEvent(
            tenant=None,
            user_id=None,
            department=None,
            event_type="telemetry",
            collection=None,
            data_class=None,
            decision="denied",
        )


def test_unknown_decision_is_rejected():
    with pytest.raises(ValueError, match="decision"):
        AuditEvent(
            tenant=None,
            user_id=None,
            department=None,
            event_type="query",
            collection=None,
            data_class=None,
            decision="answered",
        )


def test_builder_rejects_unknown_data_class():
    with pytest.raises(ValueError, match="data_class"):
        build_query_event(SCOPE, "hr", "secret", (), served=True, request_id=None)


def test_delete_receipt_rejects_unknown_count_keys():
    with pytest.raises(ValueError, match="count keys"):
        build_delete_receipt(SCOPE, "ref", {"question_text": 1}, None)


# ── builder semantics ─────────────────────────────────────────────────────────
def test_query_event_records_access_facts():
    served = build_query_event(
        SCOPE, "handbuecher", "internal", ("ref-1", "ref-2"), served=True, request_id="abc"
    )
    assert served.event_type == "query"
    assert served.decision == "context_served"
    assert served.doc_refs == ("ref-1", "ref-2")
    assert served.request_id == "abc"
    empty = build_query_event(SCOPE, "handbuecher", "internal", (), served=False, request_id="abc")
    assert empty.decision == "no_context"


def test_ingest_event_carries_no_speaking_values():
    event = build_ingest_event(SCOPE, "handbuecher", "abc")
    assert event.event_type == "ingest"
    assert event.decision == "accepted"
    assert event.doc_refs == ()


def test_delete_receipt_carries_surrogate_and_counts():
    event = build_delete_receipt(
        SCOPE,
        "surrogate-uuid",
        {"chunks_deleted": 5, "cache_purged": 1, "sessions_redacted": 2, "feedback_deleted": 3},
        "abc",
    )
    assert event.event_type == "delete"
    assert event.decision == "executed"
    assert event.doc_refs == ("surrogate-uuid",)
    assert (event.chunks_deleted, event.cache_purged) == (5, 1)
    assert (event.sessions_redacted, event.feedback_deleted) == (2, 3)
    # a cascade whose surrogate is unknown still yields a receipt, without refs
    assert build_delete_receipt(SCOPE, None, {}, None).doc_refs == ()


def test_user_delete_receipt_maps_sessions_to_the_redacted_column():
    event = build_user_delete_receipt(SCOPE, feedback_deleted=4, sessions_deleted=2)
    assert event.event_type == "delete"
    assert event.decision == "executed"
    assert event.feedback_deleted == 4
    assert event.sessions_redacted == 2  # column counts REMOVED sessions here
    assert event.doc_refs == ()


def test_policy_denial_and_rate_limited_decisions():
    denial = build_policy_denial(SCOPE, "hr", "confidential")
    assert (denial.event_type, denial.decision) == ("policy_denial", "denied")
    limited = build_rate_limited(SCOPE, "query")
    assert (limited.event_type, limited.decision) == ("rate_limited", "limited")


def test_rate_limited_does_not_store_the_route_class():
    """The route class is transient limiter detail — the event type is enough."""
    event = build_rate_limited(SCOPE, "query")
    assert "query" not in {event.collection, event.request_id}
    assert event.doc_refs == ()


# ── auth_failure: the presented key structurally cannot arrive ────────────────
def test_auth_failure_builder_has_no_credential_parameter():
    assert list(inspect.signature(build_auth_failure).parameters) == []
    event = build_auth_failure()
    assert event.tenant is None and event.user_id is None and event.department is None
    assert (event.event_type, event.decision) == ("auth_failure", "denied")
