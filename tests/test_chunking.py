from rag_assistant.chunking import MAX_CHARS, chunk_fixed_size, chunk_markdown

DOC = """# IT-Handbuch

## VPN

### Fehlerbehebung

Wenn der VPN-Client den Fehler NF-4102 zeigt, starten Sie den Dienst neu.

| Fehlercode | Bedeutung | Lösung |
|------------|-----------|--------|
| NF-4102    | Zertifikat abgelaufen | Zertifikat erneuern |
| NF-4200    | Tunnel getrennt | Neu verbinden |

Danach den Client neu starten.

## Drucker

Der Etikettendrucker im Lager benötigt Spezialpapier.
"""


def test_header_path_prefix_present():
    chunks = chunk_markdown(DOC, "IT-Handbuch")
    vpn = [c for c in chunks if "NF-4102" in c.content and not c.is_table]
    assert vpn, "expected a text chunk containing the error code"
    assert vpn[0].content.startswith("IT-Handbuch > VPN > Fehlerbehebung:")
    assert vpn[0].section_path == "IT-Handbuch > VPN > Fehlerbehebung"


def test_doc_title_not_duplicated_when_h1_equals_title():
    chunks = chunk_markdown(DOC, "IT-Handbuch")
    assert all(not c.section_path.startswith("IT-Handbuch > IT-Handbuch") for c in chunks)


def test_tables_become_standalone_chunks():
    chunks = chunk_markdown(DOC, "IT-Handbuch")
    tables = [c for c in chunks if c.is_table]
    assert len(tables) == 1
    assert "NF-4200" in tables[0].content
    assert tables[0].content.startswith("IT-Handbuch > VPN > Fehlerbehebung:")
    # surrounding prose is NOT inside the table chunk
    assert "starten Sie den Dienst neu" not in tables[0].content


def test_text_order_preserved_around_table():
    chunks = chunk_markdown(DOC, "IT-Handbuch")
    flat = [c.content for c in chunks]
    i_before = next(i for i, c in enumerate(flat) if "starten Sie den Dienst neu" in c)
    i_table = next(i for i, c in enumerate(flat) if "NF-4200" in c)
    i_after = next(i for i, c in enumerate(flat) if "Danach den Client neu starten" in c)
    assert i_before < i_table < i_after


def test_oversized_sections_are_subsplit():
    big = "# Doc\n\n## Lang\n\n" + ("Satz mit Inhalt. " * 400)
    chunks = chunk_markdown(big, "Doc")
    assert len(chunks) > 1
    assert all(len(c.content) <= MAX_CHARS + 200 for c in chunks)  # prefix headroom
    assert all(c.content.startswith("Doc > Lang:") for c in chunks)


def test_fixed_size_baseline_exists_for_eval():
    chunks = chunk_fixed_size(DOC, "IT-Handbuch")
    assert chunks and all(c.section_path == "IT-Handbuch" for c in chunks)
