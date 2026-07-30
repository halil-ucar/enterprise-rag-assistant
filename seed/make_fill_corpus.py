"""Generated haystack — deterministic distractor mass (0 LLM tokens).

The curated core (core_spec.py) carries retrieval QUALITY via designed hard negatives.
This generator carries SCALE: thousands of template-assembled documents so the right
answer must be found among a realistic crowd, and so index size / latency become
measurable. No document here is ever a golden anchor — pure distractors.

Deterministic (`random.Random(SEED)`): byte-identical on every machine, so eval numbers
stay comparable. NEVER committed (git-ignored) — regenerate on demand.

DISJOINT NAMESPACE: the vocabulary below shares no *distinctive* token with the core's
reserved set (system names, error codes, multi-word facts) — asserted by `--selftest`.
Bare short numbers are allowed to overlap; anchors are doc_id-based, so a fill doc that
happens to contain "40" can never become a false answer to a core question.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

from core_spec import reserved_tokens

OUT = Path(__file__).parent / "corpus-fill"
SEED = 42

# vocabulary — deliberately disjoint from the core reserved tokens
SYSTEMS = [
    "APPGATE",
    "LOGSTREAM",
    "DATAHUB",
    "MESHPOINT",
    "CACHEGRID",
    "FLOWDECK",
    "NODERING",
    "BYTEVAULT",
    "SYNCWAVE",
    "GRIDLOCK",
    "PIXELPORT",
    "QUARTZDB",
    "RELAYBOX",
    "TIDEMARK",
    "VECTORPORT",
    "WAVEFORM",
]
THEMEN = [
    "Datenarchivierung",
    "Lizenzpflege",
    "Schnittstellenbetrieb",
    "Protokollierung",
    "Kapazitätsplanung",
    "Patch-Management",
    "Zertifikatsverwaltung",
    "Lastverteilung",
    "Datenexport",
    "Rollenverwaltung",
    "Wartungsplanung",
    "Ereignisüberwachung",
]
BEREICHE = [
    "Bereich Nord",
    "Bereich Süd",
    "Bereich Ost",
    "Aussenstelle West",
    "Rechenzentrum 2",
    "Etage 3",
    "Laborumgebung",
    "Testcluster",
]
ROLLEN = [
    "Betriebsteam",
    "Anwendungsbetreuung",
    "Servicedesk",
    "Infrastrukturteam",
    "Qualitätssicherung",
    "Release-Management",
]
VERBEN = ["prüft", "überwacht", "dokumentiert", "koordiniert", "aktualisiert", "protokolliert"]


def _doc(rng: random.Random, i: int) -> dict:
    kind = rng.choice(["runbook", "richtlinie", "protokoll", "changelog"])
    sysname = f"{rng.choice(SYSTEMS)}-{rng.randint(100, 899)}"
    thema = rng.choice(THEMEN)
    bereich = rng.choice(BEREICHE)
    rolle = rng.choice(ROLLEN)
    code = f"GX-{rng.randint(100, 999)}"  # disjoint from M-/A-/VPN-/LJ-/ERP-/CRM-
    header = (
        f"> **Dokument-Nr.** NF-FILL-{i:05d} · **Version** 1.{rng.randint(0, 9)} "
        f"· **Stand** 2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d} "
        f"· **Verantwortlich** {rolle}\n\n"
    )

    if kind == "runbook":
        title = f"Runbook {sysname}"
        body = (
            f"# {title}\n\n{header}"
            f"## System\n\nDas System {sysname} unterstützt die {thema} im {bereich}. "
            f"Der Betrieb erfolgt durch das {rolle}, das den Zustand regelmäßig {rng.choice(VERBEN)}.\n\n"
            f"## Betrieb\n\nRoutineläufe finden werktags statt. Bei Störungen mit Kennung {code} "
            f"wird der Dienst über die Verwaltungskonsole neu gestartet und der Vorgang protokolliert.\n\n"
            f"## Wartung\n\nWartungsfenster werden im Vorfeld angekündigt. Nicht mehr benötigte "
            f"Daten werden gemäß der {thema} bereinigt.\n"
        )
    elif kind == "richtlinie":
        title = f"Richtlinie {thema} — {bereich}"
        body = (
            f"# {title}\n\n{header}"
            f"## Geltungsbereich\n\nDiese Richtlinie regelt die {thema} im {bereich}. "
            f"Sie gilt für alle Mitarbeitenden, die dort tätig sind.\n\n"
            f"## Regelungen\n\nDie Umsetzung wird durch das {rolle} begleitet. Abweichungen "
            f"sind zu begründen und über das interne Portal zu beantragen.\n\n"
            f"## Verantwortlich\n\nFür Rückfragen steht das {rolle} zur Verfügung.\n"
        )
    elif kind == "protokoll":
        title = f"Protokoll {thema} {bereich}"
        body = (
            f"# {title}\n\n{header}"
            f"## Teilnehmende\n\nAm Abstimmungstermin nahm das {rolle} teil. "
            f"Thema war die {thema} im {bereich}.\n\n"
            f"## Beschlüsse\n\nDas {rolle} {rng.choice(VERBEN)} die weiteren Schritte. "
            f"Die nächste Prüfung von {sysname} erfolgt im Folgemonat.\n\n"
            f"## Offene Punkte\n\nEinzelne Punkte zur {thema} bleiben offen und werden vertagt.\n"
        )
    else:  # changelog
        title = f"Changelog {sysname}"
        body = (
            f"# {title}\n\n{header}"
            f"## Änderungen\n\nDie aktuelle Fassung von {sysname} verbessert die {thema}. "
            f"Das {rolle} hat die Anpassungen abgenommen.\n\n"
            f"## Hinweise\n\nBekannte Meldung {code} tritt in seltenen Fällen auf und ist unkritisch.\n"
        )

    # 90% general handbuecher/all, 10% a separate hr slice (department hr)
    if rng.random() < 0.1:
        collection, department = "hr", "hr"
    else:
        collection, department = "handbuecher", "all"
    return {
        "doc_id": f"fill-{i:05d}",
        "title": title,
        "collection": collection,
        "department": department,
        "markdown": body,
    }


def generate(n: int) -> list[dict]:
    rng = random.Random(SEED)
    return [_doc(rng, i) for i in range(n)]


def _distinctive(tokens: set[str]) -> set[str]:
    """Reserved tokens worth guarding: contain a letter/hyphen or are long. Bare
    short numbers ('14','40','500') are excluded — they cannot corrupt doc-anchored
    Recall and are unavoidable in any realistic text."""
    out = set()
    for t in tokens:
        if any(c.isalpha() for c in t) or "-" in t or len(t) > 6:
            out.add(t.lower())
    return out


def _leaks(docs: list[dict], guarded: set[str]) -> list[str]:
    """Guarded tokens that appear as WHOLE tokens in the corpus.

    Token-boundary matching, not substring: 'WAVEFORM-101' must not count as a
    leak of 'M-101' — no tokenizer (incl. Postgres 'simple' FTS) would split it
    that way, and anchors are doc_id-based regardless. A bare substring test
    produced exactly that false positive at n>=2000.
    """
    blob = "\n".join(d["markdown"] for d in docs).lower()
    hits = []
    for t in guarded:
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", blob):
            hits.append(t)
    return sorted(hits)


def selftest(n: int = 2000) -> int:
    # Test the ACTUAL default output size, not a fixed 500-sample: a guarantee
    # that only holds for a subset the generator never ships is not a guarantee.
    a = generate(n)
    b = generate(n)
    assert a == b, "non-deterministic output"
    guarded = _distinctive(reserved_tokens())
    leaks = _leaks(a, guarded)
    assert not leaks, f"fill corpus leaks reserved tokens: {leaks}"
    ids = [d["doc_id"] for d in a]
    assert len(ids) == len(set(ids)), "duplicate doc_ids"
    print(f"selftest OK — deterministic, {len(guarded)} guarded tokens, 0 leaks, {len(a)} docs")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=2000, help="number of fill documents")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest(args.n)

    OUT.mkdir(parents=True, exist_ok=True)
    docs = generate(args.n)
    manifest = []
    for d in docs:
        (OUT / f"{d['doc_id']}.md").write_text(d["markdown"], encoding="utf-8")
        manifest.append(
            {
                "doc_id": d["doc_id"],
                "title": d["title"],
                "collection": d["collection"],
                "department": d["department"],
            }
        )
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {len(docs)} fill docs to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
