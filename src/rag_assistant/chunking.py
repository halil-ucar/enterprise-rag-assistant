"""Structure-aware chunking with header-path prefix.

Decisions (docs/ARCHITECTURE.md §ingestion):
- Split along Markdown headers, not fixed windows — fixed-size cuts procedures in half.
  (Fixed-size remains available as an eval comparison mode.)
- Markdown tables become standalone chunks (a split table is garbage for retrieval).
- Every chunk gets its header path as prefix ("Doc > Section > Subsection: ...") —
  context travels into the embedding at zero LLM cost.
- Oversized sections are sub-split with overlap; the prefix is repeated on each part.
"""

from __future__ import annotations

import re

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from .domain import ChunkDraft

# ~500 tokens target, ~600 max; token ≈ 4 chars for German/English mix.
TARGET_CHARS = 2000
MAX_CHARS = 2400
OVERLAP_CHARS = 200

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _section_path(doc_title: str, meta: dict[str, str]) -> str:
    parts = [doc_title] + [
        meta[k] for k, _ in (("h1", 1), ("h2", 2), ("h3", 3), ("h4", 4)) if k in meta
    ]
    # de-duplicate consecutive repeats (h1 often equals the doc title)
    deduped: list[str] = []
    for p in parts:
        if not deduped or deduped[-1].strip().lower() != p.strip().lower():
            deduped.append(p.strip())
    return " > ".join(deduped)


def _split_out_tables(text: str) -> list[tuple[str, bool]]:
    """Return [(block, is_table), ...] preserving order. A table = ≥2 consecutive |…| lines."""
    blocks: list[tuple[str, bool]] = []
    current: list[str] = []
    table: list[str] = []

    def flush_current() -> None:
        joined = "\n".join(current).strip()
        if joined:
            blocks.append((joined, False))
        current.clear()

    def flush_table() -> None:
        if len(table) >= 2:
            flush_current()  # text preceding the table comes first
            blocks.append(("\n".join(table).strip(), True))
        else:
            current.extend(table)  # a single |…| line is not a real table
        table.clear()

    for line in text.splitlines():
        if _TABLE_LINE.match(line):
            table.append(line)
        else:
            if table:
                flush_table()
            current.append(line)
    if table:
        flush_table()
    flush_current()
    return blocks


def chunk_markdown(markdown: str, doc_title: str) -> list[ChunkDraft]:
    """Chunk one Markdown document. Deterministic, pure."""
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=_HEADERS, strip_headers=True)
    sub_splitter = RecursiveCharacterTextSplitter(
        chunk_size=TARGET_CHARS, chunk_overlap=OVERLAP_CHARS, separators=["\n\n", "\n", ". ", " "]
    )

    drafts: list[ChunkDraft] = []
    seq = 0
    for section in header_splitter.split_text(markdown):
        path = _section_path(doc_title, section.metadata)
        prefix = f"{path}:\n\n"
        for block, is_table in _split_out_tables(section.page_content):
            if is_table:
                parts = [block]  # tables are never sub-split
            elif len(block) > MAX_CHARS:
                parts = sub_splitter.split_text(block)
            else:
                parts = [block]
            for part in parts:
                content = prefix + part.strip()
                drafts.append(
                    ChunkDraft(
                        seq=seq,
                        content=content,
                        section_path=path,
                        is_table=is_table,
                        token_estimate=estimate_tokens(content),
                    )
                )
                seq += 1
    return drafts


def chunk_fixed_size(markdown: str, doc_title: str, size_chars: int = 2000) -> list[ChunkDraft]:
    """Naive fixed-size baseline — exists ONLY as the eval comparison mode."""
    splitter = RecursiveCharacterTextSplitter(chunk_size=size_chars, chunk_overlap=200)
    return [
        ChunkDraft(
            seq=i,
            content=part,
            section_path=doc_title,
            token_estimate=estimate_tokens(part),
        )
        for i, part in enumerate(splitter.split_text(markdown))
    ]
