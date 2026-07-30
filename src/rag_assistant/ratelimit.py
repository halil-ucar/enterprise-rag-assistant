"""Per-identity rate limiting (A3): pure token-bucket arithmetic + Redis adapter.

Algorithm decision: token bucket, not sliding-window log — O(1) state per
identity, burst tolerance as an explicit size (capacity), and the whole
arithmetic is a pure function ("determinism first"). Limits are per identity
(tenant + user), never per IP: the system runs behind proxies, and identity
exists from the auth layer on.

Known, ACCEPTED race: the GET → take() → SET round trip is not atomic, so
parallel requests of the same identity can slightly under-count. Documented
instead of half-fixed — the Lua-script/atomic variant is the named scaling
step (see docs/ARCHITECTURE.md).

Failure mode: fail-open on store errors (ERROR log, request passes). The
limiter protects AVAILABILITY; it is not an authorization gate — auth, RLS
and data-class routing stay fail-closed. A Redis outage must not turn a
degradation into a full API outage. Operator alternative (documented, not
built): fail-closed in production mode only.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BucketState:
    tokens: float
    updated_at: float


def take(
    state: BucketState | None,
    now: float,
    capacity: float,
    refill_per_s: float,
    cost: float = 1.0,
) -> tuple[bool, BucketState, float]:
    """One pure token-bucket step: refill since `updated_at`, then try to spend
    `cost`. `now` ALWAYS comes from outside (the adapter uses time.monotonic()),
    so every edge case is testable without any clock.

    Returns (allowed, new_state, retry_after_s); state=None (first request)
    means a full bucket; retry_after is the remaining time until `cost` would
    be available.
    """
    tokens = (
        capacity
        if state is None
        else min(capacity, state.tokens + (now - state.updated_at) * refill_per_s)
    )
    if tokens >= cost:
        return True, BucketState(tokens=tokens - cost, updated_at=now), 0.0
    retry_after = (cost - tokens) / refill_per_s if refill_per_s > 0 else math.inf
    return False, BucketState(tokens=tokens, updated_at=now), retry_after


class RateLimitStore(Protocol):
    """The GET/SET(+EX) subset of Redis the limiter needs — FakeRateLimitStore
    implements the same surface for unit tests. Positional-only parameters so
    the real redis client (param name `name`) satisfies the protocol."""

    def get(self, key: str, /) -> Awaitable[str | None]: ...

    def set(self, key: str, value: str, /, *, ex: int) -> Awaitable[Any]: ...


class RedisRateLimiter:
    """GET → take() → SET with an EX TTL. Keys: rl:{tenant}:{user_id}:{route_class};
    value: JSON {tokens, updated_at}. The TTL (ceil(capacity/refill) + 60s)
    self-heals orphaned keys of idle identities."""

    def __init__(self, store: RateLimitStore, *, now_fn: Callable[[], float] = time.monotonic):
        self.store = store
        self.now_fn = now_fn

    async def acquire(
        self,
        tenant: str,
        user_id: str,
        route_class: str,
        *,
        capacity: float,
        refill_per_s: float,
        cost: float = 1.0,
    ) -> tuple[bool, float]:
        key = f"rl:{tenant}:{user_id}:{route_class}"
        try:
            raw = await self.store.get(key)
            state = None
            if raw:
                data = json.loads(raw)
                state = BucketState(
                    tokens=float(data["tokens"]), updated_at=float(data["updated_at"])
                )
            allowed, new_state, retry_after = take(
                state, self.now_fn(), capacity, refill_per_s, cost
            )
            ttl = (math.ceil(capacity / refill_per_s) if refill_per_s > 0 else 0) + 60
            await self.store.set(
                key,
                json.dumps({"tokens": new_state.tokens, "updated_at": new_state.updated_at}),
                ex=ttl,
            )
            return allowed, retry_after
        except Exception:
            # Fail-open (see module docstring): availability guard, not an
            # authorization gate. Log carries identity only, never content.
            log.exception("rate limiter store failure — failing open (key=%s)", key)
            return True, 0.0
