"""Answer-cache key — pure function, unit-tested.

The cache sits IN FRONT of the database and therefore OUTSIDE of RLS
enforcement. Its key must carry the full permission context, or the cache
becomes a permission bypass. Components:

- tenant + permission scope (department)  → no cross-user leaks
- data class                              → confidential answers never shared across classes
- CONDENSED standalone question           → raw follow-ups ("und wie lösche ich das?")
                                            would collide across sessions
- corpus_version                          → any ingest/delete invalidates implicitly
- embedding_version                       → model swap invalidates
"""

from __future__ import annotations

import hashlib
import re

from .domain import DataClass, QueryScope

_WS = re.compile(r"\s+")


def normalize_question(q: str) -> str:
    return _WS.sub(" ", q.strip().lower())


def build_answer_cache_key(
    scope: QueryScope,
    data_class: DataClass,
    condensed_question: str,
    corpus_version: int,
    embedding_version: int,
) -> str:
    payload = "\x1f".join(
        [
            scope.cache_scope(),
            data_class.value,
            normalize_question(condensed_question),
            str(corpus_version),
            str(embedding_version),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"answer:{scope.tenant}:{digest}"
