"""Unit tests for /metrics (A5.1).

The three rules under test, beyond plain counting:
- label hygiene: no user_id, no tenant, no free text — a marker planted in a
  question must never surface in the exposition, and an unknown collection
  becomes the literal label 'unknown' (cardinality is never user-controlled);
- per-instance CollectorRegistry: two app constructions must not collide
  ("Duplicated timeseries" is the documented pytest failure mode of the
  library's global default registry);
- honest inventory: all eight metrics exist with exactly the pinned labels.
"""

from __future__ import annotations

import pytest

from rag_assistant.config import Settings
from rag_assistant.domain import DataClass
from rag_assistant.llm.registry import ProviderRegistry
from rag_assistant.obs import Metrics, route_class_of
from rag_assistant.ports import LLMMessage
from rag_assistant.testing.fakes import FakeLLM
from test_api_audit import (  # noqa: F401 — app_state is a fixture, found via module globals
    CONDENSE_DIRECT,
    app_state,
    client,
    frames,
)

MSG = [LLMMessage(role="user", content="hi")]


# ── pure route classifier ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/query", "query"),
        ("POST", "/feedback", "query"),
        ("POST", "/ingest", "ingest"),
        ("DELETE", "/documents/vpn-handbuch", "ingest"),
        ("GET", "/health", "free"),
        ("GET", "/metrics", "free"),
        ("GET", "/", "free"),
        ("GET", "/documents", "free"),
        ("DELETE", "/me/data", "free"),
    ],
)
def test_route_class_of(method, path, expected):
    assert route_class_of(method, path) == expected


# ── registry isolation ────────────────────────────────────────────────────────
def test_two_instances_do_not_collide():
    """Per-instance CollectorRegistry: a second construction must not raise
    'Duplicated timeseries' (it would with the global default REGISTRY)."""
    first, second = Metrics(), Metrics()
    first.requests_total.labels(route_class="query").inc()
    assert second.registry.get_sample_value("rag_requests_total", {"route_class": "query"}) in (
        None,
        0.0,
    )
    assert first.registry.get_sample_value("rag_requests_total", {"route_class": "query"}) == 1.0


# ── inventory + label pin ─────────────────────────────────────────────────────
EXPECTED_LABELS = {
    "rag_requests_total": ("route_class",),
    "rag_request_seconds": ("route_class",),
    "rag_provider_calls_total": ("provider", "kind", "outcome"),
    "rag_cache_events_total": ("result",),
    "rag_collection_queries_total": ("collection",),
    "rag_policy_denials_total": (),
    "rag_rate_limited_total": ("route_class",),
    "rag_audit_write_failures_total": (),
}


def test_metric_inventory_and_labels_are_pinned():
    """Every metric exists with EXACTLY these labels — never user_id, never
    tenant, never free text. Any addition fails here first."""
    m = Metrics()
    names = {
        # family.name lacks the _total suffix for counters; normalize back.
        family.name + ("_total" if family.type == "counter" else "")
        for family in m.registry.collect()
    }
    assert names == set(EXPECTED_LABELS)
    for attr, expected in [
        (m.requests_total, ("route_class",)),
        (m.request_seconds, ("route_class",)),
        (m.provider_calls, ("provider", "kind", "outcome")),
        (m.cache_events, ("result",)),
        (m.collection_queries, ("collection",)),
        (m.policy_denials, ()),
        (m.rate_limited, ("route_class",)),
        (m.audit_write_failures, ()),
    ]:
        assert tuple(attr._labelnames) == expected


# ── provider outcome counting in the registry ─────────────────────────────────
class FailingLLM(FakeLLM):
    async def complete(self, messages, **kwargs):  # type: ignore[override]
        raise RuntimeError("provider down")


def _providers(metrics: Metrics, failing_cloud: bool) -> ProviderRegistry:
    cloud: FakeLLM = FailingLLM("openai-mini") if failing_cloud else FakeLLM("openai-mini")
    cloud.kind = "cloud"
    return ProviderRegistry(
        Settings(_env_file=None),
        providers={"openai-mini": cloud, "ollama": FakeLLM("ollama")},
        metrics=metrics,
    )


async def test_success_counts_ok():
    m = Metrics()
    await _providers(m, failing_cloud=False).complete("mini", DataClass.INTERNAL, MSG)
    assert (
        m.registry.get_sample_value(
            "rag_provider_calls_total",
            {"provider": "openai-mini", "kind": "cloud", "outcome": "ok"},
        )
        == 1.0
    )


async def test_fallback_and_error_outcomes():
    m = Metrics()
    reg = _providers(m, failing_cloud=True)
    await reg.complete("mini", DataClass.INTERNAL, MSG)  # cloud fails → local ok
    assert (
        m.registry.get_sample_value(
            "rag_provider_calls_total",
            {"provider": "openai-mini", "kind": "cloud", "outcome": "fallback"},
        )
        == 1.0
    )
    assert (
        m.registry.get_sample_value(
            "rag_provider_calls_total",
            {"provider": "ollama", "kind": "local", "outcome": "ok"},
        )
        == 1.0
    )

    reg.providers["ollama"] = FailingLLM("ollama")  # kind=local — LAST chain link
    with pytest.raises(RuntimeError):
        await reg.complete("mini", DataClass.CONFIDENTIAL, MSG)
    assert (
        m.registry.get_sample_value(
            "rag_provider_calls_total",
            {"provider": "ollama", "kind": "local", "outcome": "error"},
        )
        == 1.0
    )


def test_streaming_counts_the_selected_provider():
    m = Metrics()
    reg = _providers(m, failing_cloud=False)
    reg.stream("mini", DataClass.INTERNAL, MSG)
    assert (
        m.registry.get_sample_value(
            "rag_provider_calls_total",
            {"provider": "openai-mini", "kind": "cloud", "outcome": "ok"},
        )
        == 1.0
    )


def test_registry_without_metrics_stays_silent():
    """metrics=None (the default) must not count or crash — existing callers
    and tests stay untouched."""
    reg = ProviderRegistry(Settings(_env_file=None), providers={"ollama": FakeLLM("ollama")})
    assert reg.metrics is None


# ── endpoint-level: /metrics exposition + hygiene (fixture from test_api_audit)
async def test_metrics_endpoint_is_free_and_lists_all_metrics(app_state):  # noqa: F811
    app_state()
    async with client() as c:
        r = await c.get("/metrics")  # deliberately no X-API-Key
    assert r.status_code == 200
    for name in EXPECTED_LABELS:
        assert name in r.text


async def test_question_marker_never_reaches_the_exposition(app_state):  # noqa: F811
    marker = "MARKER-metrics-7c2e"
    retriever, _, llm = app_state()
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    async with client() as c:
        r = await c.post(
            "/query", json={"question": f"Was ist {marker}?"}, headers={"X-API-Key": "demo-anna-it"}
        )
        assert r.status_code == 200
        exposition = (await c.get("/metrics")).text

    assert marker not in exposition
    assert 'rag_collection_queries_total{collection="handbuecher"} 1.0' in exposition


async def test_unknown_collection_becomes_the_unknown_label(app_state):  # noqa: F811
    _, _, llm = app_state()
    llm.queue(CONDENSE_DIRECT, "Antwort.")

    async with client() as c:
        r = await c.post(
            "/query",
            json={"question": "x", "collection": "attacker-chosen-name"},
            headers={"X-API-Key": "demo-anna-it"},
        )
        assert r.status_code == 200
        exposition = (await c.get("/metrics")).text

    assert "attacker-chosen-name" not in exposition
    assert 'rag_collection_queries_total{collection="unknown"} 1.0' in exposition


async def test_cache_miss_and_rate_limited_counters(app_state):  # noqa: F811
    retriever, _, llm = app_state(
        rate_limit_enabled=True, rate_limit_query_burst=2, rate_limit_query_per_min=1
    )
    await app_state.seed(retriever)
    llm.queue(CONDENSE_DIRECT, "Antwort [S1].")

    async with client() as c:
        headers = {"X-API-Key": "demo-anna-it"}
        assert (await c.post("/query", json={"question": "a"}, headers=headers)).status_code == 200
        assert (
            await c.post(
                "/feedback", json={"rating": 1, "condensed_question": "q"}, headers=headers
            )
        ).status_code == 200
        limited = await c.post(
            "/feedback", json={"rating": 1, "condensed_question": "q"}, headers=headers
        )
        assert limited.status_code == 429
        exposition = (await c.get("/metrics")).text

    assert 'rag_cache_events_total{result="miss"} 1.0' in exposition
    assert 'rag_rate_limited_total{route_class="query"} 1.0' in exposition
    assert 'rag_requests_total{route_class="query"} 3.0' in exposition
