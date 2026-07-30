> **Dokument-Nr.** NF-DOC-VPN-HANDBUCH-V3 · **Version** 3.0 · **Stand** 2026-03-01 · **Verantwortlich** IT-Betrieb / IT-Sicherheit, Nordfels IT GmbH

| Version | Datum | Änderung |
|---------|-------|----------|
| 1.0 | 2023-02-15 | Erstfassung VPN-Handbuch |
| 2.0 | 2024-06-01 | Umstellung auf Profil Nordfels-Standard |
| 3.0 | 2026-03-01 | Zertifikatslaufzeit verlängert, aktuelle Fassung |

# VPN-Handbuch (v3)

## Geltung

Diese Fassung des VPN-Handbuchs ist Gültig ab 01.03.2026 und ersetzt Version 2 vollständig. Dies ist die aktuell gültige Fassung für alle Mitarbeitenden der Nordfels IT GmbH an den Standorten Hagen und Köln; ältere Ausdrucke oder lokal gespeicherte Kopien sind zu vernichten. Maßgeblich ist ausschließlich das im Intranet veröffentlichte Original, das bei jeder Änderung neu freigegeben wird.

## Einrichtung

Installiere zuerst den VPN-Client aus dem Softwarecenter und wähle bei der ersten Anmeldung das Profil Nordfels-Standard aus. Starte den Client anschließend neu und melde dich mit deinem regulären Windows-Konto an, damit die Konfiguration übernommen wird. Prüfe nach dem ersten Verbindungsaufbau, ob der Standortname korrekt angezeigt wird, und wende dich bei Abweichungen an den IT-Service-Desk.

## Zertifikat

Für den Verbindungsaufbau wird ein Gerätezertifikat benötigt, das automatisch auf das Endgerät ausgerollt wird. Das Gerätezertifikat ist 18 Monate gültig; diese Laufzeit wurde mit v3 von zuvor zwölf Monaten heraufgesetzt, um die Zahl der Erneuerungsvorgänge zu senken. Läuft das Zertifikat aus, so erfolgt die Erneuerung im Self-Service-Portal, ohne dass ein Ticket eröffnet werden muss. Plane die Erneuerung möglichst einige Tage vor Ablauf ein, damit keine Unterbrechung des Zugangs entsteht.

## Fehlerbehebung

Tritt beim Verbindungsaufbau eine Störung auf, prüfe zunächst den angezeigten Fehlercode und gehe nach folgendem Schema vor. Bei VPN-201 ist das Gerätezertifikat abgelaufen; erneuere es in diesem Fall im Portal und starte den Client danach neu. Bei VPN-204 wurde der Tunnel getrennt; stelle die Verbindung erneut her und prüfe die Netzwerkanbindung des Endgeräts. Bleibt der Fehler bestehen, dokumentiere den Code und wende dich mit dieser Angabe an den IT-Service-Desk, der die weitere Analyse übernimmt.
