"""lex_queries contract: german leg = language, simple leg = identifiers ONLY.

The simple leg once accepted any word ≥5 chars — ordinary German words matched
unstemmed everywhere and, score-summed with the german leg, pushed word-overlap
noise above exact identifier hits (measured: the LJ70-E501 anchor at lex rank 6).
These tests pin the leg contract so it cannot silently widen again.
"""

from rag_assistant.store.pg import lex_queries


def test_german_leg_keeps_content_words():
    german, simple = lex_queries("Was bedeutet der VPN-Fehler NF-4102?")
    assert "bedeutet" in german and "VPN-Fehler" in german
    assert "NF-4102" in simple


def test_simple_leg_empty_without_identifiers():
    german, simple = lex_queries("Wie viele Zeichen muss ein Passwort heute mindestens haben?")
    assert simple == ""  # "Passwort", "haben", "heute" are NOT identifiers
    assert "Passwort" in german and "mindestens" in german


def test_simple_leg_catches_codes_hyphens_and_short_ids():
    _, simple = lex_queries("Der Drucker LJ-90 zeigt LJ90-E501, Gehaltsband E3 fehlt")
    for tok in ("LJ-90", "LJ90-E501", "E3"):
        assert tok in simple
    assert "Drucker" not in simple and "Gehaltsband" not in simple


def test_german_leg_drops_short_tokens():
    german, _ = lex_queries("Wo ist der Sammelplatz am Standort Köln?")
    assert "Sammelplatz" in german and "Standort" in german
    assert " am " not in f" {german} "  # 2-char tokens never enter the query
