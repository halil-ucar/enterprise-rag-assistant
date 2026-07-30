"""Permission-scoped answer cache (Redis).

The key (cachekey.py) carries tenant + department + data class + condensed
question + corpus/embedding versions. Additionally every cache entry is indexed
by the doc_ids it cites, so the deletion cascade can purge answers derived from a
deleted document — belt and suspenders on top of the corpus_version bump.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis


class AnswerCache:
    def __init__(self, redis: aioredis.Redis, ttl_s: int = 3600):
        self.redis = redis
        self.ttl_s = ttl_s

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(key)
        return json.loads(raw) if raw else None

    async def set(self, key: str, payload: dict[str, Any], cited_doc_ids: list[str]) -> None:
        pipe = self.redis.pipeline()
        pipe.set(key, json.dumps(payload, ensure_ascii=False), ex=self.ttl_s)
        for doc_id in cited_doc_ids:
            pipe.sadd(f"cachedocs:{doc_id}", key)
            pipe.expire(f"cachedocs:{doc_id}", self.ttl_s * 2)
        await pipe.execute()

    async def purge_by_doc(self, doc_id: str) -> int:
        """Delete every cached answer that cited this document. Returns count."""
        index_key = f"cachedocs:{doc_id}"
        keys = await self.redis.smembers(index_key)  # type: ignore[misc]
        n = 0
        if keys:
            n = int(await self.redis.delete(*keys))
        await self.redis.delete(index_key)
        return n
