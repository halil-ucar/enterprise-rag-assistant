"""Evaluation harness — measured, not claimed.

Two layers, separately measured (best practice):
  1. RETRIEVAL (default, 0 LLM cost): ablation dense → hybrid → hybrid+rerank
     over the golden set; Recall@5 + MRR against doc+section anchors
     (chunking-independent — chunk ids change with every strategy, sections don't).
  2. ANSWERS (--answers): full pipeline per question (agentic for hard ones),
     deterministic contains/refusal/injection checks, citation validity rate,
     latency p50/p95 + total-latency SLO assertion per run mode.
  3. JUDGE (--judge, requires --answers): faithfulness per the RAGAS definition
     (claim decomposition + NLI verdicts, see rag_assistant/judge.py), judged
     against the exact context the generator saw. The judge is POLICY-GATED per
     data class (rag_assistant/judge_select.py): cloud judge (Anthropic) only for
     public/internal, local judge for confidential — evaluation obeys the same
     data-class matrix as generation; judge ≠ generator holds for every pairing.
     Reported, never asserted — LLM-judge scores fluctuate between runs, so they
     stay a report metric, not a CI gate (determinism-first invariant).

Output: eval/runs/<ts>.json + a Markdown table for the README.

The RETRIEVAL ablation is deterministic reporting (always exit 0). Only the
--answers path is a red/green gate: exit 1 when a deterministic check fails or
the total-latency p95 SLO is exceeded. Every report carries an index
fingerprint (doc/chunk counts per demo scope) so a stale or cross-tier-
contaminated index is visible instead of silently changing the numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from rag_assistant.citations import extractive_answer, is_refusal, validate_citations
from rag_assistant.config import get_registry, get_settings
from rag_assistant.domain import Candidate, DataClass, QueryScope, Trace
from rag_assistant.embeddings import build_embedder
from rag_assistant.judge import FaithfulnessJudge, JudgeError
from rag_assistant.judge_select import select_judge_provider
from rag_assistant.llm.registry import ProviderRegistry
from rag_assistant.pipeline import RagPipeline
from rag_assistant.policy import effective_data_class
from rag_assistant.rerankers import build_reranker
from rag_assistant.store.pg import PgBackend, PgRetriever, PgStore, vec_literal

GOLDEN = Path(__file__).parent.parent / "seed" / "golden_set.yaml"
RUNS = Path(__file__).parent / "runs"


def pctl(vals: list[float], q: int) -> float | None:
    return round(statistics.quantiles(vals, n=100)[q - 1], 1) if len(vals) >= 2 else None


# Total-latency SLO per run mode (native/MPS vs container/CPU — the CPU reranker
# alone costs seconds; asserting dev SLOs in container mode would always fail).
# Only total_ms is a gate: the pipeline is called directly here (not via SSE),
# so TTFT is never populated in eval — asserting it would test nothing.
SLO = {
    "dev": {"total_ms": 8000},
    "container": {"total_ms": 15000},
}

USERS = {
    "anna": QueryScope(tenant="nordfels", user_id="anna", department="it"),
    "ben": QueryScope(tenant="nordfels", user_id="ben", department="hr"),
}


def scope_for(q: dict) -> QueryScope:
    return USERS[q.get("user", "anna")]


def hit_rank(cands: list[Candidate], q: dict, k: int = 5) -> int | None:
    """1-based rank of the first candidate matching the doc + section anchor.

    Supports expected_doc (single) and expected_doc_any (FAQ/prose duplicates —
    any listed doc counts). Section substring optional per doc-any question.
    """
    want_docs = [q["expected_doc"]] if q.get("expected_doc") else q.get("expected_doc_any", [])
    want_sec = q.get("expected_section_contains", "").lower()
    for i, c in enumerate(cands[:k], start=1):
        if c.doc_id in want_docs and want_sec in c.section_path.lower():
            return i
    return None


def is_retrieval_q(q: dict) -> bool:
    return bool(q.get("expected_doc") or q.get("expected_doc_any"))


async def index_fingerprint(store: PgStore) -> dict:
    """Doc/chunk counts as each demo scope sees them (RLS-scoped). Recorded in
    every report so a stale or cross-tier-contaminated index (e.g. smoke seeded
    on top of core) is visible in the artifact instead of silently shifting the
    numbers — eval-core and eval-full are the SAME command, differing only in
    which corpus was last seeded."""
    fp: dict[str, Any] = {}
    for name, scope in USERS.items():
        s = await store.stats(scope)
        fp[name] = {
            "documents": s["documents"],
            "chunks": s["chunks"],
            "per_collection": {
                r["collection"]: {"documents": r["documents"], "chunks": r["chunks"]}
                for r in s["per_collection"]
            },
        }
    return fp


async def dense_only(
    backend: PgBackend, scope: QueryScope, collection: str, qvec, k: int, emb_v: int = 1
):
    """Ablation baseline: dense leg alone — measured LIKE-FOR-LIKE with the dense
    leg inside hybrid. Same ANN search params (ef_search + iterative scan) and
    the same embedding_version filter that PgRetriever.search applies; otherwise
    a default ef_search under a filtered HNSW scan handicaps dense and flatters
    the hybrid story with a search-parameter artifact instead of a real recall
    gap."""
    sql = """
    SELECT c.id, c.doc_id, d.title AS doc_title, c.section_path, c.content
    FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
    WHERE c.collection = %(collection)s AND c.embedding_version = %(emb_v)s
    ORDER BY c.embedding <=> %(qvec)s::vector
    LIMIT %(k)s
    """
    async with backend.scoped_tx(scope) as conn:
        await conn.execute("SET LOCAL hnsw.ef_search = 60")
        if backend.supports_iterative_scan:
            await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        cur = await conn.execute(
            sql, {"collection": collection, "qvec": vec_literal(qvec), "k": k, "emb_v": emb_v}
        )
        rows = await cur.fetchall()
    return [
        Candidate(chunk_id=str(r[0]), doc_id=r[1], doc_title=r[2], section_path=r[3], content=r[4])
        for r in rows
    ]


def _summarize(bucket: dict[str, Any]) -> dict[str, Any]:
    n = bucket["n"]
    return {
        "recall@5": round(bucket["hits"] / n, 3) if n else 0.0,
        "mrr": round(sum(bucket["rr"]) / n, 3) if n else 0.0,
        "n": n,
    }


async def run_retrieval_matrix(backend, retriever, embedder, reranker, questions) -> dict:
    """Ablation dense → hybrid → hybrid+rerank, with a per-CATEGORY breakdown
    (aggregate MRR hides *where* a configuration wins or loses) and retrieval
    latency per config. No LLM cost."""
    # Only claim a rerank row when a reranker actually ran. With RERANKER_BACKEND=off
    # (a documented setting) the row would otherwise duplicate hybrid's numbers
    # under the rerank heading — an unmeasured number in a "measured" table.
    configs = ["dense", "hybrid"] + (["hybrid+rerank"] if reranker else [])
    agg: dict[str, dict[str, Any]] = {c: {"hits": 0, "rr": [], "n": 0} for c in configs}
    by_cat: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in configs}
    latency: dict[str, list[float]] = {c: [] for c in configs}

    for q in questions:
        if not is_retrieval_q(q):
            continue
        scope = scope_for(q)
        collection = q.get("collection", "handbuecher")
        cat = q.get("category", "?")
        emb_v = retriever._emb_version(collection)
        qvec = (await embedder.embed([q["question"]]))[0]
        for config in configs:
            t0 = time.perf_counter()
            if config == "dense":
                cands = await dense_only(backend, scope, collection, qvec, 30, emb_v)
            else:
                cands = await retriever.search(scope, collection, q["question"], qvec, top_k=30)
                if config == "hybrid+rerank" and reranker and cands:
                    scores = await reranker.rerank(q["question"], [c.content for c in cands])
                    for c, s in zip(cands, scores, strict=True):
                        c.rerank_score = s
                    cands.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)
            latency[config].append((time.perf_counter() - t0) * 1000)
            rank = hit_rank(cands, q)
            rr = 1.0 / rank if rank else 0.0
            for bucket in (
                agg[config],
                by_cat[config].setdefault(cat, {"hits": 0, "rr": [], "n": 0}),
            ):
                bucket["n"] += 1
                bucket["hits"] += 1 if rank else 0
                bucket["rr"].append(rr)

    return {
        "overall": {c: _summarize(agg[c]) for c in configs},
        "by_category": {c: {cat: _summarize(b) for cat, b in by_cat[c].items()} for c in configs},
        "latency_ms": {
            c: {"p50": pctl(latency[c], 50), "p95": pctl(latency[c], 95)} for c in configs
        },
    }


async def run_answers(
    pipeline: RagPipeline,
    registry,
    questions,
    judges: dict[DataClass, FaithfulnessJudge | None] | None = None,
) -> dict:
    rows = []
    ttfts, totals = [], []
    for q in questions:
        scope = scope_for(q)
        collection = q.get("collection", "handbuecher")
        data_class = effective_data_class(registry.get(collection))
        # Judge selection is per data class (policy-gated); None = judging was
        # requested but no permitted judge is configured for this class.
        judge = judges.get(data_class) if judges is not None else None
        trace = Trace(collection=collection, data_class=data_class)
        t0 = time.perf_counter()
        try:
            cfg = registry.get(collection)
            if cfg is not None and cfg.generation == "extractive":
                # Extractive collections: no LLM call on the whole path (mirrors
                # api.query) — the passages themselves are the answer.
                candidates = await pipeline.retrieve(scope, collection, q["question"], trace)
                bundle = pipeline.build_context(candidates, trace)
                text, _ = extractive_answer(bundle)
                cits_valid = True
                generator_model = "extractive"
            elif q.get("hard"):
                answer, bundle = await pipeline.run_agentic(
                    scope, collection, data_class, q["question"], q["question"], trace
                )
                text = answer.text
                cits_valid = answer.citations_valid
                generator_model = trace.model
            else:
                candidates = await pipeline.retrieve(scope, collection, q["question"], trace)
                bundle = pipeline.build_context(candidates, trace)
                result, provider = await pipeline.registry.complete(
                    "mini",
                    data_class,
                    pipeline.answer_messages(q["question"], bundle),
                    max_tokens=600,
                )
                text, _, cits_valid = validate_citations(result.text, bundle)
                generator_model = provider.model
        except Exception as exc:  # noqa: BLE001 — one flaky generation must not lose the run
            # A provider timeout/outage on ONE question (e.g. a cold qwen3 on CPU
            # for a confidential answer) marks that case failed and moves on, so
            # retrieval + every other answer + faithfulness still get written.
            rows.append(
                {
                    "id": q["id"],
                    "category": q["category"],
                    "ok": False,
                    "refused": False,
                    "citations_valid": True,  # no answer emitted → not a citation defect
                    "generation_error": f"{type(exc).__name__}: {exc}"[:200],
                    "notes": f"GENERATION FAILED: {type(exc).__name__}",
                    "total_ms": None,
                }
            )
            continue
        total_ms = (time.perf_counter() - t0) * 1000
        totals.append(total_ms)
        if trace.ttft_ms:
            ttfts.append(trace.ttft_ms)

        refused = is_refusal(text)
        ok = True
        notes = []
        if q.get("expect_refusal"):
            ok = refused
            notes.append("refusal" if ok else "MISSING refusal")
        if "expected_answer_contains" in q:
            missing = [s for s in q["expected_answer_contains"] if s.lower() not in text.lower()]
            if missing:
                ok = False
                notes.append(f"missing: {missing}")
        for bad in q.get("must_not_contain", []):
            if bad.lower() in text.lower():
                ok = False
                notes.append(f"CONTAINS FORBIDDEN: {bad}")
        if not cits_valid:
            notes.append("INVALID citation marker")
        row = {
            "id": q["id"],
            "category": q["category"],
            "ok": ok,
            "refused": refused,
            "citations_valid": cits_valid,
            "notes": "; ".join(notes),
            "total_ms": round(total_ms),
        }
        if judges is not None and judge is None:
            # Fail-closed leftover: never grade with a disallowed provider —
            # the case is reported as unjudged instead.
            row["judge_skipped"] = "no_judge_for_class"
        if judge:
            try:
                fr = await judge.score_answer(
                    q["question"], text, bundle.text, generator_model=generator_model
                )
                row["faithfulness"] = round(fr.score, 3) if fr.score is not None else None
                row["claims"] = len(fr.verdicts)
                if fr.skipped:
                    row["judge_skipped"] = fr.skipped
                # Audit trail (eval/runs is gitignored): answer + per-claim verdict
                # and reason, so a 0.2 can be inspected instead of taken on faith.
                if fr.verdicts:
                    row["faithfulness_detail"] = {
                        "answer": text,
                        "verdicts": [v.model_dump() for v in fr.verdicts],
                    }
            except JudgeError as exc:
                # A judge outage must not crash the run (or lose the retrieval
                # ablation): record it distinctly and keep going.
                row["faithfulness"] = None
                row["judge_skipped"] = "judge_error"
                row["judge_error"] = str(exc)
        rows.append(row)

    def p(vals, q):
        return round(statistics.quantiles(vals, n=100)[q - 1]) if len(vals) >= 2 else None

    # Citation validity over answered cases: a refusal has no markers (would
    # inflate the rate) and a generation error produced no answer at all.
    answered = [r for r in rows if not r["refused"] and not r.get("generation_error")]
    report = {
        "cases": rows,
        "passed": sum(1 for r in rows if r["ok"]),
        "total": len(rows),
        "generation_errors": sum(1 for r in rows if r.get("generation_error")),
        "citation_validity": {
            "valid": sum(1 for r in answered if r["citations_valid"]),
            "n": len(answered),
            "rate": round(sum(1 for r in answered if r["citations_valid"]) / len(answered), 3)
            if answered
            else None,
        },
        "latency": {
            "ttft_p50": p(ttfts, 50),
            "ttft_p95": p(ttfts, 95),
            "total_p50": p(totals, 50),
            "total_p95": p(totals, 95),
        },
    }
    if judges is not None:
        scored = [r["faithfulness"] for r in rows if r.get("faithfulness") is not None]
        skips = Counter(r["judge_skipped"] for r in rows if r.get("judge_skipped"))
        report["faithfulness"] = {
            "mean": round(statistics.mean(scored), 3) if scored else None,
            "n_scored": len(scored),
            "n_skipped": sum(skips.values()),
            # e.g. {"refusal": 4, "judge_error": 1}: transient errors stay visible
            # and never masquerade as legitimate N/A (honesty invariant).
            "skipped_by_reason": dict(skips),
            # One judge per data class (policy-gated) — report the full mapping.
            "judge_models": {
                dc.value: (j.provider.model if j else None) for dc, j in judges.items()
            },
        }
    return report


def markdown_table(overall: dict) -> str:
    # MRR@5: hit_rank scans only the top 5 (a hit at rank 6 scores 0), so the
    # metric is truncated-MRR — labeled precisely to match what is computed.
    lines = ["| Konfiguration | Recall@5 | MRR@5 | n |", "|---|---|---|---|"]
    for config, m in overall.items():
        lines.append(f"| {config} | {m['recall@5']:.3f} | {m['mrr']:.3f} | {m['n']} |")
    return "\n".join(lines)


def category_table(by_category: dict) -> str:
    """MRR per category across configs — shows WHERE hybrid/rerank help or hurt."""
    cats = sorted({c for cfg in by_category.values() for c in cfg})
    lines = ["| Kategorie | dense | hybrid | hybrid+rerank | n |", "|---|---|---|---|---|"]
    for cat in cats:
        row = [cat]
        n = 0
        for cfg in ("dense", "hybrid", "hybrid+rerank"):
            m = by_category.get(cfg, {}).get(cat)
            row.append(f"{m['mrr']:.3f}" if m else "–")
            n = m["n"] if m else n
        row.append(str(n))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--answers", action="store_true", help="also run generation + checks")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="faithfulness via policy-gated judges: cloud judge for public/internal, "
        "local judge for confidential (RAGAS definition, judge != generator)",
    )
    parser.add_argument(
        "--skip-retrieval",
        action="store_true",
        help="skip the retrieval ablation (deterministic; reuse prior numbers) and go straight to answers",
    )
    parser.add_argument(
        "--golden",
        default=str(GOLDEN),
        help="golden set yaml (default smoke; pass golden_set_core.yaml for the core)",
    )
    args = parser.parse_args()
    if args.judge and not args.answers:
        parser.error("--judge requires --answers (it grades generated answers)")
    if args.skip_retrieval and not args.answers:
        parser.error("--skip-retrieval only makes sense with --answers (nothing else would run)")

    settings = get_settings()
    judges: dict[DataClass, FaithfulnessJudge | None] | None = None
    if args.judge:
        # One judge per data class, selected through the SAME policy gate as
        # generation (E1/Phase-0): confidential is judged locally, always —
        # a cloud key must never widen what evaluation may see.
        judges = {}
        for dc in DataClass:
            provider = select_judge_provider(dc, settings)
            judges[dc] = FaithfulnessJudge(provider) if provider else None
        print("── judges (policy-gated per data class) ──")
        for dc, j in judges.items():
            model = f"{j.provider.name}:{j.provider.model}" if j else "none configured"
            print(f"  {dc.value:12s} → {model}")
        print()
    registry = get_registry()
    questions = yaml.safe_load(Path(args.golden).read_text(encoding="utf-8"))["questions"]

    backend = PgBackend(settings.database_url, rrf_k=settings.rrf_k)
    await backend.open()
    retriever = PgRetriever(
        backend, versions={n: c.embedding_version for n, c in registry.collections.items()}
    )
    store = PgStore(backend)
    embedder = build_embedder(settings)
    reranker = build_reranker(settings)

    fingerprint = await index_fingerprint(store)
    report: dict[str, Any] = {
        "run_mode": settings.run_mode,
        "profile": settings.rag_profile,
        "embeddings": settings.embeddings_backend,
        "reranker": settings.reranker_backend,
        "golden": Path(args.golden).name,
        "index": fingerprint,
    }
    print("── index fingerprint (RLS-scoped, detects stale/contaminated index) ──")
    for user, fp in fingerprint.items():
        print(
            f"  {user:5s} sees {fp['documents']} docs / {fp['chunks']} chunks {fp['per_collection']}"
        )
    print()

    if args.skip_retrieval:
        print("── retrieval ablation skipped (--skip-retrieval; deterministic, reuse prior run) ──")
    else:
        print("── retrieval ablation ──")
        matrix = await run_retrieval_matrix(backend, retriever, embedder, reranker, questions)
        report["retrieval"] = matrix
        print(markdown_table(matrix["overall"]))
        print("\n── per category (MRR) ──")
        print(category_table(matrix["by_category"]))
        print("\n── retrieval latency (ms) ──")
        for cfg, lat in matrix["latency_ms"].items():
            print(f"  {cfg:14s} p50={lat['p50']} p95={lat['p95']}")

    exit_code = 0
    if args.answers:
        print("\n── answer quality + latency ──")
        providers = ProviderRegistry(settings)
        pipeline = RagPipeline(providers, retriever, embedder, reranker, settings)
        answers = await run_answers(pipeline, registry, questions, judges=judges)
        report["answers"] = answers
        for row in answers["cases"]:
            if row.get("generation_error"):
                print(f"ERROR {row['id']} [{row['category']}] {row['notes']}")
                continue
            mark = "PASS" if row["ok"] else "FAIL"
            extra = ""
            if judges is not None:
                f = row.get("faithfulness")
                extra = (
                    f" faith={f:.3f}"
                    if f is not None
                    else f" faith=n/a({row.get('judge_skipped', '?')})"
                )
            print(
                f"{mark}  {row['id']} [{row['category']}] {row['notes']}"
                f"{extra} ({row['total_ms']} ms)"
            )
        print(f"\n{answers['passed']}/{answers['total']} checks passed")
        if answers["generation_errors"]:
            print(
                f"⚠ {answers['generation_errors']} generation error(s) — provider "
                f"timeout/outage, not a content failure (see cases[].generation_error)"
            )
        cv = answers["citation_validity"]
        print(f"citation validity: {cv['valid']}/{cv['n']} (rate={cv['rate']})")
        if judges is not None:
            fa = answers["faithfulness"]
            print(
                f"faithfulness: mean={fa['mean']} "
                f"(n_scored={fa['n_scored']}, n_skipped={fa['n_skipped']} "
                f"{fa['skipped_by_reason']}, judges={fa['judge_models']})"
            )
        print(f"latency: {answers['latency']}")

        slo = SLO.get(settings.run_mode, SLO["container"])
        p95 = answers["latency"]["total_p95"]
        if p95 and p95 > slo["total_ms"]:
            print(f"SLO FAIL: total p95 {p95}ms > {slo['total_ms']}ms ({settings.run_mode})")
            exit_code = 1
        if answers["passed"] < answers["total"]:
            exit_code = 1

    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport → {out}")
    await backend.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
