> **Dokument-Nr.** NF-DOC-RUNBOOK-NF-CRM · **Version** 2.0 · **Stand** 2025-11-15 · **Verantwortlich** IT-Betrieb / Applikationsbetreuung (Nordfels IT GmbH, Standort Hagen)

| Version | Datum | Bearbeitung | Änderung |
|---|---|---|---|
| 1.2 | 2025-06-12 | J. Feldmann | Fehlerkatalog überarbeitet |
| 1.3 | 2025-09-01 | A. Wisniewski | Sync-Prozeduren ergänzt |
| 2.0 | 2025-11-15 | J. Feldmann | Neustrukturierung, Freigabe der aktuellen Fassung |

# Runbook NF-CRM (Kundendatenbank)

## System

Dieses Runbook beschreibt den Betrieb und die Störungsbehebung für das CRM-System NF-CRM, das die Kundendatenbank der Nordfels IT GmbH an den Standorten Hagen und Köln abbildet. Es richtet sich an die Applikationsbetreuung und den diensthabenden IT-Betrieb und ist bei jeder Störung als erste Handlungsanweisung heranzuziehen. Prüfe zu Beginn den allgemeinen Systemzustand und die Verfügbarkeit der zentralen Dienste, bevor einzelne Fehler bearbeitet werden. Halte alle durchgeführten Schritte im Betriebstagebuch nachvollziehbar fest. Eskaliere an die zweite Ebene, sobald ein Fehler mit den hier beschriebenen Maßnahmen nicht innerhalb des vorgesehenen Zeitrahmens behoben werden kann. Verwende ausschließlich die freigegebenen Werkzeuge und ändere keine Konfiguration ohne dokumentierte Freigabe.

## Fehlercodes

Die folgende Übersicht führt die betriebsrelevanten Meldungen und die zugehörigen Sofortmaßnahmen auf. Tritt der Fehler CRM-30 auf, so ist die Synchronisation fehlgeschlagen und die Datensätze zwischen den beteiligten Systemen können auseinanderlaufen. Prüfe in diesem Fall die Verbindung zum Zielsystem, kontrolliere die Protokolle auf abgebrochene Übertragungen und starte den Sync-Job neu, damit der Abgleich vollständig durchläuft. Prüfe anschließend, ob der Job vollständig durchläuft, und dokumentiere die Störung samt Uhrzeit im Betriebstagebuch. Bleibt die Meldung bestehen oder tritt sie wiederholt auf, eskaliere unverzüglich an die Applikationsbetreuung und sichere die relevanten Protokolldateien für die Ursachenanalyse. Ändere keine Kundendaten manuell, solange der Abgleich nicht ordnungsgemäß abgeschlossen wurde. Informiere nach der Behebung die betroffenen Fachbereiche über die wiederhergestellte Verfügbarkeit und vermerke den Abschluss der Störung mit Uhrzeit. Führe im Anschluss eine kurze Nachkontrolle durch, um sicherzustellen, dass keine Folgefehler auftreten und die offenen Datensätze vollständig abgeglichen wurden. Übergib die dokumentierten Erkenntnisse an die reguläre Schichtübergabe, damit wiederkehrende Muster frühzeitig erkannt und dauerhaft abgestellt werden können.
