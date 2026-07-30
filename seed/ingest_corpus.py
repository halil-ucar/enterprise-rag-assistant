"""Ingest a corpus directly through the store (no API needed).

Corpus staging (--corpus):
  smoke  seed/corpus/       12 hand docs — CI plumbing, fast tests (default)
  core   seed/corpus-core/  curated hard negatives — retrieval QUALITY
  full   core + seed/corpus-fill/  + generated haystack — SCALE / latency

Prefers PDFs (real parsing stage via docling) and falls back to the Markdown twins
when docling isn't installed — the ingestion result is identical either way because
Markdown is the intermediate format.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from rag_assistant.chunking import chunk_markdown
from rag_assistant.config import get_registry, get_settings
from rag_assistant.domain import QueryScope
from rag_assistant.embeddings import build_embedder
from rag_assistant.parsing import UnsupportedFormatError, parse_to_markdown
from rag_assistant.store.pg import PgBackend, PgStore

SEED_DIR = Path(__file__).parent
CORPUS_DIRS = {
    "smoke": [SEED_DIR / "corpus"],
    "core": [SEED_DIR / "corpus-core"],
    "full": [SEED_DIR / "corpus-core", SEED_DIR / "corpus-fill"],
}


def _load_manifest(corpus_dir: Path) -> list[dict]:
    path = corpus_dir / "manifest.json"
    if not path.exists():
        raise SystemExit(f"missing {path} — run the corpus generator for this stage first")
    entries = json.loads(path.read_text(encoding="utf-8"))
    for e in entries:
        e["_dir"] = corpus_dir
    return entries


async def _ingest_one(store: PgStore, embedder, registry, entry: dict) -> tuple[str, int, str]:
    doc_id = entry["doc_id"]
    corpus_dir: Path = entry["_dir"]
    pdf = corpus_dir / f"{doc_id}.pdf"
    md = corpus_dir / f"{doc_id}.md"
    markdown, source = "", ""
    if pdf.exists():
        try:
            markdown = parse_to_markdown(pdf.read_bytes(), "pdf")
            source = "pdf(docling)"
        except UnsupportedFormatError:
            pass
    if not markdown:
        markdown = md.read_text(encoding="utf-8")
        source = "md"

    content_hash = hashlib.sha256(markdown.encode()).hexdigest()
    col = registry.get(entry["collection"])
    dept = entry["department"] if entry["department"] != "all" else "it"
    scope = QueryScope(tenant=registry.default_tenant, user_id="seed", department=dept)
    # Skip embedding when the document is unchanged — re-seeding an unchanged
    # corpus (make refresh) otherwise pays the full CPU embedding cost every time.
    if await store.content_hash(scope, doc_id) == content_hash:
        return "unchanged", 0, source
    chunks = chunk_markdown(markdown, entry["title"])
    vectors = await embedder.embed([c.content for c in chunks])
    status = await store.upsert_document(
        scope,
        entry["collection"],
        doc_id,
        entry["title"],
        content_hash,
        entry["department"],
        chunks,
        vectors,
        col.embedding_version if col else 1,
    )
    return status, len(chunks), source


async def _foreign_docs(store: PgStore, registry, entries: list[dict]) -> dict[str, str]:
    """Documents in the target collections that are NOT part of this tier's
    manifest (other tiers, UI uploads). Returns doc_id → a department scope
    that can see it. Checked under both demo departments so it/hr/all are covered."""
    keep = {e["doc_id"] for e in entries}
    collections = {e["collection"] for e in entries}
    foreign: dict[str, str] = {}
    for dept in ("it", "hr"):
        scope = QueryScope(tenant=registry.default_tenant, user_id="seed", department=dept)
        for d in await store.list_documents(scope):
            if d["collection"] in collections and d["doc_id"] not in keep:
                foreign.setdefault(d["doc_id"], dept)
    return foreign


async def _reset_foreign(store: PgStore, registry, entries: list[dict]) -> int:
    """Delete foreign-tier docs from the target collections — otherwise switching
    smoke↔core↔full silently mixes corpora on one DB (upsert never removes
    anything), and the eval then scores against a contaminated index.
    Deliberately OPT-IN (--reset): it also removes UI uploads, so the additive
    default (make seed / make refresh) only WARNS instead of deleting."""
    removed = 0
    for doc_id, dept in (await _foreign_docs(store, registry, entries)).items():
        scope = QueryScope(tenant=registry.default_tenant, user_id="seed", department=dept)
        await store.delete_document(scope, doc_id)
        removed += 1
    return removed


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", choices=list(CORPUS_DIRS), default="smoke")
    parser.add_argument("--quiet", action="store_true", help="summary only (for large stages)")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete docs of OTHER tiers from the target collections first "
        "(prevents cross-tier contamination of eval numbers)",
    )
    args = parser.parse_args()

    settings = get_settings()
    registry = get_registry()
    backend = PgBackend(settings.database_url, rrf_k=settings.rrf_k)
    await backend.open()
    store = PgStore(backend)
    embedder = build_embedder(settings)

    entries: list[dict] = []
    for d in CORPUS_DIRS[args.corpus]:
        entries.extend(_load_manifest(d))

    if args.reset:
        removed = await _reset_foreign(store, registry, entries)
        print(f"reset: removed {removed} foreign-tier doc(s) from the target collections")
    else:
        foreign = await _foreign_docs(store, registry, entries)
        if foreign:
            print(
                f"WARNING: {len(foreign)} doc(s) in the target collections are not part of "
                f"this tier's manifest (other tier or uploads). Retrieval and eval numbers "
                f"will include them — pass --reset for a clean, comparable index."
            )

    total_chunks = 0
    for i, entry in enumerate(entries, 1):
        status, n, source = await _ingest_one(store, embedder, registry, entry)
        total_chunks += n
        if not args.quiet:
            print(f"{entry['doc_id']:32s} {status:9s} chunks={n:3d} via {source}")
        elif i % 200 == 0:
            print(f"  … {i}/{len(entries)} docs, {total_chunks} chunks")

    await backend.close()
    print(f"seed complete: corpus={args.corpus}, {len(entries)} docs, {total_chunks} chunks")


if __name__ == "__main__":
    asyncio.run(main())
