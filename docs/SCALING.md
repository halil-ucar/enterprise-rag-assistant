# Scaling plan — what changes, and what deliberately doesn't

The demo is a platform architecture at small operating size. Growth swaps adapters and
topology behind stable ports. Thresholds below are engineering orders of magnitude from
published vendor/community benchmarks — named and linked in the next section — not
measurements of this repo; treat them as planning figures.

## Measured boundary — and the on-demand scale test

**1. What this repo has measured, first-class:** everything measured here was measured
**up to 5,826 chunks, container/CPU** (the measured box under Stage 1 below). Above that
boundary this document is a **path, not a claim** — the stages describe which adapter
and topology moves are designed in, not results anyone has observed on this codebase.

**2. The benchmarks behind the planning figures** — named, so "vendor/community
benchmarks" is not an anonymous appeal to authority. All of these ran on foreign
hardware over foreign data: they give direction, never a promise for this workload.

- Supabase — [pgvector v0.5.0: Faster semantic search with HNSW indexes](https://supabase.com/blog/increase-performance-pgvector-hnsw)
- Neon — [pgvector: 30x Faster Index Build for your Vector Embeddings](https://neon.com/blog/pgvector-30x-faster-index-build-for-your-vector-embeddings)
- Crunchy Data — [HNSW Indexes with Postgres and pgvector](https://www.crunchydata.com/blog/hnsw-indexes-with-postgres-and-pgvector)
- Jonathan Katz — [An early look at HNSW performance with pgvector](https://jkatz05.com/post/postgres/pgvector-hnsw-performance/)

**3. The on-demand scale test (recipe, deliberately not executed):** when a concrete
target size needs a real number, measure it — on your hardware, with real embeddings:

- **Dataset:** the German subset of
  [Cohere/wikipedia-2023-11-embed-multilingual-v3](https://huggingface.co/datasets/Cohere/wikipedia-2023-11-embed-multilingual-v3)
  on Hugging Face, 100k–200k rows — real **1024-dimensional** embeddings, the same
  dimension BGE-M3 produces, so index geometry matches this system. Check the dataset's
  license terms at retrieval time.
- **Memory budget first:** rule of thumb `N × D × 4 bytes × ~2` (vectors + HNSW graph
  overhead) ⇒ **0.8–1.6 GB** for 100k–200k rows at D=1024 — feasible on an ordinary
  laptop; 1M rows ≈ 8 GB is not realistic there.
- **Keep the build in memory:** size `maintenance_work_mem` so the HNSW build never
  falls back to the disk-bound build path — a disk-path build measures the fallback,
  not the index, and the numbers are worthless.
- **Parallel build:** pgvector ≥ 0.6 builds HNSW with parallel workers — use it, and
  record the version with the result.
- **Rejected — synthetic random embeddings:** an HNSW graph's structure and recall
  behavior depend on the real geometry of real data; random vectors measure nothing
  about retrieval quality, and even the build time would be distorted (~1.5×
  [benchmark-reported]). Hence the real-dataset recipe above.

## Stage 1 — up to ~5–10M vectors (this system, as is)

- pgvector + HNSW on managed Postgres, tenant partitioning inside Postgres.
- Embeddings/reranker move from in-process to a small GPU service (TEI/vLLM) or a hosted
  reranker with a contract — same `EmbeddingProvider`/`Reranker` ports.
- Queue stays arq; workers scale horizontally (the ingestion contract doesn't change).

> **Measured here (not a vendor figure):** on the full 5 826-chunk eval index in
> container/CPU mode, dense retrieval runs ≈5 ms and hybrid ≈19 ms (p50), but the
> in-process CPU cross-encoder runs ≈28 s (p50) — it dominates end-to-end time and is
> exactly why the reranker is the first component to move to a GPU/hosted service here.
> Recall justifies keeping it: over that index the reranker lifts Recall@5 from 0.91
> (hybrid) to 0.97 (see the README eval).

## Stage 2 — 10–100M vectors

- Swap the `Retriever` adapter: Qdrant/Milvus (built-in quantization, typically 4–32×
  memory reduction; lower tail latencies at scale). The eval harness runs against BOTH
  backends before the switch — the golden set is the migration gate.
- Answer cache and session store stay Redis; corpus_version semantics unchanged.

## Stage 3 — >100M to billions

- Disk-based indexes (DiskANN-class), object-storage engines.
- Routing BEFORE search over the `tenant_id` geometry: enterprise corpora at this scale
  decompose into tenant partitions — the demo already has that data geometry.

## What does NOT change at any stage

- Schemas (tenant/lineage/ACL/class/version fields) and the ports
  (`Retriever.search(scope, collection, query, vector, top_k)`).
- The ACL model and where it's enforced (as close to the data as the engine allows).
- The deletion cascade semantics and its test.
- The eval harness — it gates every migration.
- Provider routing by data class.

## Latency engineering (why speed is designed, not hoped for)

Budget: retrieval single/double-digit ms (HNSW is sublinear) · rerank 50–300 ms on
GPU/hosted · the LLM dominates. Mechanisms: ONE pre-call before generation (combined
condense+route, skipped-condensation on first turn) · router keeps simple queries off the
expensive loop · loop budgets bound the worst case mathematically · streaming makes
perceived latency sub-second · tight context (top 3–5) keeps prefill short · scoped answer
cache · stateless API scales horizontally. Throughput ceilings in production are provider
rate limits → per-tenant budgets.
