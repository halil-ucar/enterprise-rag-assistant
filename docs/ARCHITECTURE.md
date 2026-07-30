# Architecture decisions

Every significant decision with its rationale and the rejected alternatives.
Evidence discipline: claims marked **[benchmark-reported]** come from public write-ups and
are treated as directional, not gospel — the eval harness (`make eval`) measures what
actually matters on THIS corpus.

## Principles

1. **Platform architecture at demo scale.** Contracts, schemas and patterns are those of a
   multi-tenant enterprise platform from day 1. Scaling swaps adapters and topology — never
   the architecture (see [SCALING.md](SCALING.md)).
2. **Every claim is demonstrable.** No feature without a visible proof: an eval number, a
   UI trace, or a CI test.
3. **Retrieval is the quality ceiling.** Most care goes into ingestion, hybrid search and
   retrieval metrics — an agent on top of weak retrieval just spends more money being wrong
   in more elaborate ways.
4. **Security in three layers, all as code + test:** access (RLS), data (class routing +
   deletion), prompt (injection hardening).

## Storage: PostgreSQL + pgvector

**Decision:** one system for vectors, metadata, full-text search, row-level security and
ACID deletes.
**Why:** below tens of millions of vectors a dedicated engine isn't justified, and having
everything in one transactional system is what makes the three security stories cheap:
permissions are SQL policies, deletion is a cascade, hybrid search is one query.
**Rejected:** Qdrant as primary store (measurably faster at 5–10M+ vectors
[benchmark-reported] — that's the documented migration path via the `Retriever` port, not
the default; as default it costs the RLS story plus an extra service) · Chroma/FAISS
(prototype profile, no operations model) · managed vector search (vendor lock-in for a
reference implementation).

## Schema geometry (the expensive-to-retrofit part)

Every chunk row carries `tenant_id`, `doc_id` (deletion lineage), `content_hash`
(idempotency), `department` (ACL), `data_class`, `embedding_version`. Collections live in
declarative YAML validated at startup — tenant onboarding is a config change.
`embedding_version` enables blue-green reindexing: model swap = build in parallel, switch
atomically. `tenant_id` is the future partition/shard key.

## Ingestion

**Decision:** async queue (arq/Redis) + worker; parse (Docling → Markdown, fallback:
Markdown passthrough) → structure-aware chunking → embed → atomic upsert.
**Why:** sync ingestion in a request handler is the clearest toy signal; the job-in/status-out
contract survives any scale. Markdown preserves the structure chunking needs; tables become
standalone chunks; every chunk gets its header path as prefix ("poor man's contextual
retrieval": context enters the embedding at zero LLM cost).
**Rejected:** fixed-size chunking (kept ONLY as eval comparison mode — it cuts procedures in
half) · LLM-contextual enrichment per chunk (strong reported gains [benchmark-reported], but
one LLM call per chunk; a flag-able upgrade, not a default) · re-ingest as in-place update
(search would see mixed generations; we swap atomically inside one transaction).

## Retrieval chain

```
query → [condense+route: ONE mini-call] → dense (BGE-M3, top 30)
                                        → FTS german+simple (top 30)
        → RRF (k=60, in SQL) → cross-encoder rerank → top 3–5 → context assembly
```

- **Hybrid is the baseline**: embeddings miss exact terms (error codes, product names);
  full-text misses paraphrases. German twist: compounds ("Serverraumtemperatur") defeat
  stemming — the dense leg compensates; a `simple` (unstemmed) column protects codes from
  the stemmer. Both legs run under the same RLS transaction.
- **RRF with k=60** (industry default in major engines; a default, not a proven optimum):
  rank-based fusion avoids normalizing incompatible score scales. Implemented as one SQL
  CTE with a FULL OUTER JOIN — no extra infrastructure.
- **Each FTS leg is its OWN rank list** (decision revised 2026-07-12, measured on the
  hard-negative corpus): the first implementation summed
  `ts_rank_cd(german) + ts_rank_cd(simple)` into one lexical list, and the simple leg
  accepted any word ≥5 chars. Two mistakes, one lesson. (1) The two configs are different
  score scales — chunks matching many common words in *both* configs got double credit, so
  word-overlap noise outranked the exact identifier hit *inside its own list* (diagnosed:
  the LJ70-E501 anchor at lex rank 6, fused position 11). The whole argument for RRF —
  scores across systems don't compare — applies *between* FTS configs too; now RRF fuses
  three rank lists (dense · german · simple). (2) The simple leg is identifiers-only
  (digit/hyphen tokens); ordinary long words match unstemmed everywhere and belong to the
  german leg. Effect on the 5 826-chunk index: hybrid Recall@5 0.844 → 0.906, MRR
  0.680 → 0.793 (error_code 0.567 → 0.833, location 0.600 → 1.000); end-to-end with the
  reranker MRR 0.938 → 0.953 (the cleaner pool helps the final stage too, version twins
  0.800 → 0.900); dense unchanged — the honest cost: one multi-doc question slipped
  rank 1 → 2 in the hybrid stage (n=2; the reranker restores it). Latency unchanged
  (~18 ms p50). Unit-pinned in `tests/test_lex_queries.py`.
- **Two-stage ranking**: bi-encoder recalls fast, the cross-encoder (bge-reranker-v2-m3)
  reads query+candidate together and re-sorts precisely. Deliberately switchable OFF so the
  eval MEASURES its contribution.
- **Context assembly**: dedup by (doc, section), hard token budget, few passages beat many
  (precision AND short prefill). Citation anchors are doc + section path — stable across
  re-chunking, human-readable.
- **Filtered ANN caveat:** selective RLS/collection filters can starve HNSW results
  (post-filtering); pgvector ≥ 0.8 iterative index scans are enabled when available
  (version-detected at startup).

## Generation & providers

**Decision:** provider registry with tiers — mini (default generator + the single pre-call),
strong (agentic path only), local (Ollama). OpenAI adapter serves **Azure OpenAI via config
flags** (endpoint/api-version/deployment): EU residency is a config change, not a rebuild.
Fallback chain with a three-line circuit breaker; the active provider is visible per answer.
**Why:** in RAG the knowledge comes from retrieval, not model size — route simple queries to
small models, reserve big ones for the corrective loop.
**Rejected:** one hardcoded provider (kills the enterprise story) · a proxy service for
provider abstraction (one more container without demo value) · local generation as default
(fine in production on GPU serving; on laptops it costs the latency story — it remains the
confidential/offline path).

**Data-class policy is CODE, fail-closed:** `confidential` collections route EVERY LLM call
of the request path (condense/route, grading, generation) to local providers — the user
question itself may be confidential, not just documents. Unknown collection/class ⇒
confidential. No cloud fallback on local failure: wrongly-local costs quality, wrongly-cloud
is an incident. One request targets exactly ONE collection, so the class is known before the
first LLM call.

## Data flows & privacy floors (Phase 0)

Every byte that can leave the host, by data class. "Local" = this machine,
in-process or via the host's Ollama; nothing else sees the content. Test data is
governed exactly like production data — no control below references the fact
that the demo corpus is synthetic.

| Flow | public / internal | confidential |
|---|---|---|
| Retrieval (Postgres FTS + pgvector), embeddings, reranker | local | local |
| Request path: condense/route, grading, generation | cloud allowed (OpenAI/Azure, no-training API terms) or local | **local only, no fallback** |
| Eval: answer generation | same matrix as the request path | **local only** |
| Eval: LLM judge (faithfulness) | cloud judge allowed (Anthropic, commercial no-training terms) or local | **local judge only** |
| Model weights download (BGE-M3, reranker) | HF, telemetry disabled, revision pinnable | same |
| `generation: extractive` collections | **no LLM call at all** — passages are the answer | same |

Three rules generate this table:

1. **One matrix, no side channels.** `judge_provider_kinds` ≡ `allowed_provider_kinds`
   — evaluation obeys the same gate as generation (the judge reads the same
   answer + contexts). Closed in Phase 0: previously the eval judge was built
   directly on a cloud provider, bypassing the policy.
2. **Free tiers are banned for every class** — they pay with training rights.
   The former free-tier fallback/judge (Gemini) is removed entirely; the
   fallback mechanism itself stays and is exercised through fake providers.
3. **The hard floor:** `confidential` never reaches the `cloud` kind. The
   provider matrix knows a third kind `sovereign` (an EU/EEA-hosted provider
   under contractual no-training terms — e.g. a rented sovereign inference endpoint,
   attached via the OpenAI adapter's `base_url` + `kind='sovereign'`); it is a
   documented slot, none is configured by default. The `offline` profile is
   stricter than both: local only, for everything.

Environment separation is config, not code: each environment carries its own
`.env` (demo keys exist only in demo), the container image disables HF
telemetry globally, and `EMBEDDING_MODEL_REVISION`/`RERANKER_MODEL_REVISION`
pin exact model commits (supply-chain hygiene). The startup gate that refuses
demo state in production (`deployment_mode`) is built — see "Production gate
& rate limiting" below.

## Orchestration

**Decision:** router-first. ONE structured mini-call returns `{standalone_query, route}`
(condensation is a no-op on empty history). Direct path: retrieve → stream. Agentic path:
corrective loop (grade → rewrite → re-retrieve → generate → groundedness) as a LangGraph
state machine with hard guards (max 2 iterations + token budget; on exhaustion: best
available answer, never an error).
**Why:** agentic depth costs multiples in tokens and latency [benchmark-reported]; the
router is the cheapest pattern with the highest return because it CONTROLS that cost. A
state machine with conditional edges is testable and bounded — a free agent is neither.
Two serial pre-calls before the first token would structurally endanger TTFT; hence the
combined call. The agentic path generates inside the loop (non-streamed) because streaming
a possibly-discarded generation to the user would be wrong; the validated answer is then
chunk-streamed.
**Rejected:** free ReAct agent (unbounded) · multi-agent frameworks (overkill for one
retrieval tool) · groundedness check on the direct path (latency tax on the 80% case).

## Grounding

Inline citation markers `[S#]`, POST-validated against the actually provided sources —
hallucinated markers are stripped, every citation resolves to doc + section. Without
sufficient evidence the contract is an explicit refusal (its own golden-set case). The
eval judge is a DIFFERENT model family than every generator (self-preference bias —
this includes fine-tune backbones), and which judge sees which answer is decided by
the data-class policy, never by convenience (see "Data flows & privacy floors").

## Security

| Threat | Countermeasure | Proof |
|---|---|---|
| Cross-department data leak | Postgres RLS: `FORCE ROW LEVEL SECURITY`, separate `rag_app` role (owner bypass trap), `set_config(..., is_local)` per transaction (pool leak trap); unset context matches zero rows | integration test |
| Permission bypass via cache | The answer cache sits IN FRONT of RLS ⇒ its key carries tenant + department + data class + corpus/embedding version; condensed question, never the raw follow-up | unit test |
| Confidential data egress | Deterministic data-class routing, fail-closed, whole request path, no cloud fallback | unit test |
| Indirect prompt injection | Demarcation blocks + instruction hierarchy ("document content is DATA"); generation path has NO tools; prepared injection document in the corpus | golden-set case |
| Stored XSS via documents | UI renders exclusively via `textContent` (markup stays inert); injection doc includes a markup payload | golden-set case |
| Residual data after deletion | Deletion cascade: chunks+vectors (the vector still encodes the content) → cached answers → session messages citing the doc (deterministic via citation anchors) → feedback rows; `corpus_version` bump invalidates stale cache keys | integration test in CI |
| Confidential egress via evaluation | The eval judge is selected through the SAME data-class gate as generation (`judge_provider_kinds`); confidential answers+contexts are judged only locally, a configured cloud key cannot widen the flow | unit test |
| Token theft via XSS | BFF pattern (A6): tokens never reach the browser — only an opaque `HttpOnly` session cookie; unsafe methods additionally require the per-session CSRF token | unit + Playwright |

Deliberately NOT built (documented production concern): LLM-based injection
classifiers (probabilistic — the deterministic layers are the design answer; a
classifier would be defense-in-depth).

## Identity (A6)

**Auth port.** ONE dependency (`api.require_scope`) over pluggable adapters in
`auth.py`: `StaticKeyAuth` (the demo keys, verbatim — the former
`security.py`) and `OidcAuth`. The adapter is selected ONCE in the lifespan
from the `auth_backend` SETTING — runtime behavior never branches on
`deployment_mode` (E2); production enforces oidc only through the gate (R3).
Every adapter yields the same `QueryScope` (tenant/user/department), so RLS
and the cache key are untouched by construction. The 401 path is shared and
credential-free: `build_auth_failure()` has no parameter, no presented
key/token/code can reach audit or logs.

**Token validation.** IdP-agnostic: Discovery + JWKS only (`PyJWKClient`,
called via `asyncio.to_thread` — it blocks only on a JWKS cache miss).
`jwt.decode` with an explicit `["RS256", "ES256"]` allowlist (the alg-none
class is structurally excluded), issuer and audience pinned, 30 s exp/nbf
leeway. Issuer consistency (the classic container↔browser pitfall): the iss
claim is validated against the PUBLIC issuer URL, always; only backchannel
FETCHES (discovery, token endpoint, JWKS) are rewritten by prefix
replacement to `OIDC_INTERNAL_ISSUER_URL`. Claims are minimized: `user_id` =
`sub` (pseudonymous), department from a configurable claim, missing claim →
403 fail-closed — never a default department.

**BFF + CSRF (E4, IETF BCP "OAuth 2.0 for Browser-Based Apps").** Tokens
never reach the browser: the API runs the authorization-code flow (state +
PKCE S256 from the stdlib, single-use flow state in Redis, confidential
client on the backchannel) and hands the browser an opaque
`HttpOnly; SameSite=Lax` cookie backed by a Redis auth session
(user-bound keys, TTL 8 h default). The cookie re-activates the CSRF attack
class, so cookie-authenticated unsafe methods require the per-session
`X-CSRF-Token` (constant-time compare); header auth (Bearer, X-API-Key) is
CSRF-immune and exempt. The cookie value is `{user_id}:{sid}` — the
user-bound key is reconstructable without a per-request SCAN; the sid alone
is the secret.
**Rejected:** tokens in the browser (localStorage/SPA flow — XSS-stealable,
exactly what the BFF prevents) · a vendor IdP SDK (locks the backend to one
IdP; Discovery+JWKS is the whole contract).

**Demo bridge — a deliberate trade-off.** dex `staticPasswords` cannot carry
custom claims, so the demo needs an email→department bridge
(`OIDC_DEMO_DEPARTMENT_MAP`, JSON, parsed fail-closed at startup; email is
read for the lookup, never stored). Alternatives were rejected: teaching dex
custom claims (not supported for static users; bending the demo IdP is the
wrong layer) and defaulting the department (violates fail-closed). The
bridge is confined to the demo by readiness rule R10: a configured map in
production is a startup-blocking finding.

**dex in the stack.** Pinned image, config-file IdP, demo-only credentials
on the fictional domain; the api service does NOT depend on it — the
static-key default starts without dex. Operated IdPs (Zitadel/Keycloak) are
the production rungs of the same ladder.

## Production gate & rate limiting

**Decision (E2):** `deployment_mode` (demo | production) is its OWN setting,
orthogonal to `rag_profile` — the profile routes providers, the mode hardens
operations, and offline+production must be possible.
**Rejected:** a third `rag_profile` value (it would conflate two independent axes).

`production` runs a fail-closed startup gate (`readiness.py`, a pure function)
BEFORE any connection is opened; every finding is logged with rule, message and
remedy, then startup aborts. Unknown values for `deployment_mode`/`auth_backend`
abort startup in EVERY mode (a typo like `prod` must never silently run as demo).
The rules:

| Rule | Check |
|---|---|
| R1 | API keys are set, non-default, pairwise distinct |
| R2 | every chained provider has a known kind (cloud/sovereign/local) and is not on the free-tier denylist (E1: free tiers pay with training rights) |
| R3 | `auth_backend=oidc` required — satisfiable since A6 (see "Identity (A6)") |
| R4 | no fake embeddings/reranker (`reranker=off` is allowed: a latency lever, not fake data) |
| R5 | `HF_HUB_DISABLE_TELEMETRY=1` |
| R6 | — intentionally absent: the judge-cloud-override flag it would check was never built (stricter than planned); the number is not reused |
| R7 | audit trail available — arrives with A4 (a build-truth constant, not a config flag) |
| R8 | rate limiting enabled |
| R9 | regression guard above the policy code floor: no confidential collection may see the cloud kind |
| R10 | the OIDC demo department bridge (`OIDC_DEMO_DEPARTMENT_MAP`) must be empty — demo states stay out of production, same logic as R1 |

Consequence: with A6 every rule is satisfiable — a **fully hardened
configuration yields an empty finding list**, proven by a unit test plus a
production smoke start in the integration layer. "Startable" means the
software layer; operator duties remain. The refusal tests
with the full finding list stay in place.

**Rate limiting** is a per-identity token bucket (`ratelimit.py`): O(1) state
per identity, burst as an explicit capacity, the arithmetic a pure function
(`now` is always a parameter). Two route classes — `query` (POST /query,
/feedback: reranker + LLM cost) and `ingest` (POST /ingest, DELETE
/documents/*: embedding + write path); all GET endpoints including
health/ready stay unlimited (orchestration must always reach them). Per
identity, not per IP — the system runs behind proxies. Server-side size caps
reject oversized questions and ingest payloads (decoded length) with 413.

Known, accepted race: the Redis adapter does GET → take() → SET without
atomicity, so parallel requests of the same identity can slightly
under-count. Documented instead of half-fixed; the Lua-script/atomic variant
is the named scaling step.

**Fail-open on limiter store errors** (deliberate exception to the fail-closed
guideline, which governs security gates): the limiter protects AVAILABILITY —
auth, RLS and data-class routing stay fail-closed. A Redis outage must not
turn a degradation into a full API outage, which is exactly what an attacker
would want. Operator alternative for stricter setups (documented, not built):
fail-closed in production mode only.

## Deletion concept

Deletion is specified as a CONCEPT — what is stored where, how it dies, and
when — not just as a cascade function. The enforced schedule:

| Data | Store | Mechanism | Deadline |
|---|---|---|---|
| Documents, chunks, vectors | Postgres | document cascade (`DeletionCascade.delete_document`) | on request |
| Cached answers citing a doc | Redis | cascade purge + `corpus_version` bump | on request; TTL 1 h |
| Session messages citing a doc | Redis | cascade redaction (citation anchors) | on request; TTL 24 h |
| Feedback rows citing a doc | Postgres | cascade delete | on request |
| A user's own feedback rows | Postgres | self-service `DELETE /me/data` | on request; retention cron (`FEEDBACK_RETENTION_DAYS`, default 180 d) |
| A user's own sessions | Redis | self-service `DELETE /me/data` | on request; TTL 24 h |
| Auth sessions (BFF) | Redis | logout + self-service `DELETE /me/data` | on request; TTL `AUTH_SESSION_TTL_S` (default 8 h) |
| Audit events (metadata, surrogate refs) | Postgres | own retention cron via `SECURITY DEFINER`; doc reference dies with the document cascade | `AUDIT_RETENTION_DAYS` (default 90 d; SQL floor 30 d) |

Two deletion scopes, deliberately distinct: the DOCUMENT cascade serves the
people the corpus is ABOUT (subjects — their data lives in documents); the
SELF-SERVICE endpoint serves the system's USERS (their trail: feedback and
sessions). The endpoint takes no user parameter — the scope comes from auth,
so nobody can name a foreign user; deletion on someone else's behalf is an
operator process, not an API surface. The answer cache needs no per-user
purge: its key is permission-scoped (`tenant:department`) and never carries a
user identifier (`cachekey.py`).

Retention runs as a daily worker cron through a `SECURITY DEFINER` function
(`db/init/03_retention.sql`): retention must sweep every tenant and
department, while the app role's RLS view is deliberately bound to one
request context — a narrowly-scoped, content-free function is the
least-privilege bridge, not a blanket RLS bypass.

Beyond the live system, the concept addresses the copies people forget:

- **Backups**: `make backup` produces on-demand dumps (custom format, plus a
  git-rev sidecar recording the config state), and restorability is PROVEN,
  not assumed: `scripts/restore_check.sh` (also a CI step and
  `make restore-check`) restores every dump path into a throwaway database
  and compares row counts. Everything beyond that is an operator duty with
  specified rules, not improvisation: bounded backup rotation limits
  residual copies, restores must replay deletions performed since the
  snapshot, WAL/PITR and off-site copies are deployment concerns, and
  crypto-shredding is the escalation for archives that cannot rotate
  (operator handbook follows in Phase 3).
- **Provider copies**: public/internal contexts sent to a cloud LLM
  are retained by the provider for a bounded window under its API terms (no
  training) and expire there.
  Confidential content structurally never reaches a provider (policy gate),
  and the offline profile removes egress entirely.
- **Audit trail**: built (A4). Access records stay
  evidential while deletion voids their reference: the audit stores only
  surrogate ids (`documents.audit_ref`) whose mapping dies with the document
  cascade, plus a content-free deletion receipt per cascade run and per
  `DELETE /me/data`. Audit rows themselves have their own retention cron
  (`AUDIT_RETENTION_DAYS`); the full design is in "Audit trail (A4)" below.

## Audit trail (A4)

**What it records:** revision-grade ACCESS metadata, nothing else. The /query
event states WHO (tenant/user/department) WHEN got WHICH documents — as
surrogates — into the answer context of WHICH collection/data class:
`decision='context_served'`, or an honest `'no_context'` when retrieval found
nothing relevant. It is written AFTER context assembly and BEFORE the first
token frame on every route (the direct path only starts streaming after that
point). Answer QUALITY (citations, refusal wording, judge scores) is
telemetry and lives in trace/eval — it is not an access fact and stays out.
Further events: `ingest` (accepted, deliberately without doc_id/title),
`delete` (receipts, below), `policy_denial`, `rate_limited`, `auth_failure`
(no scope, and the builder signature has no parameter for the presented key).

**Surrogates instead of speaking ids (E3):** the audit stores only
`documents.audit_ref` values (random UUIDs). The mapping to the real
document lives in the document row and dies with the deletion cascade — the
log stays evidential, the reference becomes meaningless. Each cascade (and
each `DELETE /me/data`) writes a content-free receipt with counts.
A receipt failure never aborts a deletion (deletion takes precedence over the
audit duty — the failure is logged and counted instead); deliberate trade-off.

**Grants instead of RLS:** `audit_events` has NO row-level security —
`rag_app` simply has no SELECT. Reading is structurally impossible for the
app role, not just filtered; read access exists only via the NOLOGIN role
`rag_audit_reader` (operators attach a login) or `rag_owner`. INSERT-only
plus no UPDATE/DELETE grants makes the log append-only as a database
property (integration-tested), and `auth_failure` rows need no scope. "No
field can carry content text" is DB-enforced: closed CHECK sets for
event_type/decision/data_class, length-capped id fields, int count columns,
deliberately no jsonb. The Python event model pins the same closed sets and
its full field inventory in unit tests — code/schema drift fails fast.

**Fail-closed via SETTING, not mode branch (E2):** `audit_fail_closed=true`
makes the confidential /query access event a hard precondition — the write
is awaited, and its failure aborts the stream with a structured error frame
before any token. Runtime behavior never branches on `deployment_mode`; the
E3 demand "production ⇒ fail-closed" is enforced by readiness rule R7
(`audit_enabled` AND `audit_fail_closed`, same construction as
R8/rate limiting). `audit_enabled=false` is the demo off switch — and an R7
finding in production. Retention runs as a second worker cron through
`purge_expired_audit` (`SECURITY DEFINER`), which enforces a 30-day
floor server-side (`GREATEST`) — even a compromised app call with
`interval '0'` cannot empty the log.

**Phase 2 boundary (documented):** the ingestion worker writes no second
event per job — the accepted `ingest` event at the API is the access record.

## Observability (A5)

**Metrics** (`prometheus_client` — the single new dependency, E5): a
`Metrics` instance with its own `CollectorRegistry` hangs on the app state
(the library's global default registry breaks under pytest). Eight metrics:
`rag_requests_total` and `rag_request_seconds` (histogram, CPU-realistic
buckets up to 300 s) by route class from the HTTP middleware;
`rag_provider_calls_total` (provider/kind/outcome — ok | fallback | error)
in the provider registry; `rag_cache_events_total` (hit | miss);
`rag_collection_queries_total` (V4 counter — label clamped to registry
names, `unknown` otherwise); `rag_policy_denials_total`;
`rag_rate_limited_total`; `rag_audit_write_failures_total`.

**Label hygiene is a tested rule:** never user_id, never tenant, never free
text in labels — cardinality is never user-controlled. Tests plant a marker
in a question and assert it never reaches the exposition.

**Honest TTFB semantics:** for SSE responses the middleware measures time to
response START (~TTFB), not stream end — full answer latency remains a
trace/eval SLO concern. `GET /metrics` is auth-free (same A3 decision as
health/ready); it contains nothing user-related, and operators do not expose
it publicly (reverse proxy / network policy — operator handbook in Phase 3).
Boundary: /metrics measures the API process; the worker has no metrics in
Phase 2, its crons log counts.

**Secrets via `*_FILE`** (Docker secrets pattern): `NAME_FILE=/run/secrets/…`
fills a whitelisted setting only when the variable itself is unset —
env/.env always win. A configured but unreadable file aborts startup
(fail-closed). `.env` stays the development path; docker-compose.yml is
unchanged (wiring secrets is operator deployment).

**Structured logs:** `LOG_JSON=true` switches the root logger to a stdlib
JSON formatter (ts, level, logger, message; exceptions contribute only the
class name — the content ban holds in logs too). Default off: the current
format stays byte-identical.

**Backup/restore:** see the deletion concept's backup paragraph —
`make backup` dumps with a git-rev sidecar, and `scripts/restore_check.sh`
(CI step + `make restore-check`) proves restorability by restoring into a
throwaway database and comparing row counts.

## Evaluation

Golden set designed BEFORE the corpus (every feature has a proving case: error codes,
paraphrases, tables, compounds, multi-doc, RLS, refusal, injection). Retrieval metrics
(Recall@5, MRR@5 — anchored on doc+section, chunking-independent) are measured separately
from answer quality (deterministic contains/refusal checks + optional LLM judge). The
retrieval ablation is deterministic reporting; the answer layer (`run_eval.py --answers`)
is the red/green gate — it exits 1 on a failed check or a total-latency p95 SLO breach
(per run mode, native/MPS vs container/CPU). The smoke set is ~22 questions, the curated
core set 37 (32 retrieval-anchored) — both directional measurements with per-case error
analysis, not significance statistics; in production the set grows from user feedback
(the 👍/👎 endpoint exists for exactly that).
