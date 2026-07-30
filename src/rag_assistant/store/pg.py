"""Postgres adapters: hybrid retrieval (dense + dual-FTS fused with RRF in SQL)
and the document store (idempotent ingestion, atomic generation swap, deletion).

RLS contract: every operation runs inside `scoped_tx`, which sets the permission
context via set_config(..., is_local=true) — i.e. SET LOCAL semantics, transaction-
scoped, pool-safe. The app connects as `rag_app` (never the table owner).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..domain import Candidate, ChunkDraft, DeletionReport, QueryScope


def vec_literal(v: Sequence[float]) -> str:
    """pgvector text literal — avoids adapter registration on every pool conn."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class PgBackend:
    def __init__(self, database_url: str, rrf_k: int = 60):
        self.pool = AsyncConnectionPool(database_url, min_size=1, max_size=10, open=False)
        self.rrf_k = rrf_k
        # pgvector ≥ 0.8 adds iterative index scans (fixes the post-filter recall
        # hole under selective RLS/collection filters). Detected once at startup —
        # a failed SET LOCAL inside a transaction would poison the transaction.
        self.supports_iterative_scan = False

    async def open(self) -> None:
        await self.pool.open()
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = await cur.fetchone()
            if row:
                major, minor = (int(x) for x in str(row[0]).split(".")[:2])
                self.supports_iterative_scan = (major, minor) >= (0, 8)

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def scoped_tx(self, scope: QueryScope) -> AsyncIterator[AsyncConnection]:
        """One transaction with the RLS context applied. SET LOCAL (is_local=true)
        dies with the transaction — nothing leaks to the next pooled request."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant', %s, true),"
                "       set_config('app.user', %s, true),"
                "       set_config('app.department', %s, true)",
                (scope.tenant, scope.user_id, scope.department),
            )
            yield conn


_WORD = re.compile(r"[\wäöüÄÖÜß-]+", re.UNICODE)


def lex_queries(question: str) -> tuple[str, str]:
    """Build OR-semantics websearch queries for the two FTS legs.

    Why OR: websearch_to_tsquery ANDs terms by default — a natural-language
    question ("Was bedeutet der VPN-Fehler NF-4102?") then matches NOTHING,
    silently killing the lexical leg (found via the debug panel: every Lex
    rank was '–'). OR + ts_rank_cd restores recall while ranking by overlap.

    Each leg has ONE job (measured on the hard-negative corpus, 2026-07-12):
    - german leg: language matching — all tokens ≥3 chars, the config stems
      and drops stopwords itself (compounds like "Rufbereitschaftszulage"
      match through identical stemming on both sides)
    - simple leg: identifier matching ONLY — tokens with a digit or hyphen
      (M-205, LJ70-E501, E3, NF-FORM-ZUG). The earlier len≥5 clause flooded
      this leg with ordinary long words ("aktuell", "gültig", "haben") that
      match unstemmed everywhere: the anchor for LJ70-E501 sat at lex rank 6
      behind word-overlap noise. No identifier in the question ⇒ empty query
      ⇒ the leg simply contributes nothing.
    """
    tokens = _WORD.findall(question)
    german = [t for t in tokens if len(t) >= 3]
    simple = [t for t in tokens if any(ch.isdigit() for ch in t) or "-" in t]
    return " OR ".join(german), " OR ".join(simple)


class PgRetriever:
    """Hybrid search in ONE SQL round-trip: dense HNSW + two FTS legs, each its
    OWN rank list, RRF-fused via FULL OUTER JOIN. RLS filters every leg (same
    transaction, same policies).

    Why three separate rank lists (measured, 2026-07-12): the first version
    summed ts_rank_cd(german) + ts_rank_cd(simple) into ONE lexical list. The
    two configs are different scales, and chunks matching many common words in
    BOTH configs got double credit — word-overlap noise outranked the exact
    identifier hit inside its own list (anchor for LJ70-E501 at lex rank 6,
    fused position 11). Rank fusion exists precisely because scores across
    systems don't compare — that argument applies BETWEEN the FTS configs too.
    Now: german ranks language overlap, simple ranks identifier hits, RRF
    fuses ranks only. An empty identifier query matches nothing and simply
    contributes no list."""

    SQL = """
    WITH dense AS (
        SELECT id, row_number() OVER () AS r
        FROM (
            SELECT id FROM chunks
            WHERE collection = %(collection)s AND embedding_version = %(emb_v)s
            ORDER BY embedding <=> %(qvec)s::vector
            LIMIT %(k)s
        ) d
    ),
    lexg AS (
        SELECT id, row_number() OVER () AS r
        FROM (
            SELECT id
            FROM chunks
            WHERE collection = %(collection)s AND embedding_version = %(emb_v)s
              AND tsv_german @@ websearch_to_tsquery('german', %(q_german)s)
            ORDER BY ts_rank_cd(tsv_german, websearch_to_tsquery('german', %(q_german)s)) DESC
            LIMIT %(k)s
        ) g
    ),
    lexs AS (
        SELECT id, row_number() OVER () AS r
        FROM (
            SELECT id
            FROM chunks
            WHERE collection = %(collection)s AND embedding_version = %(emb_v)s
              AND tsv_simple @@ websearch_to_tsquery('simple', %(q_simple)s)
            ORDER BY ts_rank_cd(tsv_simple, websearch_to_tsquery('simple', %(q_simple)s)) DESC
            LIMIT %(k)s
        ) s
    ),
    fused AS (
        SELECT id,
               COALESCE(1.0 / (%(rrf_k)s + dense.r), 0)
             + COALESCE(1.0 / (%(rrf_k)s + lexg.r), 0)
             + COALESCE(1.0 / (%(rrf_k)s + lexs.r), 0) AS rrf_score,
               dense.r AS dense_rank,
               LEAST(lexg.r, lexs.r) AS lex_rank
        FROM dense
        FULL OUTER JOIN lexg USING (id)
        FULL OUTER JOIN lexs USING (id)
    )
    SELECT c.id, c.doc_id, d.title AS doc_title, c.section_path, c.page, c.content,
           f.dense_rank, f.lex_rank, f.rrf_score
    FROM fused f
    JOIN chunks c ON c.id = f.id
    JOIN documents d ON d.doc_id = c.doc_id
    ORDER BY f.rrf_score DESC, c.id
    LIMIT %(k)s
    """

    def __init__(
        self,
        backend: PgBackend,
        embedding_version: int = 1,
        versions: dict[str, int] | None = None,
    ):
        self.backend = backend
        self.embedding_version = embedding_version
        # Per-collection embedding versions (from the registry). Ingestion and
        # the cache key already use the collection's version — retrieval must
        # filter on the same one, or a version bump silently empties search.
        self.versions = versions or {}

    def _emb_version(self, collection: str) -> int:
        return self.versions.get(collection, self.embedding_version)

    async def search(
        self,
        scope: QueryScope,
        collection: str,
        query: str,
        query_vector: Sequence[float],
        *,
        top_k: int = 30,
    ) -> list[Candidate]:
        async with self.backend.scoped_tx(scope) as conn:
            await conn.execute("SET LOCAL hnsw.ef_search = 60")
            if self.backend.supports_iterative_scan:
                await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
            cur = await conn.cursor(row_factory=dict_row).execute(
                self.SQL,
                {
                    "collection": collection,
                    "emb_v": self._emb_version(collection),
                    "qvec": vec_literal(query_vector),
                    "q_german": lex_queries(query)[0],
                    "q_simple": lex_queries(query)[1],
                    "k": top_k,
                    "rrf_k": self.backend.rrf_k,
                },
            )
            rows = await cur.fetchall()
        return [
            Candidate(
                chunk_id=str(r["id"]),
                doc_id=r["doc_id"],
                doc_title=r["doc_title"],
                section_path=r["section_path"],
                page=r["page"],
                content=r["content"],
                dense_rank=r["dense_rank"],
                lex_rank=r["lex_rank"],
                rrf_score=float(r["rrf_score"]),
            )
            for r in rows
        ]


class PgStore:
    """Idempotent ingestion + deletion cascade (DB part; cache/session/feedback
    orchestration lives in deletion.DeletionCascade)."""

    def __init__(self, backend: PgBackend):
        self.backend = backend

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
    ) -> str:
        """Returns 'unchanged' | 'created' | 'updated'.

        The new chunk generation is built and swapped inside ONE transaction —
        search never sees a mix of old and new chunks.
        """
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors length mismatch")
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.execute(
                "SELECT content_hash FROM documents WHERE doc_id = %s", (doc_id,)
            )
            row = await cur.fetchone()
            if row and row[0] == content_hash:
                return "unchanged"

            status = "updated" if row else "created"
            await conn.execute(
                """
                INSERT INTO documents (doc_id, tenant, collection, title, department, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE
                SET title = EXCLUDED.title,
                    collection = EXCLUDED.collection,
                    department = EXCLUDED.department,
                    content_hash = EXCLUDED.content_hash,
                    updated_at = now()
                """,
                (doc_id, scope.tenant, collection, title, department, content_hash),
            )
            # atomic generation swap
            await conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            for chunk, vec in zip(chunks, vectors, strict=True):
                await conn.execute(
                    """
                    INSERT INTO chunks (doc_id, tenant, collection, department, seq,
                                        section_path, page, is_table, token_estimate,
                                        content, embedding_version, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                    """,
                    (
                        doc_id,
                        scope.tenant,
                        collection,
                        department,
                        chunk.seq,
                        chunk.section_path,
                        None,
                        chunk.is_table,
                        chunk.token_estimate,
                        chunk.content,
                        embedding_version,
                        vec_literal(vec),
                    ),
                )
            await self._bump_corpus_version(conn, scope.tenant, collection)
            return status

    async def delete_document(self, scope: QueryScope, doc_id: str) -> DeletionReport:
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.execute(
                "SELECT collection, audit_ref FROM documents WHERE doc_id = %s", (doc_id,)
            )
            row = await cur.fetchone()
            if row is None:
                return DeletionReport(doc_id=doc_id)
            collection, audit_ref = row[0], str(row[1])
            cur = await conn.execute("SELECT count(*) FROM chunks WHERE doc_id = %s", (doc_id,))
            n_chunks = (await cur.fetchone())[0]  # type: ignore[index]
            # FK cascade removes chunks (and their vectors — they still encode the content)
            await conn.execute("DELETE FROM documents WHERE doc_id = %s", (doc_id,))
            cur = await conn.execute(
                "DELETE FROM feedback WHERE %s = ANY(cited_doc_ids) RETURNING id", (doc_id,)
            )
            n_feedback = len(await cur.fetchall())
            await self._bump_corpus_version(conn, scope.tenant, collection)
        return DeletionReport(
            doc_id=doc_id,
            found=True,
            chunks_deleted=n_chunks,
            feedback_rows_deleted=n_feedback,
            audit_ref=audit_ref,
        )

    async def audit_refs_for(self, scope: QueryScope, doc_ids: Sequence[str]) -> list[str]:
        """Surrogate audit refs for the given documents (A4) — one tiny scoped
        SELECT, separate from the retrieval SQL, which stays byte-identical."""
        if not doc_ids:
            return []
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.execute(
                "SELECT audit_ref FROM documents WHERE doc_id = ANY(%s)", (list(doc_ids),)
            )
            return [str(r[0]) for r in await cur.fetchall()]

    async def list_documents(self, scope: QueryScope, collection: str | None = None) -> list[dict]:
        # LEFT JOIN chunks for a per-doc count; RLS filters BOTH tables in the same tx.
        sql = (
            "SELECT d.doc_id, d.collection, d.title, d.department, d.updated_at, "
            "       count(c.id) AS chunk_count "
            "FROM documents d LEFT JOIN chunks c ON c.doc_id = d.doc_id"
        )
        params: tuple = ()
        if collection:
            sql += " WHERE d.collection = %s"
            params = (collection,)
        sql += (
            " GROUP BY d.doc_id, d.collection, d.title, d.department, d.updated_at"
            " ORDER BY d.doc_id"
        )
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(sql, params)
            return list(await cur.fetchall())

    async def stats(self, scope: QueryScope) -> dict:
        """RLS-scoped knowledge-base metrics for the dashboard — counts, never content."""
        async with self.backend.scoped_tx(scope) as conn:
            dcur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT d.collection, count(DISTINCT d.doc_id) AS documents, "
                "       count(c.id) AS chunks "
                "FROM documents d LEFT JOIN chunks c ON c.doc_id = d.doc_id "
                "GROUP BY d.collection ORDER BY d.collection"
            )
            per_collection = list(await dcur.fetchall())
            fcur = await conn.execute("SELECT count(*) FROM feedback")
            row = await fcur.fetchone()
            fb = int(row[0]) if row else 0
        return {
            "documents": sum(r["documents"] for r in per_collection),
            "chunks": sum(r["chunks"] for r in per_collection),
            "collections": len(per_collection),
            "feedback": fb,
            "per_collection": per_collection,
        }

    async def document_chunks(self, scope: QueryScope, doc_id: str) -> list[dict]:
        """The chunks of one document, in order — RLS-scoped. Shows exactly how the
        chunker split a document (section path, table flag, token estimate)."""
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.cursor(row_factory=dict_row).execute(
                "SELECT seq, section_path, page, is_table, token_estimate, content "
                "FROM chunks WHERE doc_id = %s ORDER BY seq",
                (doc_id,),
            )
            return list(await cur.fetchall())

    async def content_hash(self, scope: QueryScope, doc_id: str) -> str | None:
        """Current stored content hash for a document, or None if absent —
        lets ingestion skip the (CPU-expensive) embedding step when a re-seed
        would be a no-op. RLS-scoped like everything else."""
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.execute(
                "SELECT content_hash FROM documents WHERE doc_id = %s", (doc_id,)
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def corpus_version(self, scope: QueryScope, collection: str) -> int:
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.execute(
                "SELECT corpus_version FROM collection_state WHERE tenant=%s AND collection=%s",
                (scope.tenant, collection),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def add_feedback(
        self,
        scope: QueryScope,
        collection: str,
        rating: int,
        condensed_question: str,
        route: str,
        cited_doc_ids: Sequence[str],
    ) -> None:
        async with self.backend.scoped_tx(scope) as conn:
            await conn.execute(
                """
                INSERT INTO feedback (tenant, collection, department, user_id, rating,
                                      condensed_question, route, cited_doc_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    scope.tenant,
                    collection,
                    scope.department,
                    scope.user_id,
                    rating,
                    condensed_question,
                    route,
                    list(cited_doc_ids),
                ),
            )

    async def delete_user_feedback(self, scope: QueryScope, user_id: str) -> int:
        """Delete feedback rows written by one user (self-service deletion).
        Runs inside the caller's scoped transaction — RLS bounds the statement
        to the tenant/department view the caller legitimately has."""
        async with self.backend.scoped_tx(scope) as conn:
            cur = await conn.execute("DELETE FROM feedback WHERE user_id = %s", (user_id,))
            return cur.rowcount or 0

    async def purge_expired_feedback(self, retention_days: int) -> int:
        """Retention enforcement via the SECURITY DEFINER function
        (db/init/03_retention.sql): retention must cover every tenant and
        department, which the per-request RLS view deliberately cannot —
        the function is the narrowly-scoped, content-free bridge."""
        async with self.backend.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT purge_expired_feedback(make_interval(days => %s))",
                (retention_days,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    async def _bump_corpus_version(conn: AsyncConnection, tenant: str, collection: str) -> None:
        """Any corpus change invalidates the answer cache implicitly (cache key component)."""
        await conn.execute(
            """
            INSERT INTO collection_state (tenant, collection, corpus_version)
            VALUES (%s, %s, 1)
            ON CONFLICT (tenant, collection)
            DO UPDATE SET corpus_version = collection_state.corpus_version + 1
            """,
            (tenant, collection),
        )
