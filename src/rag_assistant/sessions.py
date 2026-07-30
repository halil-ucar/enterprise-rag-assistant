"""Session memory (Redis) with deletion hooks.

Every assistant message stores the doc_ids it cited. That makes deletion
deterministic: the cascade looks up `sessiondocs:{doc_id}` and REDACTS the
affected messages (content replaced, structure kept) instead of guessing by
content or nuking whole sessions. Sessions expire via TTL as hygiene — but
TTL alone would only DELAY deletion, never satisfy it.

Keys are bound to the USER, not just the tenant: session_id comes from the
client, so a tenant-only key would let anyone continue (and feed into the
condense context) another user's conversation by knowing its id.
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis

REDACTED = "[Inhalt entfernt — Quelldokument wurde gelöscht]"


class SessionStore:
    def __init__(self, redis: aioredis.Redis, ttl_s: int = 86400):
        self.redis = redis
        self.ttl_s = ttl_s

    @staticmethod
    def _key(tenant: str, user_id: str, session_id: str) -> str:
        return f"session:{tenant}:{user_id}:{session_id}"

    async def append(
        self,
        tenant: str,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        cited_doc_ids: list[str] | None = None,
    ) -> None:
        key = self._key(tenant, user_id, session_id)
        msg = {"role": role, "content": content, "cited_doc_ids": cited_doc_ids or []}
        pipe = self.redis.pipeline()
        pipe.rpush(key, json.dumps(msg, ensure_ascii=False))
        pipe.expire(key, self.ttl_s)
        for doc_id in msg["cited_doc_ids"]:
            pipe.sadd(f"sessiondocs:{doc_id}", key)
            pipe.expire(f"sessiondocs:{doc_id}", self.ttl_s * 2)
        await pipe.execute()

    async def history(
        self, tenant: str, user_id: str, session_id: str, limit: int = 12
    ) -> list[dict]:
        raw = await self.redis.lrange(  # type: ignore[misc]
            self._key(tenant, user_id, session_id), -limit, -1
        )
        return [json.loads(m) for m in raw]

    async def delete_user_sessions(self, tenant: str, user_id: str) -> int:
        """Delete every session of ONE user (self-service deletion). Keys are
        user-bound (module docstring), so a SCAN over the user's prefix is
        exact — no message content is inspected. Stale references left in
        `sessiondocs:*` index sets are harmless: redact_doc reads via lrange,
        which simply yields nothing for a deleted key."""
        pattern = f"session:{tenant}:{user_id}:*"
        deleted = 0
        async for key in self.redis.scan_iter(match=pattern, count=100):
            deleted += int(await self.redis.delete(key))
        return deleted

    async def redact_doc(self, doc_id: str) -> int:
        """Redact every session message citing doc_id. Returns messages redacted."""
        index_key = f"sessiondocs:{doc_id}"
        session_keys = await self.redis.smembers(index_key)  # type: ignore[misc]
        redacted = 0
        for skey in session_keys:
            msgs = await self.redis.lrange(skey, 0, -1)  # type: ignore[misc]
            for i, raw in enumerate(msgs):
                msg = json.loads(raw)
                if doc_id in msg.get("cited_doc_ids", []) and msg["content"] != REDACTED:
                    msg["content"] = REDACTED
                    await self.redis.lset(skey, i, json.dumps(msg, ensure_ascii=False))  # type: ignore[misc]
                    redacted += 1
        await self.redis.delete(index_key)
        return redacted
