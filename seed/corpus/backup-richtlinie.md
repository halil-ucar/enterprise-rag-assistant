# Backup-Richtlinie

## Sicherungskonzept

Es gilt die 3-2-1-Regel: drei Kopien der Daten, auf zwei unterschiedlichen Medientypen,
davon eine Kopie außer Haus. Produktivsysteme werden täglich inkrementell und wöchentlich
voll gesichert.

## Wiederherstellung

Wiederherstellungsanfragen laufen über ein Ticket in der Kategorie "Backup/Restore".
Die Ziel-Wiederherstellungszeit (RTO) für Produktivsysteme beträgt vier Stunden,
der maximale Datenverlust (RPO) 24 Stunden.

## Aufbewahrung

Tägliche Sicherungen werden 14 Tage aufbewahrt, wöchentliche Sicherungen drei Monate,
Monatssicherungen ein Jahr.
