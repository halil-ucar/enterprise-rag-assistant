> **Dokument-Nr.** NF-DOC-FAQ-VPN · **Version** 2026.1 · **Stand** 2026-03-05 · **Verantwortlich** IT-Betrieb / Servicedesk Nordfels IT GmbH

| Version | Datum | Änderung |
|---------|------------|-----------------------------------------------|
| 2025.4  | 2025-11-18 | Erstfassung des VPN-FAQ für die Standorte Hagen und Köln |
| 2026.0  | 2026-01-22 | Fehlercode-Referenz VPN-201 ergänzt |
| 2026.1  | 2026-03-05 | Laufzeit der Zertifikate präzisiert, Runbook-Schritte gestrafft |

# FAQ VPN

Dieses FAQ richtet sich an alle Mitarbeitenden der Nordfels IT GmbH, die den gesicherten Fernzugriff über das Unternehmens-VPN nutzen. Die folgenden Fragen und Antworten sind als kurzes Runbook gedacht: Lesen Sie die betreffende Frage, führen Sie die genannten Schritte der Reihe nach aus und wenden Sie sich erst danach an den Servicedesk. Halten Sie bei jeder Rückfrage Ihren Standort (Hagen oder Köln) und Ihren Benutzernamen bereit.

## Häufige Fragen

**Frage: Wie lange gilt mein VPN-Zertifikat?**
Antwort: Das persönliche VPN-Zertifikat gilt ab dem Ausstellungsdatum genau 18 Monate. Prüfen Sie die verbleibende Restlaufzeit rechtzeitig im VPN-Client unter „Zertifikatsdetails". Erneuern Sie das Zertifikat vor Ablauf eigenständig im Selbstbedienungs-Portal: Melden Sie sich am Portal an, wählen Sie „VPN-Zertifikat erneuern" und folgen Sie den Anweisungen bis zur Bestätigung. Starten Sie anschließend den VPN-Client neu, damit das erneuerte Zertifikat geladen wird.

**Frage: Was bedeutet die Meldung VPN-201 und was ist zu tun?**
Antwort: Der Fehlercode VPN-201 zeigt an, dass Ihr Zertifikat abgelaufen ist und der Tunnel deshalb nicht aufgebaut werden kann. Öffnen Sie in diesem Fall das Selbstbedienungs-Portal und erneuern Sie das Zertifikat wie oben beschrieben. Verbinden Sie sich danach erneut mit dem VPN; die Meldung VPN-201 verschwindet, sobald ein gültiges Zertifikat vorliegt. Bleibt der Fehler bestehen, obwohl Sie erneuert haben, melden Sie sich beim Servicedesk und nennen Sie den genauen Zeitpunkt der letzten Erneuerung.

**Frage: Muss ich für die Erneuerung im Büro sein?**
Antwort: Nein, die Erneuerung ist über das Portal auch von zu Hause möglich, solange Sie sich einmalig authentifizieren können. Planen Sie die Erneuerung dennoch nicht auf den letzten Tag, damit ein abgelaufenes Zertifikat Ihren Arbeitsbeginn nicht verzögert. Notieren Sie sich den Ablauftermin gegebenenfalls in Ihrem Kalender.
