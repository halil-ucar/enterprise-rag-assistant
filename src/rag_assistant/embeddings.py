"""Embedding adapters + factory.

local  → BGE-M3 in-process via sentence-transformers (dense leg only; the sparse
         leg of hybrid search is Postgres FTS). MPS on Apple Silicon in dev mode,
         CPU in containers. Exact HF pinning keeps embedding_version meaningful.
fake   → deterministic hash embedder (tests/CI; 1024-dim to match the schema).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .config import Settings
from .ports import EmbeddingProvider
from .testing.fakes import FakeEmbedder

BGE_M3 = "BAAI/bge-m3"


class LocalEmbedder:
    dim = 1024
    version = "bge-m3-v1"

    def __init__(self, model_name: str = BGE_M3, revision: str = ""):
        from sentence_transformers import SentenceTransformer  # heavy: lazy import

        # revision pins an exact HF commit (supply-chain hygiene): the model
        # weights can then never drift under the frozen embedding_version.
        self._model = SentenceTransformer(model_name, revision=revision or None)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # sentence-transformers is sync; keep the event loop responsive.
        def _run() -> list[list[float]]:
            vectors = self._model.encode(list(texts), normalize_embeddings=True)
            return [v.tolist() for v in vectors]

        return await asyncio.to_thread(_run)


def build_embedder(settings: Settings) -> EmbeddingProvider:
    if settings.embeddings_backend == "fake":
        return FakeEmbedder(dim=1024)
    return LocalEmbedder(revision=settings.embedding_model_revision)
