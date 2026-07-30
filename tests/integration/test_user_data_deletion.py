"""User-scoped data deletion + retention against real Postgres + Redis.

Proves two things the unit fakes cannot:
  1. Self-service deletion removes EXACTLY the requesting user's feedback and
     sessions — a second user in the SAME department (fully visible under RLS)
     survives untouched, so the user_id predicate does the protecting, not RLS.
  2. The retention function (db/init/03_retention.sql, SECURITY DEFINER)
     purges expired rows and leaves fresh ones alone.
"""

import os
import uuid

import pytest
import redis.asyncio as aioredis

from rag_assistant.cache import AnswerCache
from rag_assistant.deletion import DeletionCascade
from rag_assistant.domain import QueryScope
from rag_assistant.sessions import SessionStore
from rag_assistant.store.pg import PgBackend, PgStore

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://rag_app:rag_app_pw@localhost:5432/rag")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
async def stack():
    backend = PgBackend(DATABASE_URL)
    await backend.open()
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    store = PgStore(backend)
    sessions = SessionStore(redis)
    deleter = DeletionCascade(store, AnswerCache(redis), sessions)
    yield store, sessions, deleter, backend
    await backend.close()
    await redis.aclose()


async def test_self_service_deletes_only_the_requesting_user(stack):
    store, sessions, deleter, _ = stack
    suffix = uuid.uuid4().hex[:8]
    anna = QueryScope(tenant="nordfels", user_id=f"anna-{suffix}", department="it")
    carl = QueryScope(tenant="nordfels", user_id=f"carl-{suffix}", department="it")
    for scope in (anna, carl):
        await store.add_feedback(scope, "handbuecher", 1, f"frage {scope.user_id}", "direct", [])
        await sessions.append(scope.tenant, scope.user_id, "s1", "user", "hallo")

    report = await deleter.delete_user_data(anna)

    assert report.user_id == anna.user_id
    assert report.feedback_rows_deleted == 1
    assert report.sessions_deleted == 1
    # carl (same department, fully visible to anna's RLS scope) is untouched:
    assert await sessions.history(carl.tenant, carl.user_id, "s1") != []
    # his row still exists — his own self-service delete finds exactly it
    # (doubles as test cleanup):
    assert await store.delete_user_feedback(carl, carl.user_id) == 1
    await sessions.delete_user_sessions(carl.tenant, carl.user_id)


async def test_retention_purges_only_expired_rows(stack):
    store, _, _, backend = stack
    suffix = uuid.uuid4().hex[:8]
    scope = QueryScope(tenant="nordfels", user_id=f"old-{suffix}", department="it")
    await store.add_feedback(scope, "handbuecher", 1, "alte frage", "direct", [])
    await store.add_feedback(scope, "handbuecher", 1, "neue frage", "direct", [])
    # Age one row past the horizon (UPDATE under the same scope — RLS permits).
    async with backend.scoped_tx(scope) as conn:
        await conn.execute(
            "UPDATE feedback SET created_at = now() - interval '400 days' "
            "WHERE user_id = %s AND condensed_question = %s",
            (scope.user_id, "alte frage"),
        )

    purged = await store.purge_expired_feedback(365)

    # >= 1: the function is global by design and may sweep other stale test rows.
    assert purged >= 1
    # The fresh row survived — self-service finds exactly one (and cleans up).
    assert await store.delete_user_feedback(scope, scope.user_id) == 1
