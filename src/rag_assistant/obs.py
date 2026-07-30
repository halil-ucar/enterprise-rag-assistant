"""Observability (A5): Prometheus metrics for the API process.

One `Metrics` instance per app state with its OWN CollectorRegistry — the
library's global default REGISTRY breaks under pytest (a second app
construction raises "Duplicated timeseries"). Construction is pure (no I/O),
so the lifespan can build it BEFORE the production gate runs.

Label hygiene is a RULE, enforced by tests: never user_id, never tenant,
never free text in labels. The only user-influenced label (collection) is
clamped to registry names, everything else is 'unknown' — cardinality is
never user-controlled.

Scope boundary (documented, deliberate): /metrics measures the API process
only. The worker process has no metrics in Phase 2 — its crons log counts.
For SSE responses the HTTP middleware observes time to response START
(~TTFB), not stream end; full answer latency remains a trace/eval SLO
concern. Do not present rag_request_seconds as end-to-end latency.
"""

from __future__ import annotations

import json
import logging

from prometheus_client import CollectorRegistry, Counter, Histogram


class JsonLogFormatter(logging.Formatter):
    """Stdlib-only JSON log lines (A5, E5: no new dependency): ts, level,
    logger, message. Exceptions contribute ONLY exc_type — the class name.
    No exception text, no traceback: the content ban holds in logs too."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(json_mode: bool) -> None:
    """Called once at process start (api lifespan, worker startup). False is
    a strict no-op — today's log format stays byte-identical."""
    if not json_mode:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logging.getLogger().handlers[:] = [handler]


# CPU reality of the container run mode: confidential answers on local
# qwen3 take seconds to minutes — the buckets must resolve that range.
REQUEST_SECONDS_BUCKETS = (0.05, 0.2, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)


def route_class_of(method: str, path: str) -> str:
    """Pure request classifier for metrics labels, mirroring the A3 route
    classes: 'query' (POST /query, /feedback), 'ingest' (POST /ingest,
    DELETE /documents/*), 'free' (all GET incl. /metrics, static UI, rest)."""
    if method == "POST" and path in ("/query", "/feedback"):
        return "query"
    if method == "POST" and path == "/ingest":
        return "ingest"
    if method == "DELETE" and path.startswith("/documents/"):
        return "ingest"
    return "free"


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests_total = Counter(
            "rag_requests_total",
            "HTTP requests by route class",
            ["route_class"],
            registry=self.registry,
        )
        self.request_seconds = Histogram(
            "rag_request_seconds",
            "HTTP request duration; for SSE this is time to response START (~TTFB)",
            ["route_class"],
            buckets=REQUEST_SECONDS_BUCKETS,
            registry=self.registry,
        )
        self.provider_calls = Counter(
            "rag_provider_calls_total",
            "LLM provider calls by outcome (ok | fallback | error)",
            ["provider", "kind", "outcome"],
            registry=self.registry,
        )
        self.cache_events = Counter(
            "rag_cache_events_total",
            "Answer cache lookups (hit | miss)",
            ["result"],
            registry=self.registry,
        )
        self.collection_queries = Counter(
            "rag_collection_queries_total",
            "Queries per collection (registry names only; unknown otherwise)",
            ["collection"],
            registry=self.registry,
        )
        self.policy_denials = Counter(
            "rag_policy_denials_total",
            "Requests denied by the data-class policy",
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "rag_rate_limited_total",
            "Requests rejected by the per-identity rate limiter",
            ["route_class"],
            registry=self.registry,
        )
        self.audit_write_failures = Counter(
            "rag_audit_write_failures_total",
            "Failed audit trail writes (critical failures also abort the stream)",
            registry=self.registry,
        )
