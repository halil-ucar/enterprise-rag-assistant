"""The RAG pipeline: one pre-call (condense+route), retrieval with rerank,
direct streaming path, and the corrective (CRAG-style) loop as a LangGraph
state machine with hard guards.

Why ONE pre-call: two serial LLM calls before the first token would
structurally endanger the TTFT budget. The mini-call returns
{standalone_query, route} in a single round-trip; with empty history the
condensation part is a no-op.

Why a state machine instead of a free agent: fixed steps with conditional
edges are controllable, testable, and bounded (max iterations + token budget —
loop runaway is the documented agentic failure mode).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .citations import ContextBundle, assemble_context, validate_citations
from .config import Settings
from .domain import Answer, Candidate, DataClass, QueryScope, RouteDecision, Trace
from .llm.registry import ProviderRegistry
from .ports import EmbeddingProvider, LLMMessage, Reranker, Retriever
from .prompts import (
    CONDENSE_ROUTE_SYSTEM,
    GRADE_SYSTEM,
    GROUNDEDNESS_SYSTEM,
    REWRITE_SYSTEM,
    SYSTEM_ANSWER,
    answer_user_prompt,
    condense_route_prompt,
    grade_prompt,
    groundedness_prompt,
    rewrite_prompt,
)

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_loose(text: str) -> dict[str, Any]:
    """Tolerant JSON parse (strips code fences; empty dict on failure —
    callers treat failure as 'keep the safe default'). Valid-but-non-object
    JSON (a bare list/string/bool, seen from small local models in json mode)
    is failure too: every caller does .get() on the result."""
    try:
        data = json.loads(_JSON_FENCE.sub("", text).strip())
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class LoopState(TypedDict, total=False):
    """State of the corrective loop (module-level: langgraph resolves hints via globals)."""

    scope: QueryScope
    collection: str
    data_class: DataClass
    question: str
    query: str
    trace: Trace
    candidates: list[Candidate]
    bundle: ContextBundle
    sufficient: bool
    grade_reason: str
    answer_text: str
    grounded: bool
    iterations: int
    tokens_spent: int


class RagPipeline:
    def __init__(
        self,
        registry: ProviderRegistry,
        retriever: Retriever,
        embedder: EmbeddingProvider,
        reranker: Reranker | None,
        settings: Settings,
    ):
        self.registry = registry
        self.retriever = retriever
        self.embedder = embedder
        self.reranker = reranker
        self.settings = settings

    # ── pre-call: condense + route in ONE structured mini-call ────────────────
    async def condense_and_route(
        self, question: str, history: list[dict], data_class: DataClass, trace: Trace
    ) -> RouteDecision:
        history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
        t0 = time.perf_counter()
        result, provider = await self.registry.complete(
            "mini",
            data_class,
            [
                LLMMessage(role="system", content=CONDENSE_ROUTE_SYSTEM),
                LLMMessage(role="user", content=condense_route_prompt(history_text, question)),
            ],
            max_tokens=300,
            json_mode=True,
        )
        trace.add_stage("condense_route", (time.perf_counter() - t0) * 1000)
        trace.input_tokens += result.input_tokens
        trace.output_tokens += result.output_tokens
        data = parse_json_loose(result.text)
        route = data.get("route", "direct")
        return RouteDecision(
            standalone_query=data.get("standalone_query") or question,
            route=route if route in ("direct", "agentic") else "direct",
            reason=data.get("reason", ""),
        )

    # ── retrieval leg: embed → hybrid search → rerank ─────────────────────────
    async def retrieve(
        self, scope: QueryScope, collection: str, query: str, trace: Trace
    ) -> list[Candidate]:
        t0 = time.perf_counter()
        qvec = (await self.embedder.embed([query]))[0]
        trace.add_stage("embed_query", (time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        candidates = await self.retriever.search(
            scope, collection, query, qvec, top_k=self.settings.retrieve_top_k
        )
        trace.add_stage("hybrid_search", (time.perf_counter() - t0) * 1000)

        if self.reranker and candidates:
            t0 = time.perf_counter()
            scores = await self.reranker.rerank(query, [c.content for c in candidates])
            for cand, score in zip(candidates, scores, strict=True):
                cand.rerank_score = score
            candidates.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)
            trace.add_stage("rerank", (time.perf_counter() - t0) * 1000)
        return candidates

    def build_context(self, candidates: list[Candidate], trace: Trace) -> ContextBundle:
        bundle = assemble_context(
            candidates,
            top_n=self.settings.context_top_n,
            token_budget=self.settings.context_token_budget,
        )
        trace.candidates = candidates
        return bundle

    def answer_messages(self, question: str, bundle: ContextBundle) -> list[LLMMessage]:
        return [
            LLMMessage(role="system", content=SYSTEM_ANSWER),
            LLMMessage(role="user", content=answer_user_prompt(question, bundle.text)),
        ]

    # ── agentic path: CRAG-style corrective loop (LangGraph) ──────────────────

    def _budget_exhausted(self, state: LoopState) -> bool:
        return (
            state.get("iterations", 0) >= self.settings.max_loop_iterations
            or state.get("tokens_spent", 0) >= self.settings.loop_token_budget
        )

    async def _node_retrieve(self, state: LoopState) -> dict:
        candidates = await self.retrieve(
            state["scope"], state["collection"], state["query"], state["trace"]
        )
        bundle = self.build_context(candidates, state["trace"])
        return {"candidates": candidates, "bundle": bundle}

    async def _node_grade(self, state: LoopState) -> dict:
        result, _ = await self.registry.complete(
            "mini",
            state["data_class"],
            [
                LLMMessage(role="system", content=GRADE_SYSTEM),
                LLMMessage(
                    role="user",
                    content=grade_prompt(state["question"], state["bundle"].text),
                ),
            ],
            max_tokens=150,
            json_mode=True,
        )
        data = parse_json_loose(result.text)
        spent = state.get("tokens_spent", 0) + result.input_tokens + result.output_tokens
        # Fail-safe: unparsable grade counts as sufficient (never loop on noise).
        return {
            "sufficient": bool(data.get("sufficient", True)),
            "grade_reason": data.get("reason", ""),
            "tokens_spent": spent,
        }

    async def _node_rewrite(self, state: LoopState) -> dict:
        result, _ = await self.registry.complete(
            "mini",
            state["data_class"],
            [
                LLMMessage(role="system", content=REWRITE_SYSTEM),
                LLMMessage(
                    role="user",
                    content=rewrite_prompt(
                        state["question"], state["query"], state.get("grade_reason", "")
                    ),
                ),
            ],
            max_tokens=150,
            json_mode=True,
        )
        data = parse_json_loose(result.text)
        spent = state.get("tokens_spent", 0) + result.input_tokens + result.output_tokens
        return {
            "query": data.get("query") or state["query"],
            "iterations": state.get("iterations", 0) + 1,
            "tokens_spent": spent,
        }

    async def _node_generate(self, state: LoopState) -> dict:
        result, provider = await self.registry.complete(
            "strong",
            state["data_class"],
            self.answer_messages(state["question"], state["bundle"]),
            max_tokens=800,
        )
        trace = state["trace"]
        trace.provider = provider.name
        trace.model = provider.model
        trace.tier = "strong"
        trace.input_tokens += result.input_tokens
        trace.output_tokens += result.output_tokens
        spent = state.get("tokens_spent", 0) + result.input_tokens + result.output_tokens
        return {"answer_text": result.text, "tokens_spent": spent}

    async def _node_check(self, state: LoopState) -> dict:
        result, _ = await self.registry.complete(
            "mini",
            state["data_class"],
            [
                LLMMessage(role="system", content=GROUNDEDNESS_SYSTEM),
                LLMMessage(
                    role="user",
                    content=groundedness_prompt(state["answer_text"], state["bundle"].text),
                ),
            ],
            max_tokens=150,
            json_mode=True,
        )
        data = parse_json_loose(result.text)
        spent = state.get("tokens_spent", 0) + result.input_tokens + result.output_tokens
        # Fail-safe: unparsable check counts as grounded (best available answer beats error).
        return {"grounded": bool(data.get("grounded", True)), "tokens_spent": spent}

    def _after_grade(self, state: LoopState) -> str:
        if state.get("sufficient", True) or self._budget_exhausted(state):
            return "generate"
        return "rewrite"

    def _after_check(self, state: LoopState) -> str:
        if state.get("grounded", True) or self._budget_exhausted(state):
            return END
        return "rewrite"

    def build_agentic_graph(self):
        g: StateGraph = StateGraph(LoopState)
        g.add_node("retrieve", self._node_retrieve)
        g.add_node("grade", self._node_grade)
        g.add_node("rewrite", self._node_rewrite)
        g.add_node("generate", self._node_generate)
        g.add_node("check", self._node_check)
        g.set_entry_point("retrieve")
        g.add_edge("retrieve", "grade")
        g.add_conditional_edges(
            "grade", self._after_grade, {"generate": "generate", "rewrite": "rewrite"}
        )
        g.add_edge("rewrite", "retrieve")
        g.add_edge("generate", "check")
        g.add_conditional_edges("check", self._after_check, {END: END, "rewrite": "rewrite"})
        return g.compile()

    async def run_agentic(
        self,
        scope: QueryScope,
        collection: str,
        data_class: DataClass,
        question: str,
        query: str,
        trace: Trace,
    ) -> tuple[Answer, ContextBundle]:
        """Corrective loop; the final validated answer is pseudo-streamed by the
        API layer (streaming a possibly-discarded generation would be wrong)."""
        graph = self.build_agentic_graph()
        final: LoopState = await graph.ainvoke(
            {
                "scope": scope,
                "collection": collection,
                "data_class": data_class,
                "question": question,
                "query": query,
                "trace": trace,
                "iterations": 0,
                "tokens_spent": 0,
            }
        )
        trace.route = "agentic"
        trace.loop_iterations = final.get("iterations", 0)
        bundle = final["bundle"]
        clean, citations, cits_valid = validate_citations(final.get("answer_text", ""), bundle)
        from .citations import is_refusal  # local import to avoid cycle at module load

        return Answer(
            text=clean,
            citations=citations,
            refused=is_refusal(clean),
            citations_valid=cits_valid,
        ), bundle
