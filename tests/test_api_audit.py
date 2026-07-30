"""Endpoint-level unit tests for the audit write path (A4.3).

No services: the app state is assembled by hand (fake store/cache/sessions,
in-memory retriever, scripted FakeLLM) and requests go through httpx's ASGI
transport without the lifespan. Under test:

- the /query access event exists on all three routes with surrogate refs,
- fail-closed: a broken writer + AUDIT_FAIL_CLOSED + confidential aborts the
  stream with an error frame and NOT ONE token frame (this also proves the
  write happens before the first token),
- non-critical failures never break the answer,
- auth_failure / rate_limited / ingest / policy_denial / failed-before-context
  hooks, and AUDIT_ENABLED=false as the demo off switch.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import rag_assistant.api as api_mod
from conftest import make_settings
from rag_assistant.config import Registry
from rag_assistant.domain import CollectionCfg, DataClass
from rag_assistant.llm.registry import ProviderRegistry
from rag_assistant.obs import Metrics
from rag_assistant.pipeline import RagPipeline
from rag_assistant.ratelimit import RedisRateLimiter
from rag_assistant.testing.fakes import (
    FailingAuditLog,
    FakeAuditLog,
    FakeEmbedder,
    FakeLLM,
    FakeRateLimitStore,
    FakeReranker,
    InMemoryRetriever,
)

ANNA = {"X-API-Key": "demo-anna-it"}
BEN = {"X-API-Key": "demo-ben-hr"}

CONDENSE_DIRECT = '{"standalone_query": "vpn fehler", "route": "direct"}'
CONDENSE_AGENTIC = '{"standalone_query": "vpn fehler", "route": "agentic"}'

VPN_MD = "# VPN\n\n## Fehler\n\nDer Fehlercode NF-4102 bedeutet: Zertifikat abgelaufen."


def _registry() -> Registry:
    return Registry(
        default_tenant="nordfels",
        default_collection="handbuecher",
        collections={
            "handbuecher": CollectionCfg(
                name="handbuecher", tenant="nordfels", data_class=DataClass.INTERNAL
            ),
            "hr": CollectionCfg(name="hr", tenant="nordfels", data_class=DataClass.CONFIDENTIAL),
            "archiv": CollectionCfg(
                name="archiv",
                tenant="nordfels",
                data_class=DataClass.INTERNAL,
                generation="extractive",
            ),
        },
    )


class FakeQueryStore:
    """The store subset these endpoints touch, plus the A4 surrogate lookup."""

    async def corpus_version(self, scope, collection) -> int:
        return 1

    async def audit_refs_for(self, scope, doc_ids) -> list[str]:
        return [f"ref-{d}" for d in doc_ids]

    async def add_feedback(self, *args, **kwargs) -> None:
        return None


class NullCache:
    async def get(self, key):
        return None

    async def set(self, key, value, doc_ids):
        return None


class NullSessions:
    async def history(self, *args):
        return []

    async def append(self, *args):
        return None


class FakeArq:
    class _Job:
        job_id = "job-1"

    async def enqueue_job(self, *args, **kwargs):
        return self._Job()


@pytest.fixture
def app_state(monkeypatch):
    """Assemble the full /query state with fakes; returns (setup, audit_log)."""

    async def _seed(retriever: InMemoryRetriever) -> None:
        from rag_assistant.chunking import chunk_markdown

        chunks = chunk_markdown(VPN_MD, "VPN-Handbuch")
        for coll in ("handbuecher", "hr", "archiv"):
            await retriever.add(coll, "vpn-doc", "VPN-Handbuch", "all", chunks)

    def _setup(llm: FakeLLM | None = None, audit=None, **overrides):
        values: dict = {
            "deployment_mode": "demo",
            "api_key_anna": "demo-anna-it",
            "api_key_ben": "demo-ben-hr",
            "rate_limit_enabled": False,
        }
        values.update(overrides)
        s = make_settings(**values)
        embedder = FakeEmbedder()
        retriever = InMemoryRetriever(embedder)
        llm = llm if llm is not None else FakeLLM("ollama")
        providers = ProviderRegistry(s, providers={"ollama": llm})
        pipeline = RagPipeline(providers, retriever, embedder, FakeReranker(), s)
        audit = audit if audit is not None else FakeAuditLog()
        monkeypatch.setattr(api_mod.state, "settings", s, raising=False)
        monkeypatch.setattr(api_mod.state, "registry", _registry(), raising=False)
        monkeypatch.setattr(api_mod.state, "providers", providers, raising=False)
        monkeypatch.setattr(api_mod.state, "pipeline", pipeline, raising=False)
        monkeypatch.setattr(api_mod.state, "store", FakeQueryStore(), raising=False)
        monkeypatch.setattr(api_mod.state, "cache", NullCache(), raising=False)
        monkeypatch.setattr(api_mod.state, "sessions", NullSessions(), raising=False)
        monkeypatch.setattr(api_mod.state, "audit", audit, raising=False)
        monkeypatch.setattr(api_mod.state, "metrics", Metrics(), raising=False)
        monkeypatch.setattr(api_mod.state, "arq", FakeArq(), raising=False)
        monkeypatch.setattr(
            api_mod.state, "ratelimiter", RedisRateLimiter(FakeRateLimitStore()), raising=False
        )
        return retriever, audit, llm

    _setup.seed = _seed  # type: ignore[attr-defined]
    return _setup


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_mod.app), base_url="http://test")


def frames(body: str) -> list[tuple[str, dict | list]]:
    out: list[tuple[str, dict | list]] = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


async def _drain() -> None:
    """Let fire-and-forget audit tasks run to completion."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _query(payload: dict, headers: dict = ANNA) -> httpx.Response:
    async with client() as c:
        r = await c.post("/query", json=payload, headers=headers)
    await _drain()
    return r


# ── access event on all three routes ──────────────────────────────────────────
async def test_direct_route_writes_the_access_event(app_state):
    retriever, audit, llm = app_state()
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    r = await _query({"question": "Was bedeutet NF-4102?"})

    assert any(e == "token" for e, _ in frames(r.text))
    [event] = audit.events
    assert (event.event_type, event.decision) == ("query", "context_served")
    assert event.doc_refs == ("ref-vpn-doc",)  # surrogate, not the slug
    assert (event.collection, event.data_class) == ("handbuecher", "internal")
    assert len(event.request_id or "") == 32


async def test_agentic_route_writes_the_access_event(app_state):
    retriever, audit, llm = app_state()
    await app_state.seed(retriever)
    llm.queue(
        CONDENSE_AGENTIC,
        '{"sufficient": true}',
        "Antwort [S1].",
        '{"grounded": true}',
    )

    r = await _query({"question": "Was bedeutet NF-4102?"})

    assert any(e == "token" for e, _ in frames(r.text))
    [event] = audit.events
    assert (event.event_type, event.decision) == ("query", "context_served")
    assert event.doc_refs == ("ref-vpn-doc",)


async def test_extractive_route_writes_the_access_event_without_llm(app_state):
    retriever, audit, llm = app_state()
    await app_state.seed(retriever)

    r = await _query({"question": "Was bedeutet NF-4102?", "collection": "archiv"})

    assert any(e == "token" for e, _ in frames(r.text))
    [event] = audit.events
    assert (event.event_type, event.decision) == ("query", "context_served")
    assert llm.calls == []  # extractive: no LLM saw anything


async def test_empty_retrieval_records_an_honest_no_context(app_state):
    _, audit, llm = app_state()  # no documents seeded
    llm.queue(CONDENSE_DIRECT, "Antwort.")

    await _query({"question": "Was bedeutet NF-4102?"})

    [event] = audit.events
    assert event.decision == "no_context"
    assert event.doc_refs == ()


# ── fail-closed vs non-critical ───────────────────────────────────────────────
async def test_fail_closed_confidential_aborts_before_any_token(app_state):
    """audit_fail_closed + confidential + broken writer → error frame, and NOT
    ONE token frame: the write point provably sits before the stream."""
    retriever, _, llm = app_state(audit=FailingAuditLog(), audit_fail_closed=True)
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    r = await _query({"question": "Gehaltsband E3?", "collection": "hr"}, headers=BEN)

    fs = frames(r.text)
    assert all(e != "token" for e, _ in fs)
    errors = [d for e, d in fs if e == "error"]
    assert errors and "Zugriffsprotokollierung" in errors[0]["message"]  # type: ignore[index]


async def test_broken_writer_is_non_critical_for_internal(app_state, caplog):
    """Same broken writer, internal collection: the answer streams, the
    failure is logged — availability is not sacrificed below confidential."""
    import logging

    retriever, _, llm = app_state(audit=FailingAuditLog(), audit_fail_closed=True)
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    with caplog.at_level(logging.ERROR, logger="rag_assistant.api"):
        r = await _query({"question": "Was bedeutet NF-4102?"})

    assert any(e == "token" for e, _ in frames(r.text))
    assert any("audit write failed" in rec.message for rec in caplog.records)
    registry = api_mod.state.metrics.registry
    assert (registry.get_sample_value("rag_audit_write_failures_total") or 0) >= 1


async def test_confidential_without_fail_closed_setting_streams_through(app_state):
    """Demo default (audit_fail_closed=false): even confidential access is a
    non-critical write — the R7 gate, not runtime code, forces the flag on
    for production (E2)."""
    retriever, _, llm = app_state(audit=FailingAuditLog(), audit_fail_closed=False)
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    r = await _query({"question": "Gehaltsband E3?", "collection": "hr"}, headers=BEN)

    assert any(e == "token" for e, _ in frames(r.text))


async def test_audit_disabled_writes_nothing(app_state):
    retriever, audit, llm = app_state(audit_enabled=False)
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    r = await _query({"question": "Was bedeutet NF-4102?"})

    assert any(e == "token" for e, _ in frames(r.text))
    assert audit.events == []


# ── error paths ───────────────────────────────────────────────────────────────
class BrokenLLM(FakeLLM):
    async def complete(self, messages, **kwargs):  # type: ignore[override]
        raise RuntimeError("provider down")


async def test_failure_before_context_records_a_failed_event(app_state):
    retriever, audit, _ = app_state(llm=BrokenLLM("ollama"))
    await app_state.seed(retriever)

    r = await _query({"question": "Was bedeutet NF-4102?"})

    assert any(e == "error" for e, _ in frames(r.text))
    [event] = audit.events
    assert (event.event_type, event.decision) == ("query", "failed")
    assert event.doc_refs == ()


async def test_policy_denial_is_recorded(app_state):
    # No provider registered at all → PermissionError from the policy chain.
    retriever, audit, _ = app_state()
    await app_state.seed(retriever)
    api_mod.state.providers.providers.clear()
    api_mod.state.providers.chains = {"mini": [], "strong": [], "local": []}

    r = await _query({"question": "Gehaltsband E3?", "collection": "hr"}, headers=BEN)

    assert any(e == "error" for e, _ in frames(r.text))
    [event] = audit.events
    assert (event.event_type, event.decision) == ("policy_denial", "denied")
    assert (event.collection, event.data_class) == ("hr", "confidential")


# ── auth / limiter / ingest hooks ─────────────────────────────────────────────
async def test_auth_failure_event_carries_no_scope_and_no_key(app_state):
    _, audit, _ = app_state()
    presented = "sk-super-secret-attacker-key"

    async with client() as c:
        r = await c.get("/documents", headers={"X-API-Key": presented})
    await _drain()

    assert r.status_code == 401
    [event] = audit.events
    assert (event.event_type, event.decision) == ("auth_failure", "denied")
    assert event.tenant is None and event.user_id is None and event.department is None
    assert presented not in "".join(str(v) for v in vars(event).values())


async def test_rate_limited_event_is_written_on_429(app_state):
    _, audit, llm = app_state(
        rate_limit_enabled=True, rate_limit_query_burst=1, rate_limit_query_per_min=1
    )

    async with client() as c:
        first = await c.post(
            "/feedback", json={"rating": 1, "condensed_question": "q"}, headers=ANNA
        )
        second = await c.post(
            "/feedback", json={"rating": 1, "condensed_question": "q"}, headers=ANNA
        )
    await _drain()

    assert (first.status_code, second.status_code) == (200, 429)
    limited = [e for e in audit.events if e.event_type == "rate_limited"]
    assert limited and limited[0].decision == "limited"
    assert limited[0].user_id == "anna"


async def test_ingest_event_has_no_speaking_values(app_state):
    _, audit, _ = app_state()

    async with client() as c:
        r = await c.post(
            "/ingest",
            json={"doc_id": "geheim-doc", "title": "Geheimer Titel", "content_text": "Inhalt"},
            headers=ANNA,
        )
    await _drain()

    assert r.status_code == 200
    [event] = audit.events
    assert (event.event_type, event.decision) == ("ingest", "accepted")
    assert event.collection == "handbuecher"
    dump = "".join(str(v) for v in vars(event).values())
    assert "geheim-doc" not in dump and "Geheimer Titel" not in dump and "Inhalt" not in dump


# ── marker hygiene at the unit level ──────────────────────────────────────────
async def test_question_marker_never_reaches_any_event_field(app_state):
    marker = "MARKER-a9f3e2"
    retriever, audit, llm = app_state()
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    await _query({"question": f"Was bedeutet {marker}?"})

    assert audit.events
    for event in audit.events:
        assert marker not in "".join(str(v) for v in vars(event).values())
