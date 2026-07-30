> **Dokument-Nr.** NF-DOC-VPN-HANDBUCH-V2 · **Version** 2.0 · **Stand** 2024-06-01 · **Verantwortlich** IT-Betrieb / IT-Sicherheit, Nordfels IT GmbH — **STATUS: ABGELÖST**

| Version | Datum | Änderung |
|---------|-------|----------|
| 1.0 | 2023-02-15 | Erstfassung VPN-Handbuch |
| 2.0 | 2024-06-01 | Umstellung auf Profil Nordfels-Standard |
| — | 2026-03-01 | Abgelöst durch Version 3 |

# VPN-Handbuch (v2, abgelöst)

## Geltung

Diese Fassung ist Abgelöst durch Version 3 (gültig ab 01.03.2026) und daher nicht mehr anwenden. Das Dokument bleibt allein zu Dokumentationszwecken erhalten; für den laufenden Betrieb an den Standorten Hagen und Köln ist ausschließlich die aktuelle Version 3 des VPN-Handbuchs heranzuziehen. Ältere Ausdrucke oder lokal gespeicherte Kopien dieser Fassung sind zu vernichten.

## Einrichtung

Installiere zuerst den VPN-Client aus dem Softwarecenter und wähle bei der ersten Anmeldung das Profil Nordfels-Standard aus. Starte den Client anschließend neu und melde dich mit deinem regulären Windows-Konto an, damit die Konfiguration übernommen wird. Prüfe nach dem ersten Verbindungsaufbau, ob der Standortname korrekt angezeigt wird, und wende dich bei Abweichungen an den IT-Service-Desk.

## Zertifikat

Für den Verbindungsaufbau wird ein Gerätezertifikat benötigt, das automatisch auf das Endgerät ausgerollt wird. Das Gerätezertifikat ist 12 Monate gültig und muss nach Ablauf dieser Laufzeit ersetzt werden. Läuft das Zertifikat aus, so erfolgt die Erneuerung im Self-Service-Portal, ohne dass ein Ticket eröffnet werden muss. Plane die Erneuerung möglichst einige Tage vor Ablauf ein, damit keine Unterbrechung des Zugangs entsteht.

## Fehlerbehebung

Tritt beim Verbindungsaufbau eine Störung auf, prüfe zunächst den angezeigten Fehlercode und gehe nach folgendem Schema vor. Bei VPN-201 ist das Gerätezertifikat abgelaufen; erneuere es in diesem Fall im Portal und starte den Client danach neu. Bei VPN-204 wurde der Tunnel getrennt; stelle die Verbindung erneut her und prüfe die Netzwerkanbindung des Endgeräts. Bleibt der Fehler bestehen, dokumentiere den Code und wende dich mit dieser Angabe an den IT-Service-Desk, der die weitere Analyse übernimmt.
