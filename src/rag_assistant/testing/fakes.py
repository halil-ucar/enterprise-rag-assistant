"""Deterministic test doubles implementing the ports.

They make the whole pipeline unit-testable without models, GPUs or services:
CI runs on these; the deletion-cascade proof runs against real Postgres/Redis
but with the FakeEmbedder (no 2GB downloads in CI).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import AsyncIterator, Sequence

from ..audit import AuditEvent
from ..domain import Candidate, ChunkDraft, QueryScope
from ..fusion import rrf
from ..ports import LLMMessage, LLMResult

_TOKEN = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text)}


class FakeEmbedder:
    """Hash-based bag-of-words embedding: deterministic, order-independent,
    similar token sets → similar vectors. Good enough to exercise cosine search."""

    def __init__(self, dim: int = 64, version: str = "fake-1"):
        self.dim = dim
        self.version = version

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big")
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class FakeReranker:
    """Token-overlap scorer — monotone in real relevance for the test corpus."""

    name = "fake-reranker"

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        q = _tokens(query)
        return [len(q & _tokens(t)) / (len(q) or 1) for t in texts]


class FakeLLM:
    """Scripted LLM: returns queued responses, else an echo with citation marker.

    kind='local' so it is valid on every policy path (incl. confidential).
    """

    def __init__(self, name: str = "fake-llm", responses: list[str] | None = None):
        self.name = name
        self.kind = "local"
        self.model = "fake"
        self.responses = list(responses or [])
        self.calls: list[list[LLMMessage]] = []

    def queue(self, *texts: str) -> None:
        self.responses.extend(texts)

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResult:
        self.calls.append(list(messages))
        text = self.responses.pop(0) if self.responses else "Antwort [S1]."
        return LLMResult(text=text, input_tokens=50, output_tokens=20)

    async def _agen(self, text: str) -> AsyncIterator[str]:
        for i in range(0, len(text), 8):
            yield text[i : i + 8]

    def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        text = self.responses.pop(0) if self.responses else "Antwort [S1]."
        return self._agen(text)


class FakeRateLimitStore:
    """Dict-backed stand-in for the Redis GET/SET(+EX) subset the rate limiter
    uses (ratelimit.RateLimitStore) — unit tests need no Redis."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.data[key] = value
        self.ttls[key] = ex


class FakeAuditLog:
    """Collects events in a list — unit tests assert on exact event contents."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    async def purge_expired(self, retention_days: int) -> int:
        return 0


class FailingAuditLog(FakeAuditLog):
    """write() raises — exercises the critical/non-critical split of the
    audit write path (fail-closed stream abort vs. logged-and-counted)."""

    async def write(self, event: AuditEvent) -> None:
        raise RuntimeError("audit backend down")


class InMemoryRetriever:
    """Second Retriever implementation — proves the port has no Postgres leak
    and powers unit tests of the full chain (hybrid = cosine + token overlap → RRF)."""

    def __init__(self, embedder: FakeEmbedder | None = None):
        self.embedder = embedder or FakeEmbedder()
        # (scope-visible department, collection, candidate, vector)
        self._rows: list[tuple[str, str, Candidate, list[float]]] = []

    async def add(
        self,
        collection: str,
        doc_id: str,
        doc_title: str,
        department: str,
        chunks: Sequence[ChunkDraft],
    ) -> None:
        vecs = await self.embedder.embed([c.content for c in chunks])
        for c, v in zip(chunks, vecs, strict=True):
            cand = Candidate(
                chunk_id=f"{doc_id}:{c.seq}",
                doc_id=doc_id,
                doc_title=doc_title,
                section_path=c.section_path,
                content=c.content,
            )
            self._rows.append((department, collection, cand, v))

    async def delete_document(self, doc_id: str) -> int:
        before = len(self._rows)
        self._rows = [r for r in self._rows if r[2].doc_id != doc_id]
        return before - len(self._rows)

    async def search(
        self,
        scope: QueryScope,
        collection: str,
        query: str,
        query_vector: Sequence[float],
        *,
        top_k: int = 30,
    ) -> list[Candidate]:
        visible = [
            (cand, vec)
            for dept, coll, cand, vec in self._rows
            if coll == collection and dept in (scope.department, "all")
        ]
        if not visible:
            return []
        dense = sorted(
            visible,
            key=lambda cv: -sum(a * b for a, b in zip(query_vector, cv[1], strict=False)),
        )[:top_k]
        q = _tokens(query)
        lex = sorted(
            (cv for cv in visible if q & _tokens(cv[0].content)),
            key=lambda cv: -len(q & _tokens(cv[0].content)),
        )[:top_k]

        dense_ids = [cv[0].chunk_id for cv in dense]
        lex_ids = [cv[0].chunk_id for cv in lex]
        fused = rrf([dense_ids, lex_ids])[:top_k]

        by_id = {cv[0].chunk_id: cv[0] for cv in visible}
        out: list[Candidate] = []
        for chunk_id, score in fused:
            cand = by_id[chunk_id].model_copy()
            cand.rrf_score = score
            cand.dense_rank = dense_ids.index(chunk_id) + 1 if chunk_id in dense_ids else None
            cand.lex_rank = lex_ids.index(chunk_id) + 1 if chunk_id in lex_ids else None
            out.append(cand)
        return out
