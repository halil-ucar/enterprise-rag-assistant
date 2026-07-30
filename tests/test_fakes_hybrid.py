"""The fake layer must be good enough to demonstrate hybrid > single-leg retrieval."""

from rag_assistant.chunking import chunk_markdown
from rag_assistant.domain import QueryScope
from rag_assistant.testing.fakes import FakeEmbedder, InMemoryRetriever

SCOPE_IT = QueryScope(tenant="nordfels", user_id="anna", department="it")
SCOPE_HR = QueryScope(tenant="nordfels", user_id="ben", department="hr")

VPN_DOC = """# VPN-Handbuch

## Fehlerbehebung

Der Fehlercode NF-4102 bedeutet: Zertifikat abgelaufen. Lösung: Zertifikat erneuern.
"""

HR_DOC = """# Gehaltsbänder

## Übersicht

Interne Gehaltsbänder der Abteilung HR.
"""


async def _retriever() -> InMemoryRetriever:
    r = InMemoryRetriever(FakeEmbedder())
    await r.add(
        "handbuecher", "doc-vpn", "VPN-Handbuch", "all", chunk_markdown(VPN_DOC, "VPN-Handbuch")
    )
    await r.add("hr", "doc-gehalt", "Gehaltsbänder", "hr", chunk_markdown(HR_DOC, "Gehaltsbänder"))
    return r


async def test_exact_error_code_is_found():
    r = await _retriever()
    qv = (await r.embedder.embed(["NF-4102"]))[0]
    results = await r.search(SCOPE_IT, "handbuecher", "Was bedeutet NF-4102?", qv, top_k=5)
    assert results and "NF-4102" in results[0].content


async def test_rank_metadata_populated_for_debug_panel():
    r = await _retriever()
    qv = (await r.embedder.embed(["Zertifikat abgelaufen"]))[0]
    results = await r.search(SCOPE_IT, "handbuecher", "Zertifikat abgelaufen", qv, top_k=5)
    top = results[0]
    assert top.rrf_score > 0
    assert top.dense_rank is not None or top.lex_rank is not None


async def test_department_scoping_hides_hr_docs():
    r = await _retriever()
    qv = (await r.embedder.embed(["Gehaltsbänder"]))[0]
    it_results = await r.search(SCOPE_IT, "hr", "Gehaltsbänder", qv, top_k=5)
    hr_results = await r.search(SCOPE_HR, "hr", "Gehaltsbänder", qv, top_k=5)
    assert it_results == []
    assert hr_results and hr_results[0].doc_id == "doc-gehalt"


async def test_deletion_removes_from_retrieval():
    r = await _retriever()
    await r.delete_document("doc-vpn")
    qv = (await r.embedder.embed(["NF-4102"]))[0]
    assert await r.search(SCOPE_IT, "handbuecher", "NF-4102", qv, top_k=5) == []
