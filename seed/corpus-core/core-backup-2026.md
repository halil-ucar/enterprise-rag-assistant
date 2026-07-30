> **Dokument-Nr.** NF-DOC-BACKUP-2026 · **Version** 2026.1 · **Stand** 2026-02-01 · **Verantwortlich** IT-Betrieb / Team Infrastruktur (Nordfels IT GmbH, Standort Hagen)

| Version | Datum | Bearbeitung | Änderung |
|---|---|---|---|
| 2025.3 | 2025-11-20 | R. Kahle | Entwurf für die Neufassung erstellt |
| 2026.0 | 2026-01-10 | T. Ostermann | Interne Review Infrastruktur und Datenschutz |
| 2026.1 | 2026-02-01 | R. Kahle | Freigabe, löst das Konzept von 2023 ab |

# Backup-Konzept 2026

## Geltung

Dieses Backup-Konzept ist konzernweit für alle produktiven Systeme der Nordfels IT GmbH an den Standorten Hagen und Köln verbindlich. Es ist gültig ab 01.02.2026 und ersetzt das Konzept von 2023 vollständig; ab diesem Stichtag gelten ausschließlich die hier festgelegten Vorgaben. Alle abweichenden Regelungen aus früheren Fassungen sind mit dem Inkrafttreten dieses Dokuments gegenstandslos und dürfen nicht länger als Referenz herangezogen werden. Verantwortlich für die Umsetzung ist das Team Infrastruktur; fachliche Rückfragen sind an den IT-Betrieb zu richten. Abweichungen im Einzelfall bedürfen einer schriftlichen Genehmigung durch die IT-Leitung und sind zu dokumentieren. Die Wirksamkeit des Konzepts wird jährlich überprüft und bei Bedarf angepasst.

## Kennzahlen

Für die produktiven Systeme gilt ein maximaler Datenverlust (RPO) von 4 Stunden. Das bedeutet, dass im Fehlerfall höchstens die in diesem Zeitraum angefallenen Datenänderungen verloren gehen dürfen; die Sicherungsintervalle sind entsprechend eng zu takten. Ergänzend ist eine Wiederherstellzeit (RTO) von 2 Stunden vorgesehen, innerhalb derer ein betroffenes System nach einer Störung wieder produktiv verfügbar sein muss. Diese Kennzahlen sind bei der Planung von Wartungsfenstern und Kapazitäten zwingend zu berücksichtigen. Zur Absicherung der Zielwerte sind Wiederherstellungstests regelmäßig durchzuführen und die gemessenen Zeiten zu protokollieren. Werden die Kennzahlen in einem Test verfehlt, ist unverzüglich eine Ursachenanalyse einzuleiten und eine Korrekturmaßnahme festzulegen. Die Verantwortung für die Einhaltung liegt beim IT-Betrieb, der die Ergebnisse quartalsweise an die IT-Leitung berichtet.

## Aufbewahrung

Die täglichen Sicherungen (Tagessicherungen) werden 30 Tage vorgehalten und stehen in diesem Zeitraum für eine punktgenaue Wiederherstellung zur Verfügung. Nach Ablauf der Frist werden die entsprechenden Sicherungssätze automatisiert und nachvollziehbar gelöscht, sofern keine gesetzliche oder vertragliche Aufbewahrungspflicht entgegensteht. Wochensicherungen und Monatssicherungen werden nach dem etablierten Generationenprinzip zusätzlich vorgehalten, um auch länger zurückliegende Zustände rekonstruieren zu können. Die Sicherungsmedien sind an beiden Standorten redundant und räumlich getrennt zu lagern, damit ein lokaler Schadensfall nicht den gesamten Bestand gefährdet. Jeder Sicherungslauf ist zu überwachen; fehlgeschlagene Läufe sind am Folgetag zu wiederholen und im Betriebstagebuch zu vermerken. Die Einhaltung der Aufbewahrungsfristen wird stichprobenartig durch das Team Infrastruktur kontrolliert und dokumentiert.
