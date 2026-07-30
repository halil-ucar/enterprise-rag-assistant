# VPN-Handbuch

## Einrichtung

Den Nordfels-VPN-Client aus dem Softwarecenter installieren und mit dem Firmenkonto anmelden.
Nach der ersten Anmeldung wird automatisch ein Gerätezertifikat ausgestellt. Die Verbindung
erfolgt über das Profil "Nordfels-Standard".

## Fehlerbehebung

Bei Verbindungsproblemen zuerst den Client neu starten und die Netzwerkverbindung prüfen.

| Fehlercode | Bedeutung | Lösung |
|------------|-----------|--------|
| NF-4102 | Gerätezertifikat abgelaufen | Zertifikat im Self-Service-Portal erneuern, danach Client neu starten |
| NF-4200 | Tunnel unerwartet getrennt | Neu verbinden; bei Wiederholung WLAN wechseln |
| NF-4315 | Anmeldung abgelehnt | Passwort prüfen; nach drei Fehlversuchen 15 Minuten Sperre |
| NF-5001 | Kein Serverkontakt | Status-Seite prüfen; ggf. Ticket an den IT-Support |

Der Fehler NF-4102 ist der häufigste Fall: Das Gerätezertifikat läuft nach 12 Monaten ab
und muss im Self-Service-Portal erneuert werden.

## Support

Bei ungelösten Problemen ein Ticket in der Kategorie "Netzwerk/VPN" eröffnen.
