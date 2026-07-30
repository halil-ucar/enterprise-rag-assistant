"""Context assembly + citation validation (the underrated last meter).

- Few, highly relevant passages beat many: precision AND latency (short prefill).
- Dedup by (doc_id, section_path): the same section never enters twice.
- Hard token budget.
- Citations are inline markers [S#] validated POST-generation against the
  actually-provided sources — the model cannot invent a source that passes.
  Stable anchors: doc + section path (+ page), never volatile chunk indexes.
"""

from __future__ import annotations

import re

from .domain import Candidate, Citation
from .prompts import REFUSAL_TEXT, document_block

_MARKER = re.compile(r"\[S(\d+)\]")


class ContextBundle:
    def __init__(self, text: str, sources: dict[str, Candidate]):
        self.text = text
        self.sources = sources  # "S1" -> Candidate

    @property
    def cited_doc_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for cand in self.sources.values():
            seen.setdefault(cand.doc_id, None)
        return list(seen)


def assemble_context(
    candidates: list[Candidate], top_n: int = 5, token_budget: int = 4000
) -> ContextBundle:
    """Pick top_n candidates (already rank-ordered), dedup by section, respect budget.
    Marks the chosen candidates with in_context=True (feeds the debug panel)."""
    chosen: list[Candidate] = []
    seen_sections: set[tuple[str, str]] = set()
    budget = token_budget
    for cand in candidates:
        key = (cand.doc_id, cand.section_path)
        if key in seen_sections:
            continue
        cost = max(1, len(cand.content) // 4)
        if cost > budget and chosen:
            continue
        chosen.append(cand)
        seen_sections.add(key)
        budget -= cost
        if len(chosen) >= top_n:
            break

    sources: dict[str, Candidate] = {}
    blocks: list[str] = []
    for i, cand in enumerate(chosen, start=1):
        marker = f"S{i}"
        cand.in_context = True
        sources[marker] = cand
        blocks.append(document_block(marker, cand.doc_title, cand.section_path, cand.content))
    return ContextBundle(text="\n\n".join(blocks), sources=sources)


def validate_citations(text: str, bundle: ContextBundle) -> tuple[str, list[Citation], bool]:
    """Post-validate inline markers against the provided sources.

    Returns (clean_text, citations_in_order, all_markers_valid).
    Unknown markers (hallucinated sources) are stripped from the text.
    """
    seen: dict[str, None] = {}
    all_valid = True

    def _check(m: re.Match[str]) -> str:
        marker = f"S{m.group(1)}"
        if marker in bundle.sources:
            seen.setdefault(marker, None)
            return m.group(0)
        nonlocal all_valid
        all_valid = False
        return ""  # strip hallucinated source markers

    clean = _MARKER.sub(_check, text)
    citations = [
        Citation(
            marker=marker,
            doc_id=bundle.sources[marker].doc_id,
            doc_title=bundle.sources[marker].doc_title,
            section_path=bundle.sources[marker].section_path,
            page=bundle.sources[marker].page,
        )
        for marker in seen
    ]
    return clean, citations, all_valid


def is_refusal(text: str) -> bool:
    return REFUSAL_TEXT.lower() in text.lower()


EXTRACTIVE_HEADER = "Fundstellen aus der Wissensbasis (extraktiver Modus, ohne Sprachmodell):"


def extractive_answer(bundle: ContextBundle) -> tuple[str, list[Citation]]:
    """Answer for generation='extractive' collections: the retrieved passages
    themselves, verbatim, with the same [S#] markers a generated answer carries.
    No LLM sees the question or the passages. Empty retrieval → the standard
    refusal, so threshold behaviour stays identical to the generated path."""
    if not bundle.sources:
        return REFUSAL_TEXT, []
    blocks: list[str] = []
    citations: list[Citation] = []
    for marker, cand in bundle.sources.items():
        blocks.append(f"[{marker}] {cand.doc_title} — {cand.section_path}:\n{cand.content}")
        citations.append(
            Citation(
                marker=marker,
                doc_id=cand.doc_id,
                doc_title=cand.doc_title,
                section_path=cand.section_path,
                page=cand.page,
            )
        )
    return EXTRACTIVE_HEADER + "\n\n" + "\n\n".join(blocks), citations
