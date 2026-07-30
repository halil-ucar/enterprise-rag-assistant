"""Verify every golden anchor resolves against the INGESTED corpus (0 LLM tokens).

Runs AFTER `ingest_corpus.py`. For each anchored question it checks that a chunk
actually exists with the expected doc_id and a section_path containing the expected
substring, under the question's RLS scope. Refusal/unanswerable questions carry no
anchor and are skipped (their contract is checked by the answer eval).

Exit code 1 if any anchor fails — a broken anchor means the corpus and the golden set
drifted apart, and every downstream Recall/MRR number would be meaningless.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

from rag_assistant.config import get_settings
from rag_assistant.domain import QueryScope
from rag_assistant.store.pg import PgBackend

USERS = {
    "anna": QueryScope(tenant="nordfels", user_id="anna", department="it"),
    "ben": QueryScope(tenant="nordfels", user_id="ben", department="hr"),
}


async def _anchor_ok(
    backend: PgBackend, scope: QueryScope, collection: str, doc_id: str, section_sub: str
) -> bool:
    async with backend.scoped_tx(scope) as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM chunks "
            "WHERE collection = %s AND doc_id = %s AND section_path ILIKE %s",
            (collection, doc_id, f"%{section_sub}%"),
        )
        row = await cur.fetchone()
    return bool(row and row[0] > 0)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default=str(Path(__file__).parent / "golden_set_core.yaml"))
    args = parser.parse_args()

    questions = yaml.safe_load(Path(args.golden).read_text(encoding="utf-8"))["questions"]
    settings = get_settings()
    backend = PgBackend(settings.database_url, rrf_k=settings.rrf_k)
    await backend.open()

    checked = failed = skipped = 0
    for q in questions:
        scope = USERS[q.get("user", "anna")]
        collection = q.get("collection", "handbuecher")
        section = q.get("expected_section_contains", "")

        if q.get("expected_doc"):
            docs = [q["expected_doc"]]
        elif q.get("expected_doc_any"):
            docs = q["expected_doc_any"]
        else:
            skipped += 1  # refusal / unanswerable — no anchor
            continue

        checked += 1
        if q.get("expected_doc_any"):
            ok = False
            for d in docs:
                if await _anchor_ok(backend, scope, collection, d, section):
                    ok = True
                    break
        else:
            ok = await _anchor_ok(backend, scope, collection, docs[0], section)

        if not ok:
            failed += 1
            print(
                f"FAIL {q['id']}: no chunk for doc={docs} section~='{section}' "
                f"(collection={collection}, user={q.get('user', 'anna')})"
            )

    await backend.close()
    print(
        f"\nanchors: {checked - failed}/{checked} resolved, {skipped} refusal/unanswerable skipped"
    )
    if failed:
        print(f"{failed} broken anchor(s) — corpus and golden set drifted. Fix before eval.")
        return 1
    print("all anchors resolve ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
