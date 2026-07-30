"""FastAPI service: SSE chat endpoint with glass-box trace, async ingestion,
document/user-data deletion, feedback, health/readiness.

SSE contract (ui/index.html consumes this):
  event: meta       — once, early: route, provider, model, data class, cache flag
  event: token      — streamed answer text chunks
  event: citations  — validated citation list (final, after post-validation)
  event: done       — full trace for the glass-box metadata line + debug panel

The direct path streams real tokens; the agentic path generates inside the guarded
loop first (streaming a possibly-discarded generation would be wrong) and then
pseudo-streams the validated answer.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import __version__
from .audit import (
    AuditEvent,
    AuditWriteError,
    build_auth_failure,
    build_ingest_event,
    build_policy_denial,
    build_query_event,
    build_rate_limited,
)
from .auth import (
    SESSION_COOKIE,
    AuthAdapter,
    AuthSessionStore,
    OidcAuth,
    StaticKeyAuth,
    parse_session_cookie,
)
from .cache import AnswerCache
from .cachekey import build_answer_cache_key
from .citations import extractive_answer, is_refusal, validate_citations
from .config import Registry, Settings, get_registry, get_settings
from .deletion import DeletionCascade
from .domain import DataClass, QueryScope, RouteDecision, Trace
from .embeddings import build_embedder
from .llm.registry import ProviderRegistry
from .obs import Metrics, route_class_of, setup_logging
from .pipeline import RagPipeline
from .policy import effective_data_class, ingest_department_allowed
from .ratelimit import RedisRateLimiter
from .readiness import validate_config_values, validate_production_readiness
from .rerankers import build_reranker
from .sessions import SessionStore
from .store.audit import PgAuditLog
from .store.pg import PgBackend, PgRetriever, PgStore

log = logging.getLogger(__name__)

UI_INDEX = Path(__file__).resolve().parent.parent.parent / "ui" / "index.html"

# Sanity cap for identifier-like request fields (collection, session_id) —
# checked in the endpoints (not only Pydantic) alongside the settings-driven
# size limits.
MAX_FIELD_CHARS = 200


class AppState:
    settings: Settings
    registry: Registry
    auth: AuthAdapter
    auth_sessions: AuthSessionStore
    backend: PgBackend
    store: PgStore
    retriever: PgRetriever
    redis: aioredis.Redis
    cache: AnswerCache
    sessions: SessionStore
    deleter: DeletionCascade
    providers: ProviderRegistry
    pipeline: RagPipeline
    ratelimiter: RedisRateLimiter
    audit: PgAuditLog
    metrics: Metrics
    arq: Any


state = AppState()
# Module-level default mirrors the settings default (auth_backend=static-key)
# so lifespan-less unit tests keep working; the lifespan re-selects the
# adapter from the SETTING (E2 — never from deployment_mode).
state.auth = StaticKeyAuth()


# ── audit write path (A4) ──────────────────────────────────────────────────────
async def _write_audit(event: AuditEvent, *, critical: bool) -> None:
    """The single audit write path. critical=True only for the /query access
    event when AUDIT_FAIL_CLOSED is set and the data class is confidential —
    then a failed write aborts the stream (fail-closed) before any token.
    Runtime behavior NEVER branches on deployment_mode (E2); production
    enforcement of these settings lives in the readiness gate (R7)."""
    if not state.settings.audit_enabled:
        return
    try:
        await state.audit.write(event)
    except Exception as exc:  # noqa: BLE001 — any backend error is a write failure
        # Log hygiene: only the exception class — audit failures must not leak
        # event contents into logs either.
        state.metrics.audit_write_failures.inc()
        log.error("audit write failed: %s", type(exc).__name__)
        if critical:
            raise AuditWriteError() from exc


# Fire-and-forget for non-critical events. The set keeps strong references
# until completion (asyncio holds tasks weakly); _write_audit with
# critical=False never raises, so no unhandled task exceptions.
_audit_tasks: set[asyncio.Task[None]] = set()


def _audit_background(event: AuditEvent) -> None:
    task = asyncio.create_task(_write_audit(event, critical=False))
    _audit_tasks.add(task)
    task.add_done_callback(_audit_tasks.discard)


class _ReceiptAuditLog:
    """AuditLog port for the DeletionCascade, adapting the central write path:
    deletion receipts are always non-critical (deletion precedence — a
    deletion never fails on its receipt) and their failures are logged and
    counted in the one place all audit failures are."""

    async def write(self, event: AuditEvent) -> None:
        await _write_audit(event, critical=False)

    async def purge_expired(self, retention_days: int) -> int:
        return await state.audit.purge_expired(retention_days)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.log_json)
    # Fail-closed config validation in EVERY mode, before any connection —
    # DEPLOYMENT_MODE=prod (typo) must never silently run as demo.
    validate_config_values(s)
    state.settings = s
    state.registry = get_registry()  # validates config/collections.yaml at startup
    # Pure constructions, no I/O — needed here so the production gate can scan
    # the provider chains before the first connection is opened. Metrics first:
    # the registry counts provider outcomes through it.
    state.metrics = Metrics()
    state.providers = ProviderRegistry(s, metrics=state.metrics)
    if s.deployment_mode == "production":
        # No partial starts: connections only exist after a passed gate. This
        # also keeps the refusal test a unit test (no services required).
        findings = validate_production_readiness(s, state.registry, state.providers, os.environ)
        if findings:
            for f in findings:
                log.error("readiness %s: %s (remedy: %s)", f.rule, f.message, f.remedy)
            raise RuntimeError(f"production readiness failed: {len(findings)} finding(s)")
    state.backend = PgBackend(s.database_url, rrf_k=s.rrf_k)
    await state.backend.open()
    state.store = PgStore(state.backend)
    # Per-collection embedding versions: ingestion writes them (worker) and the
    # cache key carries them — retrieval must filter on the SAME version or a
    # blue-green reindex (bump in collections.yaml) silently returns zero rows.
    state.retriever = PgRetriever(
        state.backend,
        versions={n: c.embedding_version for n, c in state.registry.collections.items()},
    )
    state.redis = aioredis.from_url(s.redis_url, decode_responses=True)
    state.cache = AnswerCache(state.redis, ttl_s=s.answer_cache_ttl_s)
    state.ratelimiter = RedisRateLimiter(state.redis)
    state.sessions = SessionStore(state.redis, ttl_s=s.session_ttl_s)
    # Adapter selection happens ONCE, from the auth_backend SETTING (E2 —
    # never from deployment_mode). The auth-session store exists in both
    # modes so the /me/data sweep stays uniform; only OidcAuth reads it.
    state.auth_sessions = AuthSessionStore(state.redis, ttl_s=s.auth_session_ttl_s)
    if s.auth_backend == "oidc":
        oidc = OidcAuth(s)
        oidc.sessions = state.auth_sessions
        state.auth = oidc
    else:
        state.auth = StaticKeyAuth()
    state.audit = PgAuditLog(state.backend)
    state.deleter = DeletionCascade(
        state.store,
        state.cache,
        state.sessions,
        audit=_ReceiptAuditLog(),
        auth_sessions=state.auth_sessions,
    )
    embedder = build_embedder(s)
    reranker = build_reranker(s)
    state.pipeline = RagPipeline(state.providers, state.retriever, embedder, reranker, s)
    state.arq = await create_pool(RedisSettings.from_dsn(s.redis_url))
    log.info("api ready (profile=%s, run_mode=%s)", s.rag_profile, s.run_mode)
    yield
    await state.backend.close()
    await state.redis.aclose()


app = FastAPI(title="enterprise-rag-assistant", version=__version__, lifespan=lifespan)


# ── metrics middleware (A5) ────────────────────────────────────────────────────
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Counts and times every request by route class. HONEST semantics: for
    SSE responses this measures time to response START (~TTFB), not stream
    end — full answer latency is a trace/eval SLO concern, not this metric."""
    route_class = route_class_of(request.method, request.url.path)
    t0 = time.perf_counter()
    try:
        return await call_next(request)
    finally:
        state.metrics.requests_total.labels(route_class=route_class).inc()
        state.metrics.request_seconds.labels(route_class=route_class).observe(
            time.perf_counter() - t0
        )


# ── auth dependency ────────────────────────────────────────────────────────────
async def require_scope(request: Request) -> QueryScope:
    # async so the audit write can be scheduled on the running loop. The
    # adapter (state.auth) decides HOW the request authenticates; this one
    # dependency owns the 401 path for every backend.
    scope = await state.auth.authenticate(request, state.settings, state.registry.default_tenant)
    if scope is None:
        # auth_failure has no valid scope (tenant/user_id NULL) and the
        # builder has no parameter for the presented credential — by design.
        _audit_background(build_auth_failure())
        raise HTTPException(status_code=401, detail=state.auth.unauthorized_detail)
    return scope


# ── rate limiting dependency (A3) ──────────────────────────────────────────────
def rate_limited(route_class: str):
    """Per-identity token-bucket limit, AFTER auth (identity comes from the
    scope). Route classes: 'query' (reranker/LLM cost) and 'ingest'
    (embedding/write path). All GET endpoints stay unlimited — health/ready
    must remain free for orchestration and monitoring."""

    async def _dep(scope: QueryScope = Depends(require_scope)) -> QueryScope:
        s = state.settings
        if not s.rate_limit_enabled:
            return scope
        if route_class == "query":
            capacity = float(s.rate_limit_query_burst)
            refill_per_s = s.rate_limit_query_per_min / 60.0
        else:
            capacity = float(s.rate_limit_ingest_burst)
            refill_per_s = s.rate_limit_ingest_per_min / 60.0
        allowed, retry_after = await state.ratelimiter.acquire(
            scope.tenant, scope.user_id, route_class, capacity=capacity, refill_per_s=refill_per_s
        )
        if not allowed:
            # Log hygiene: identity + route class only, never request content.
            log.warning(
                "rate_limited tenant=%s user_id=%s route_class=%s",
                scope.tenant,
                scope.user_id,
                route_class,
            )
            _audit_background(build_rate_limited(scope, route_class))
            state.metrics.rate_limited.labels(route_class=route_class).inc()
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded — retry later",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )
        return scope

    return _dep


# Module-level singletons (one dependency instance per route class — also what
# flake8-bugbear B008 expects instead of calls in argument defaults).
rate_limited_query = rate_limited("query")
rate_limited_ingest = rate_limited("ingest")


# ── BFF auth endpoints (A6) ────────────────────────────────────────────────────
# Route class 'free' (route_class_of) and no rate_limited dependency:
# redirect flows fit no rate-limit budget. Tokens NEVER appear in a browser
# response — the browser only ever holds the opaque HttpOnly session cookie.
AUTH_FLOW_TTL_S = 600  # authorization round-trip budget for state/PKCE keys


def _oidc_or_404() -> OidcAuth:
    # These routes exist only in oidc mode; a neutral 404 keeps the
    # static-key demo surface byte-identical.
    if isinstance(state.auth, OidcAuth):
        return state.auth
    raise HTTPException(status_code=404, detail="Not Found")


async def _cookie_session(request: Request) -> tuple[str, str, dict] | None:
    parsed = parse_session_cookie(request.cookies.get(SESSION_COOKIE))
    if parsed is None:
        return None
    user_id, sid = parsed
    session = await state.auth_sessions.get(state.registry.default_tenant, user_id, sid)
    if session is None:
        return None
    return user_id, sid, session


@app.get("/auth/login")
async def auth_login():
    """Start the authorization-code flow: state + PKCE S256 (stdlib), flow
    state single-use in Redis, 302 to the IdP's PUBLIC authorization
    endpoint (the backchannel rewrite never touches it)."""
    auth = _oidc_or_404()
    try:
        doc = await auth.discovery()
    except Exception as exc:  # noqa: BLE001 — any discovery failure is the same clean error
        # An unreachable IdP surfaces HERE with a clean error — deliberately
        # no IdP check in /ready (documented extension).
        log.warning("oidc discovery failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="identity provider unreachable") from None
    flow_state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    await state.redis.set(
        f"authflow:{flow_state}",
        json.dumps({"code_verifier": code_verifier}),
        ex=AUTH_FLOW_TTL_S,
    )
    params = urlencode(
        {
            "response_type": "code",
            "client_id": auth.client_id,
            "redirect_uri": state.settings.public_base_url + "/auth/callback",
            "scope": "openid email profile",
            "state": flow_state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return RedirectResponse(f"{doc['authorization_endpoint']}?{params}", status_code=302)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Finish the flow: single-use state lookup (replay → neutral 400),
    backchannel token exchange (confidential client + PKCE verifier),
    ID-token validation, session creation. BFF core guarantee: no token in
    any browser response — only the opaque session cookie + a redirect."""
    auth = _oidc_or_404()
    code = request.query_params.get("code", "")
    flow_state = request.query_params.get("state", "")
    flow_raw = None
    if code and flow_state:
        key = f"authflow:{flow_state}"
        flow_raw = await state.redis.get(key)
        await state.redis.delete(key)  # single-use, deleted BEFORE any use
    if not flow_raw:
        raise HTTPException(status_code=400, detail="invalid login response")
    code_verifier = json.loads(flow_raw)["code_verifier"]
    redirect_uri = state.settings.public_base_url + "/auth/callback"
    try:
        id_token = await auth.exchange_code(code, code_verifier, redirect_uri)
    except Exception as exc:  # noqa: BLE001 — exception text may carry IdP payload, class only
        log.warning("oidc token exchange failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="login failed") from None
    claims = await auth.validate_token(id_token)
    if claims is None:
        # Same parameterless auth_failure event as every 401 — the token
        # structurally cannot reach the audit trail.
        _audit_background(build_auth_failure())
        raise HTTPException(status_code=401, detail=auth.unauthorized_detail)
    scope = auth.scope_from_claims(claims, state.registry.default_tenant)  # 403 fail-closed
    sid, _csrf = await state.auth_sessions.create(scope.tenant, scope.user_id, scope.department)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        f"{scope.user_id}:{sid}",
        max_age=state.settings.auth_session_ttl_s,
        httponly=True,
        samesite="lax",
        secure=state.settings.session_cookie_secure,
        path="/",
    )
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """Delete exactly the caller's session (immediately — the TTL is only
    the deletion-concept backstop) and clear the cookie. CSRF-required:
    a cookie-authenticated unsafe method (E4)."""
    _oidc_or_404()
    found = await _cookie_session(request)
    if found is None:
        raise HTTPException(status_code=401, detail=state.auth.unauthorized_detail)
    user_id, sid, session = found
    token = request.headers.get("X-CSRF-Token", "")
    if not token or not secrets.compare_digest(token, session["csrf"]):
        raise HTTPException(status_code=403, detail="request rejected")
    await state.auth_sessions.delete(state.registry.default_tenant, user_id, sid)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/auth/session")
async def auth_session(request: Request):
    """UI bootstrap: who am I + the CSRF token for unsafe requests; 401
    without a valid session (the UI then offers the login action)."""
    _oidc_or_404()
    found = await _cookie_session(request)
    if found is None:
        raise HTTPException(status_code=401, detail=state.auth.unauthorized_detail)
    user_id, _sid, session = found
    return {"user_id": user_id, "department": session["department"], "csrf": session["csrf"]}


# ── request models ─────────────────────────────────────────────────────────────
class QueryRequest(BaseModel):
    question: str
    session_id: str = ""
    collection: str = ""


class IngestRequest(BaseModel):
    doc_id: str
    title: str
    collection: str = ""
    department: str = "all"
    fmt: str = "md"
    content_b64: str = ""
    content_text: str = ""  # convenience for markdown/plain text


class FeedbackRequest(BaseModel):
    rating: int  # 1 | -1
    condensed_question: str
    route: str = "direct"
    collection: str = ""
    cited_doc_ids: list[str] = []


# ── SSE helpers ────────────────────────────────────────────────────────────────
def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def error_sse(exc: Exception, data_class: DataClass) -> str:
    """A structured error frame instead of a silently aborted stream. The
    confidential fail-closed case (local provider down, NO cloud fallback) is
    the security model working — say so, don't render a generic 'Verbindungsfehler'.
    Only a class name is exposed, never the raw exception text."""
    if data_class == DataClass.CONFIDENTIAL:
        message = (
            "Vertrauliche Anfragen werden ausschließlich lokal (Ollama) verarbeitet — "
            "der lokale Dienst ist gerade nicht erreichbar. Kein Cloud-Fallback (fail-closed)."
        )
    else:
        message = "Die Anfrage konnte nicht beantwortet werden. Bitte erneut versuchen."
    return sse("error", {"message": message, "detail": type(exc).__name__})


def _trace_payload(trace: Trace) -> dict:
    payload = trace.model_dump()
    # Debug panel needs ranks/scores, not full chunk texts (keep events small).
    payload["candidates"] = [
        {
            "chunk_id": c.chunk_id,
            "doc_title": c.doc_title,
            "section_path": c.section_path,
            "dense_rank": c.dense_rank,
            "lex_rank": c.lex_rank,
            "rrf_score": round(c.rrf_score, 5),
            "rerank_score": round(c.rerank_score, 4) if c.rerank_score is not None else None,
            "in_context": c.in_context,
            "preview": c.content[:160],
        }
        for c in trace.candidates
    ]
    return payload


# ── endpoints ──────────────────────────────────────────────────────────────────
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(UI_INDEX)


@app.post("/query")
async def query(req: QueryRequest, scope: QueryScope = Depends(rate_limited_query)):
    # Server-side size caps (A3) — neutral errors, no request content echoed.
    if len(req.question) > state.settings.max_question_chars:
        raise HTTPException(
            status_code=413,
            detail=f"question exceeds the {state.settings.max_question_chars} character limit",
        )
    if len(req.collection) > MAX_FIELD_CHARS or len(req.session_id) > MAX_FIELD_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"collection/session_id must not exceed {MAX_FIELD_CHARS} characters",
        )
    collection_name = req.collection or state.registry.default_collection
    collection = state.registry.get(collection_name)
    # Label hygiene: only registry names become label values — an arbitrary
    # request string must never mint a new timeseries (cardinality).
    state.metrics.collection_queries.labels(
        collection=collection_name if collection is not None else "unknown"
    ).inc()
    data_class = effective_data_class(collection)  # unknown → confidential (fail-closed)
    session_id = req.session_id or uuid.uuid4().hex
    # Correlation id for the audit trail only (the SSE meta frame is unchanged).
    request_id = uuid.uuid4().hex
    audit_written = False

    async def _record_access(doc_ids: list[str]) -> None:
        """The A4 access event: WHO got WHICH documents (as surrogates) into
        the answer context — written AFTER context assembly and BEFORE the
        first token frame on every route. Critical (awaited, fail-closed) only
        when AUDIT_FAIL_CLOSED is set and the class is confidential."""
        nonlocal audit_written
        refs = await state.store.audit_refs_for(scope, doc_ids)
        event = build_query_event(
            scope,
            collection_name,
            data_class.value,
            tuple(refs),
            served=bool(doc_ids),
            request_id=request_id,
        )
        if state.settings.audit_fail_closed and data_class == DataClass.CONFIDENTIAL:
            await _write_audit(event, critical=True)
        else:
            _audit_background(event)
        audit_written = True

    async def _body() -> AsyncIterator[str]:
        t_start = time.perf_counter()
        trace = Trace(collection=collection_name, data_class=data_class)
        history = await state.sessions.history(scope.tenant, scope.user_id, session_id)

        # ── ONE structured pre-call: condense + route ──
        # Extractive collections skip it: their request path makes NO LLM call
        # at all — the user question must not reach any model either.
        if collection is not None and collection.generation == "extractive":
            decision = RouteDecision(standalone_query=req.question, route="extractive")
        else:
            decision = await state.pipeline.condense_and_route(
                req.question, history, data_class, trace
            )
        trace.route = decision.route

        # ── permission-scoped cache (sits in FRONT of RLS → key carries scope) ──
        corpus_v = await state.store.corpus_version(scope, collection_name)
        emb_v = collection.embedding_version if collection else 1
        cache_key = build_answer_cache_key(
            scope, data_class, decision.standalone_query, corpus_v, emb_v
        )
        cached = await state.cache.get(cache_key)
        state.metrics.cache_events.labels(result="hit" if cached else "miss").inc()
        if cached:
            trace.cache_hit = True
            trace.ttft_ms = (time.perf_counter() - t_start) * 1000
            trace.total_ms = trace.ttft_ms
            yield sse("meta", {"route": trace.route, "cache_hit": True, "session_id": session_id})
            yield sse("token", {"text": cached["answer"]})
            yield sse("citations", cached.get("citations", []))
            yield sse("done", _trace_payload(trace))
            await state.sessions.append(
                scope.tenant, scope.user_id, session_id, "user", req.question
            )
            await state.sessions.append(
                scope.tenant,
                scope.user_id,
                session_id,
                "assistant",
                cached["answer"],
                cached.get("cited_doc_ids", []),
            )
            return

        yield sse(
            "meta",
            {
                "route": decision.route,
                "standalone_query": decision.standalone_query,
                "data_class": data_class.value,
                "cache_hit": False,
                "session_id": session_id,
            },
        )

        citations_payload: list[dict] = []
        cited_doc_ids: list[str] = []
        answer_text = ""
        refused = False

        if decision.route == "extractive":
            candidates = await state.pipeline.retrieve(
                scope, collection_name, decision.standalone_query, trace
            )
            bundle = state.pipeline.build_context(candidates, trace)
            await _record_access(bundle.cited_doc_ids)
            answer_text, ex_citations = extractive_answer(bundle)
            refused = is_refusal(answer_text)
            citations_payload = [c.model_dump() for c in ex_citations]
            cited_doc_ids = list({c.doc_id: None for c in ex_citations})
            trace.provider, trace.model, trace.tier = "none", "", "extractive"
            trace.ttft_ms = (time.perf_counter() - t_start) * 1000
            # pseudo-stream the verbatim passages (no model, nothing to stream live)
            for i in range(0, len(answer_text), 24):
                yield sse("token", {"text": answer_text[i : i + 24]})
        elif decision.route == "agentic":
            answer, bundle = await state.pipeline.run_agentic(
                scope, collection_name, data_class, req.question, decision.standalone_query, trace
            )
            await _record_access(bundle.cited_doc_ids)
            answer_text, refused = answer.text, answer.refused
            citations_payload = [c.model_dump() for c in answer.citations]
            cited_doc_ids = list({c.doc_id: None for c in answer.citations})
            trace.ttft_ms = (time.perf_counter() - t_start) * 1000
            # pseudo-stream the validated answer
            for i in range(0, len(answer_text), 24):
                yield sse("token", {"text": answer_text[i : i + 24]})
        else:
            candidates = await state.pipeline.retrieve(
                scope, collection_name, decision.standalone_query, trace
            )
            bundle = state.pipeline.build_context(candidates, trace)
            # The direct path only streams AFTER this point — fail-closed is
            # enforceable without a single token leaving before the event.
            await _record_access(bundle.cited_doc_ids)
            messages = state.pipeline.answer_messages(req.question, bundle)
            t_gen = time.perf_counter()
            stream, provider = state.providers.stream("mini", data_class, messages)
            trace.provider, trace.model, trace.tier = provider.name, provider.model, "mini"
            parts: list[str] = []
            first = True
            async for delta in stream:
                if first:
                    trace.ttft_ms = (time.perf_counter() - t_start) * 1000
                    first = False
                parts.append(delta)
                yield sse("token", {"text": delta})
            trace.add_stage("generate", (time.perf_counter() - t_gen) * 1000)
            raw = "".join(parts)
            answer_text, citations, _ = validate_citations(raw, bundle)
            refused = is_refusal(answer_text)
            citations_payload = [c.model_dump() for c in citations]
            cited_doc_ids = list({c.doc_id: None for c in citations})

        trace.total_ms = (time.perf_counter() - t_start) * 1000
        yield sse("citations", citations_payload)
        yield sse("done", _trace_payload(trace))

        await state.sessions.append(scope.tenant, scope.user_id, session_id, "user", req.question)
        await state.sessions.append(
            scope.tenant, scope.user_id, session_id, "assistant", answer_text, cited_doc_ids
        )
        if not refused:
            await state.cache.set(
                cache_key,
                {
                    "answer": answer_text,
                    "citations": citations_payload,
                    "cited_doc_ids": cited_doc_ids,
                },
                cited_doc_ids,
            )

    async def event_stream() -> AsyncIterator[str]:
        # Turn any exception into a structured error frame instead of a silently
        # aborted stream. Fail-closed on the confidential path lands here too and
        # is reported as the intended security behavior, not a generic failure.
        try:
            async for frame in _body():
                yield frame
        except AuditWriteError as exc:
            # The CRITICAL access event could not be persisted — honest German
            # message: no log record, no access (fail-closed by setting).
            log.warning("query stream aborted fail-closed: audit write failed")
            yield sse(
                "error",
                {
                    "message": (
                        "Die Zugriffsprotokollierung ist derzeit nicht möglich — "
                        "der Zugriff wird nicht gewährt (fail-closed)."
                    ),
                    "detail": type(exc).__name__,
                },
            )
        except Exception as exc:  # noqa: BLE001 — the stream is the only channel to the client
            if isinstance(exc, PermissionError):
                _audit_background(build_policy_denial(scope, collection_name, data_class.value))
                state.metrics.policy_denials.inc()
            elif not audit_written:
                # Failure BEFORE context assembly (e.g. condense): record that
                # the access attempt failed — exactly one event per request.
                _audit_background(
                    AuditEvent(
                        tenant=scope.tenant,
                        user_id=scope.user_id,
                        department=scope.department,
                        event_type="query",
                        collection=collection_name,
                        data_class=data_class.value,
                        decision="failed",
                        request_id=request_id,
                    )
                )
            # Errors AFTER the access event write NO second event — the access
            # is on record, the failure lives in the log.
            log.warning("query stream failed (data_class=%s): %s", data_class.value, exc)
            yield error_sse(exc, data_class)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/ingest")
async def ingest(req: IngestRequest, scope: QueryScope = Depends(rate_limited_ingest)):
    if len(req.collection) > MAX_FIELD_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"collection must not exceed {MAX_FIELD_CHARS} characters",
        )
    collection = req.collection or state.registry.default_collection
    if state.registry.get(collection) is None:
        raise HTTPException(status_code=400, detail=f"unknown collection '{collection}'")
    if not ingest_department_allowed(scope.department, req.department):
        # Write-side RLS mirror: without this, any user could plant documents
        # into a foreign department's visibility (the DB policy also rejects it,
        # but only deep inside the worker with an opaque job error).
        raise HTTPException(
            status_code=403,
            detail=(
                f"department '{req.department}' not allowed for this user "
                f"(own department '{scope.department}' or 'all')"
            ),
        )
    content_b64 = req.content_b64 or base64.b64encode(req.content_text.encode()).decode()
    if not content_b64:
        raise HTTPException(status_code=400, detail="content_b64 or content_text required")
    # Size cap on the DECODED content (A3) — base64 inflates by ~33%, so the
    # encoded length would under-enforce the limit.
    try:
        decoded_bytes = len(base64.b64decode(content_b64))
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64") from None
    if decoded_bytes > state.settings.max_ingest_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"document exceeds the {state.settings.max_ingest_bytes} byte limit",
        )
    job = await state.arq.enqueue_job(
        "ingest_document",
        doc_id=req.doc_id,
        title=req.title,
        collection=collection,
        department=req.department,
        content_b64=content_b64,
        fmt=req.fmt,
        tenant=scope.tenant,
        user_id=scope.user_id,
    )
    # Accepted-for-processing event, deliberately WITHOUT doc_id/title
    # (speaking values). The worker writes NO second event (Phase 2 boundary).
    _audit_background(build_ingest_event(scope, collection, None))
    return {"job_id": job.job_id, "status": "queued"}


@app.get("/ingest/{job_id}")
async def ingest_status(job_id: str, scope: QueryScope = Depends(require_scope)):
    job = Job(job_id, state.arq)
    status = await job.status()
    result: Any = None
    if status == JobStatus.complete:
        try:
            result = await job.result(timeout=0)
        except Exception as exc:  # job failed — surface the reason, not a 500
            result = {"error": str(exc)}
    return {"job_id": job_id, "status": status.value, "result": result}


@app.get("/documents")
async def list_documents(scope: QueryScope = Depends(require_scope)):
    # RLS scopes this list exactly like search — no metadata leaks across departments.
    return await state.store.list_documents(scope)


@app.get("/stats")
async def stats(scope: QueryScope = Depends(require_scope)):
    """RLS-scoped knowledge-base metrics for the dashboard."""
    data = await state.store.stats(scope)
    # annotate collections with their declared data class (from the registry)
    for row in data["per_collection"]:
        cfg = state.registry.get(row["collection"])
        row["data_class"] = cfg.data_class.value if cfg else "unknown"
    return data


@app.get("/collections")
async def collections(scope: QueryScope = Depends(require_scope)):
    """The declarative collection registry — data class drives provider routing."""
    return [
        {
            "name": c.name,
            "data_class": c.data_class.value,
            "embedding_version": c.embedding_version,
            "description": c.description.strip(),
            "local_only": c.data_class.value == "confidential",
        }
        for c in state.registry.collections.values()
    ]


@app.get("/documents/{doc_id}/chunks")
async def document_chunks(doc_id: str, scope: QueryScope = Depends(require_scope)):
    """Inspect how a document was chunked (RLS-scoped)."""
    return await state.store.document_chunks(scope, doc_id)


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, scope: QueryScope = Depends(rate_limited_ingest)):
    report = await state.deleter.delete_document(scope, doc_id)
    return report.model_dump()


@app.post("/feedback")
async def feedback(req: FeedbackRequest, scope: QueryScope = Depends(rate_limited_query)):
    if len(req.collection) > MAX_FIELD_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"collection must not exceed {MAX_FIELD_CHARS} characters",
        )
    if req.rating not in (-1, 1):
        raise HTTPException(status_code=400, detail="rating must be 1 or -1")
    await state.store.add_feedback(
        scope,
        req.collection or state.registry.default_collection,
        req.rating,
        req.condensed_question,
        req.route,
        req.cited_doc_ids,
    )
    return {"status": "recorded"}


@app.delete("/me/data")
async def delete_my_data(scope: QueryScope = Depends(require_scope)):
    """Self-service deletion: delete the CALLER's own trail — feedback
    rows and sessions. Scope comes from auth, so nobody can name a foreign
    user; deleting on behalf of others is an operator process (deletion
    concept in docs/ARCHITECTURE.md)."""
    report = await state.deleter.delete_user_data(scope)
    return report.model_dump()


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint — free route class, same A3 decision as
    health/ready (orchestration and monitoring must always reach it).
    Contains nothing user-related (label hygiene is tested); operators do
    NOT expose it publicly (reverse proxy / network policy — operator
    handbook detail follows in Phase 3)."""
    return Response(content=generate_latest(state.metrics.registry), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    # deployment_mode lets the UI hide demo-only affordances (user switcher)
    # when the API runs hardened; auth_backend (A6) switches the UI between
    # X-API-Key and the BFF cookie/CSRF path — same one-field construction,
    # no extra endpoint.
    return {
        "status": "ok",
        "version": __version__,
        "deployment_mode": state.settings.deployment_mode,
        "auth_backend": state.settings.auth_backend,
    }


@app.get("/ready")
async def ready(response: Response):
    """Readiness probe over the core dependencies. Returns HTTP 503 when
    degraded so orchestrators (and the UI) can act on the status code, not just
    the body."""
    checks: dict[str, str] = {}
    try:
        async with state.backend.pool.connection() as conn:
            await conn.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"
    try:
        await state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "degraded", "checks": checks}
