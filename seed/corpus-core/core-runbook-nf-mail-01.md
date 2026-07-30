> **Dokument-Nr.** NF-DOC-RUNBOOK-NF-MAIL-01 · **Version** 1.4 · **Stand** 2025-12-01 · **Verantwortlich** IT-Betrieb Mail (Nordfels IT GmbH)

| Version | Datum | Änderung |
|---|---|---|
| 1.2 | 2025-09-14 | Neustart-Prozedur an neue Verwaltungskonsole angepasst |
| 1.3 | 2025-10-30 | Fehlercode-Tabelle überarbeitet |
| 1.4 | 2025-12-01 | Redaktionelle Korrekturen, Freigabe für Standorte Hagen und Köln |

# Runbook NF-MAIL-01 (Mailserver)

## System

Dieses Runbook beschreibt den Betrieb und die Störungsbehebung für den produktiven Mailserver »NF-MAIL-01«. Das System übernimmt den zentralen E-Mail-Ein- und -Ausgang für alle Postfächer der Nordfels IT GmbH an den Standorten Hagen und Köln. Es handelt sich um ein produktives System der Verfügbarkeitsklasse hoch; Wartungsarbeiten sind ausschließlich innerhalb der freigegebenen Wartungsfenster durchzuführen. Vor jedem Eingriff ist der aktuelle Betriebsstatus in der Verwaltungskonsole zu prüfen und der Bereitschaftsdienst zu informieren.

## Fehlercodes

Die folgende Tabelle listet die für den Mailserver definierten Fehlercodes samt Bedeutung und einzuleitender Maßnahme. Bei jeder Störung ist zunächst der exakte Code aus dem Ereignisprotokoll abzulesen und mit dieser Tabelle abzugleichen. Werden mehrere Codes gleichzeitig gemeldet, ist der Vorfall an die Fachgruppe Mail zu eskalieren.

| Code | Bedeutung | Maßnahme |
|---|---|---|
| M-101 | Postfach voll | Archivierung anstoßen |
| M-205 | SMTP-Authentifizierung fehlgeschlagen | Anmeldedaten prüfen |

Der Code »M-101« signalisiert ein volles Postfach; in diesem Fall ist umgehend die Archivierung anzustoßen, um den weiteren Nachrichtenempfang sicherzustellen. Der Code »M-205« weist auf eine fehlgeschlagene SMTP-Authentifizierung hin; hier sind die hinterlegten Anmeldedaten zu prüfen und bei Bedarf neu zu setzen. Jede durchgeführte Maßnahme ist im Betriebstagebuch zu dokumentieren.

## Neustart

Lässt sich eine Störung nicht durch die oben genannten Maßnahmen beheben, ist ein kontrollierter Neustart des Dienstes vorzunehmen. Dazu ist der Dienst maild über die Verwaltungskonsole neu zu starten; ein Neustart des gesamten Servers ist nur nach Rücksprache mit dem Bereitschaftsdienst zulässig. Nach dem Neustart ist die Funktionsfähigkeit durch eine Testnachricht zu prüfen und das Ergebnis im Betriebstagebuch festzuhalten.
