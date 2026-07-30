"""Ports (hexagonal architecture).

Contracts stay identical from 10k to 100M chunks — scaling swaps ADAPTERS
(e.g. PgRetriever → QdrantRetriever, in-process embedder → TEI service),
never these signatures. Do not leak backend specifics through a port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from .audit import AuditEvent
from .domain import Candidate, ChunkDraft, DeletionReport, QueryScope


class LLMMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMResult(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Text → vector. `version` feeds embedding_version (blue-green reindex)."""

    dim: int
    version: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class Reranker(Protocol):
    """Cross-encoder stage: query + candidate texts → relevance scores (higher = better)."""

    name: str

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]: ...


@runtime_checkable
class LLMProvider(Protocol):
    """One model behind one name. kind: 'cloud' | 'sovereign' | 'local' (policy routing)."""

    name: str
    kind: str
    model: str

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> LLMResult: ...

    def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class AuditLog(Protocol):
    """Append-only access log (A4). write() persists one metadata event;
    purge_expired() enforces retention and returns the purge count."""

    async def write(self, event: AuditEvent) -> None: ...

    async def purge_expired(self, retention_days: int) -> int: ...


@runtime_checkable
class Retriever(Protocol):
    """Hybrid search under a permission scope. RLS is enforced INSIDE the adapter."""

    async def search(
        self,
        scope: QueryScope,
        collection: str,
        query: str,
        query_vector: Sequence[float],
        *,
        top_k: int = 30,
    ) -> list[Candidate]: ...


@runtime_checkable
class DocumentStore(Protocol):
    """Ingestion + deletion cascade. Upsert is idempotent via (doc_id, content_hash)."""

    async def upsert_document(
        self,
        scope: QueryScope,
        collection: str,
        doc_id: str,
        title: str,
        content_hash: str,
        department: str,
        chunks: Sequence[ChunkDraft],
        vectors: Sequence[Sequence[float]],
        embedding_version: int,
    ) -> str: ...

    async def delete_document(self, scope: QueryScope, doc_id: str) -> DeletionReport: ...
