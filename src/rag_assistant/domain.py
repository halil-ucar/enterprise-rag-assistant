"""Domain models shared across the whole pipeline.

Everything here is transport-agnostic: no FastAPI, no SQL, no provider SDKs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class DataClass(StrEnum):
    """Sensitivity class of a collection. Drives provider routing (see policy.py).

    Fail-closed principle: anything unknown/unset is treated as CONFIDENTIAL.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"


class QueryScope(BaseModel):
    """Who is asking, in which tenant — the permission context of a request.

    This object is the single source for (a) the RLS session variables and
    (b) the permission part of the answer-cache key. Keeping both derived
    from ONE object prevents cache/RLS drift.
    """

    tenant: str
    user_id: str
    department: str

    def cache_scope(self) -> str:
        return f"{self.tenant}:{self.department}"


class CollectionCfg(BaseModel):
    """Declarative collection registry entry (config/collections.yaml)."""

    name: str
    tenant: str
    data_class: DataClass
    description: str = ""
    embedding_version: int = 1
    # 'extractive' = answers are the retrieved passages themselves — the request
    # path makes NO LLM call at all (no condense/route, no generation). The
    # strictest per-collection privacy mode: nothing to prompt, nothing to leak.
    generation: Literal["llm", "extractive"] = "llm"


class ChunkDraft(BaseModel):
    """A chunk produced by the chunker, before persistence."""

    seq: int
    content: str  # includes the header-path prefix
    section_path: str  # e.g. "IT-Handbuch > VPN > Fehlerbehebung"
    is_table: bool = False
    token_estimate: int = 0


class Candidate(BaseModel):
    """A retrieval candidate flowing through fusion → rerank → context assembly.

    Rank fields are 1-based; None means 'not returned by that search leg'.
    """

    chunk_id: str
    doc_id: str
    doc_title: str = ""
    section_path: str = ""
    page: int | None = None
    content: str
    dense_rank: int | None = None
    lex_rank: int | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None
    in_context: bool = False


class Citation(BaseModel):
    """Stable, human-readable citation anchor (survives re-chunking/re-indexing)."""

    marker: str  # "S1", "S2", ...
    doc_id: str
    doc_title: str
    section_path: str
    page: int | None = None


class RouteDecision(BaseModel):
    """Output of the combined condense+route pre-call (one structured mini-call).
    route='extractive' is never LLM-decided: it is forced by collection config
    BEFORE any model call (see api.query)."""

    standalone_query: str
    route: str = Field(pattern="^(direct|agentic|extractive)$")
    reason: str = ""


class Answer(BaseModel):
    text: str
    citations: list[Citation] = []
    refused: bool = False
    # False = the model emitted at least one marker that matched no provided
    # source (stripped by validate_citations). Measured by the eval harness.
    citations_valid: bool = True


class StageTiming(BaseModel):
    name: str
    ms: float


class Trace(BaseModel):
    """Glass-box payload: everything the UI metadata line + debug panel shows."""

    route: str = "direct"
    provider: str = ""
    model: str = ""
    tier: str = ""
    data_class: DataClass = DataClass.INTERNAL
    collection: str = ""
    cache_hit: bool = False
    stages: list[StageTiming] = []
    candidates: list[Candidate] = []
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    loop_iterations: int = 0

    def add_stage(self, name: str, ms: float) -> None:
        self.stages.append(StageTiming(name=name, ms=ms))


class UserDataDeletionReport(BaseModel):
    """Self-service deletion: what was removed for the REQUESTING user (their
    feedback rows, chat sessions and auth sessions) — logged as counts,
    never content."""

    user_id: str
    feedback_rows_deleted: int = 0
    sessions_deleted: int = 0
    auth_sessions_deleted: int = 0


class DeletionReport(BaseModel):
    """What the deletion cascade removed — logged as references/counts, never content."""

    doc_id: str
    # False = the document was not visible to the requesting scope (RLS) or does
    # not exist. The cascade steps outside the database (cache purge, session
    # redaction) MUST NOT run in that case — they are not RLS-protected.
    found: bool = False
    chunks_deleted: int = 0
    cache_entries_purged: int = 0
    session_messages_redacted: int = 0
    feedback_rows_deleted: int = 0
    # Surrogate id of the deleted document (A4): the deletion receipt references
    # THIS value, never the speaking doc_id slug. The mapping died with the row.
    audit_ref: str | None = None
