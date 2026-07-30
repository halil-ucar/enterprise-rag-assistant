> **Dokument-Nr.** NF-DOC-RUNBOOK-NF-MAIL-ARCHIV · **Version** 1.4 · **Stand** 2025-12-01 · **Verantwortlich** IT-Betrieb Archiv (Nordfels IT GmbH)

| Version | Datum | Änderung |
|---|---|---|
| 1.2 | 2025-09-14 | Neustart-Prozedur an neue Verwaltungskonsole angepasst |
| 1.3 | 2025-10-30 | Fehlercode-Tabelle überarbeitet |
| 1.4 | 2025-12-01 | Redaktionelle Korrekturen, Freigabe für Standorte Hagen und Köln |

# Runbook NF-MAIL-ARCHIV (Archivsystem)

## System

Dieses Runbook beschreibt den Betrieb und die Störungsbehebung für das Langzeit-Archivsystem »NF-MAIL-ARCHIV«. Das System übernimmt die revisionssichere Langzeitablage und Wiederauffindbarkeit archivierter E-Mails für alle Postfächer der Nordfels IT GmbH an den Standorten Hagen und Köln. Es handelt sich um ein produktives System der Verfügbarkeitsklasse hoch; Wartungsarbeiten sind ausschließlich innerhalb der freigegebenen Wartungsfenster durchzuführen. Vor jedem Eingriff ist der aktuelle Betriebsstatus in der Verwaltungskonsole zu prüfen und der Bereitschaftsdienst zu informieren.

## Fehlercodes

Die folgende Tabelle listet die für das Archivsystem definierten Fehlercodes samt Bedeutung und einzuleitender Maßnahme. Bei jeder Störung ist zunächst der exakte Code aus dem Ereignisprotokoll abzulesen und mit dieser Tabelle abzugleichen. Werden mehrere Codes gleichzeitig gemeldet, ist der Vorfall an die Fachgruppe Archiv zu eskalieren.

| Code | Bedeutung | Maßnahme |
|---|---|---|
| A-101 | Archivlauf fehlgeschlagen | Lauf erneut planen |
| A-205 | Suchindex beschädigt | Index neu aufbauen |

Der Code »A-101« signalisiert einen fehlgeschlagenen Archivlauf; in diesem Fall ist der Lauf erneut zu planen, damit keine Nachrichten aus der Langzeitablage fehlen. Der Code »A-205« weist auf einen beschädigten Suchindex hin; hier ist der Index neu aufzubauen, um die Wiederauffindbarkeit sicherzustellen. Jede durchgeführte Maßnahme ist im Betriebstagebuch zu dokumentieren.

## Neustart

Lässt sich eine Störung nicht durch die oben genannten Maßnahmen beheben, ist ein kontrollierter Neustart des Dienstes vorzunehmen. Dazu ist der Dienst archived über die Verwaltungskonsole neu zu starten; ein Neustart des gesamten Servers ist nur nach Rücksprache mit dem Bereitschaftsdienst zulässig. Nach dem Neustart ist die Funktionsfähigkeit durch einen Testabruf zu prüfen und das Ergebnis im Betriebstagebuch festzuhalten.
