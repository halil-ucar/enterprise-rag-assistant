> **Dokument-Nr.** NF-DOC-BACKUP-2023 · **Version** 2023.1 · **Stand** 2023-03-01 · **Verantwortlich** IT-Betrieb / Team Infrastruktur (Nordfels IT GmbH, Standort Hagen) · **Status: ABGELÖST**

| Version | Datum | Bearbeitung | Änderung |
|---|---|---|---|
| 2022.4 | 2022-12-15 | R. Kahle | Entwurf für die Neufassung erstellt |
| 2023.0 | 2023-02-10 | T. Ostermann | Interne Review Infrastruktur und Datenschutz |
| 2023.1 | 2023-03-01 | R. Kahle | Freigabe; inzwischen abgelöst durch das Konzept 2026 |

# Backup-Konzept 2023 (abgelöst)

## Geltung

Dieses Backup-Konzept war konzernweit für alle produktiven Systeme der Nordfels IT GmbH an den Standorten Hagen und Köln verbindlich. **Dieses Dokument ist abgelöst.** Abgelöst durch das Konzept 2026: Ab dem Inkrafttreten der Neufassung gelten ausschließlich deren Vorgaben, und diese Fassung darf nicht länger als Referenz herangezogen werden. Alle Regelungen dieses Dokuments sind mit dem Wirksamwerden des Nachfolgekonzepts gegenstandslos. Verantwortlich für die Umsetzung war das Team Infrastruktur; fachliche Rückfragen sind an den IT-Betrieb zu richten. Abweichungen im Einzelfall bedurften einer schriftlichen Genehmigung durch die IT-Leitung und waren zu dokumentieren. Die Wirksamkeit des Konzepts wurde jährlich überprüft und bei Bedarf angepasst.

## Kennzahlen

Für die produktiven Systeme galt ein maximaler Datenverlust (RPO) von 24 Stunden. Das bedeutet, dass im Fehlerfall höchstens die in diesem Zeitraum angefallenen Datenänderungen verloren gehen durften; die Sicherungsintervalle waren entsprechend zu takten. Ergänzend war eine Wiederherstellzeit (RTO) von 4 Stunden vorgesehen, innerhalb derer ein betroffenes System nach einer Störung wieder produktiv verfügbar sein musste. Diese Kennzahlen waren bei der Planung von Wartungsfenstern und Kapazitäten zwingend zu berücksichtigen. Zur Absicherung der Zielwerte waren Wiederherstellungstests regelmäßig durchzuführen und die gemessenen Zeiten zu protokollieren. Wurden die Kennzahlen in einem Test verfehlt, war unverzüglich eine Ursachenanalyse einzuleiten und eine Korrekturmaßnahme festzulegen. Die Verantwortung für die Einhaltung lag beim IT-Betrieb, der die Ergebnisse quartalsweise an die IT-Leitung berichtete.

## Aufbewahrung

Die täglichen Sicherungen (Tagessicherungen) wurden 14 Tage vorgehalten und standen in diesem Zeitraum für eine punktgenaue Wiederherstellung zur Verfügung. Nach Ablauf der Frist wurden die entsprechenden Sicherungssätze automatisiert und nachvollziehbar gelöscht, sofern keine gesetzliche oder vertragliche Aufbewahrungspflicht entgegenstand. Wochensicherungen und Monatssicherungen wurden nach dem etablierten Generationenprinzip zusätzlich vorgehalten, um auch länger zurückliegende Zustände rekonstruieren zu können. Die Sicherungsmedien waren an beiden Standorten redundant und räumlich getrennt zu lagern, damit ein lokaler Schadensfall nicht den gesamten Bestand gefährdete. Jeder Sicherungslauf war zu überwachen; fehlgeschlagene Läufe waren am Folgetag zu wiederholen und im Betriebstagebuch zu vermerken. Die Einhaltung der Aufbewahrungsfristen wurde stichprobenartig durch das Team Infrastruktur kontrolliert und dokumentiert.
