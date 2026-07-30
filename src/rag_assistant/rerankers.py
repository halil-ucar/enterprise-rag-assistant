"""Reranker adapters + factory.

Two-stage ranking is the production standard: the bi-encoder (embeddings)
recalls ~30 candidates fast, the cross-encoder reads query+candidate TOGETHER
and re-sorts the small set precisely. Deliberately switchable OFF so the eval
matrix MEASURES its contribution instead of asserting it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .config import Settings
from .ports import Reranker
from .testing.fakes import FakeReranker

BGE_RERANKER = "BAAI/bge-reranker-v2-m3"


class LocalReranker:
    name = "bge-reranker-v2-m3"

    def __init__(self, model_name: str = BGE_RERANKER, revision: str = ""):
        from sentence_transformers import CrossEncoder  # heavy: lazy import

        # revision pins an exact HF commit (supply-chain hygiene, see embeddings).
        self._model = CrossEncoder(model_name, revision=revision or None)

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        def _run() -> list[float]:
            scores = self._model.predict([(query, t) for t in texts])
            return [float(s) for s in scores]

        return await asyncio.to_thread(_run)


def build_reranker(settings: Settings) -> Reranker | None:
    if settings.reranker_backend == "off":
        return None
    if settings.reranker_backend == "fake":
        return FakeReranker()
    return LocalReranker(revision=settings.reranker_model_revision)
