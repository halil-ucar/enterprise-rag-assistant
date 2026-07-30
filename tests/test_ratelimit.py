"""Unit tests for the token-bucket rate limiter (A3).

take() is pure — `now` is a parameter, so every edge case runs without any
clock. The Redis adapter is exercised through the dict-backed fake store.
"""

from __future__ import annotations

import logging
import math

from rag_assistant.ratelimit import BucketState, RedisRateLimiter, take
from rag_assistant.testing.fakes import FakeRateLimitStore


# ── pure arithmetic ───────────────────────────────────────────────────────────
def test_first_request_gets_a_full_bucket():
    allowed, state, retry = take(None, now=100.0, capacity=5.0, refill_per_s=1.0)
    assert allowed is True
    assert state == BucketState(tokens=4.0, updated_at=100.0)
    assert retry == 0.0


def test_burst_drains_then_denies_at_a_standstill():
    """Time standstill: with `now` unchanged, exactly `capacity` requests pass."""
    state = None
    for _ in range(3):
        allowed, state, _ = take(state, now=50.0, capacity=3.0, refill_per_s=1.0)
        assert allowed is True
    allowed, state, retry = take(state, now=50.0, capacity=3.0, refill_per_s=1.0)
    assert allowed is False
    assert retry == 1.0  # 1 token missing at 1 token/s


def test_refill_is_capped_at_capacity():
    """A long idle period must not accumulate tokens beyond the burst size."""
    state = BucketState(tokens=0.0, updated_at=0.0)
    allowed, new_state, _ = take(state, now=10_000.0, capacity=5.0, refill_per_s=1.0)
    assert allowed is True
    assert new_state.tokens == 4.0  # capped at 5, then cost 1


def test_partial_refill_and_retry_after_value():
    state = BucketState(tokens=0.0, updated_at=100.0)
    # 1s at 0.5 tokens/s → 0.5 tokens; cost 1 → 0.5 missing → 1s to wait
    allowed, new_state, retry = take(state, now=101.0, capacity=10.0, refill_per_s=0.5)
    assert allowed is False
    assert new_state.tokens == 0.5
    assert new_state.updated_at == 101.0
    assert retry == 1.0


def test_cost_greater_than_capacity_never_passes():
    allowed, state, retry = take(None, now=0.0, capacity=5.0, refill_per_s=1.0, cost=10.0)
    assert allowed is False
    assert retry == 5.0  # honest arithmetic: (10 - 5) / 1


def test_zero_refill_reports_infinite_retry():
    state = BucketState(tokens=0.0, updated_at=0.0)
    allowed, _, retry = take(state, now=1.0, capacity=1.0, refill_per_s=0.0)
    assert allowed is False
    assert retry == math.inf


# ── Redis adapter over the fake store ─────────────────────────────────────────
async def test_adapter_burst_then_denial_with_retry_after():
    clock = {"now": 0.0}
    limiter = RedisRateLimiter(FakeRateLimitStore(), now_fn=lambda: clock["now"])
    for _ in range(2):
        allowed, _ = await limiter.acquire(
            "nordfels", "anna", "query", capacity=2.0, refill_per_s=0.5
        )
        assert allowed is True
    allowed, retry = await limiter.acquire(
        "nordfels", "anna", "query", capacity=2.0, refill_per_s=0.5
    )
    assert allowed is False
    assert retry == 2.0  # 1 token missing at 0.5 tokens/s


async def test_adapter_key_layout_and_self_healing_ttl():
    store = FakeRateLimitStore()
    limiter = RedisRateLimiter(store, now_fn=lambda: 0.0)
    await limiter.acquire("nordfels", "anna", "query", capacity=10.0, refill_per_s=0.5)
    key = "rl:nordfels:anna:query"
    assert key in store.data
    assert store.ttls[key] == math.ceil(10.0 / 0.5) + 60


async def test_adapter_isolates_identities_and_route_classes():
    store = FakeRateLimitStore()
    limiter = RedisRateLimiter(store, now_fn=lambda: 0.0)
    # anna exhausts her query bucket; ben and anna's ingest bucket are untouched
    await limiter.acquire("nordfels", "anna", "query", capacity=1.0, refill_per_s=0.1)
    denied, _ = await limiter.acquire("nordfels", "anna", "query", capacity=1.0, refill_per_s=0.1)
    ben_ok, _ = await limiter.acquire("nordfels", "ben", "query", capacity=1.0, refill_per_s=0.1)
    ingest_ok, _ = await limiter.acquire(
        "nordfels", "anna", "ingest", capacity=1.0, refill_per_s=0.1
    )
    assert denied is False
    assert ben_ok is True
    assert ingest_ok is True


async def test_fail_open_on_store_error(caplog):
    """The limiter protects availability, not authorization: a broken store
    lets the request pass and logs an ERROR."""

    class BrokenStore:
        async def get(self, key: str) -> str | None:
            raise ConnectionError("redis down")

        async def set(self, key: str, value: str, *, ex: int) -> None:
            raise ConnectionError("redis down")

    limiter = RedisRateLimiter(BrokenStore())
    with caplog.at_level(logging.ERROR, logger="rag_assistant.ratelimit"):
        allowed, retry = await limiter.acquire(
            "nordfels", "anna", "query", capacity=1.0, refill_per_s=0.1
        )
    assert (allowed, retry) == (True, 0.0)
    assert any("failing open" in r.message for r in caplog.records)
