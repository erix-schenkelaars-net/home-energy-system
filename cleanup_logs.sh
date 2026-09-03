#!/bin/bash
# cleanup_logs.sh — pi5new / pi5
# Verwijdert debug_*.log files op basis van de DATUM IN DE BESTANDSNAAM (niet mtime).
#
# Sinds entrypoint.sh per KALENDERDAG roteert (debug_$(date +%F).log) draagt elk bestand
# zijn eigen dag in de naam. Op mtime opruimen faalde op het ACTIEVE bestand: dat werd elke
# paar seconden geschreven, dus -mtime +N matchte het nooit → één bestand groeide onbeperkt
# (read_seplos stond op 1,35 GB, 3 weken in één file). Op filename-datum opruimen is robuust:
# het actieve bestand van vandaag blijft, alles ouder dan MAX_DAYS gaat weg.
#
# Cron (root): 0 7 * * * /home/pi/docker/cleanup_logs.sh >> /home/pi/docker/logs/debug_cleanup_$(date +\%F).log 2>&1

DOCKER_ROOT=/home/pi/docker
MAX_DAYS=5
CUTOFF=$(date -d "${MAX_DAYS} days ago" +%Y-%m-%d)   # bestanden gedateerd VÓÓR deze dag gaan weg
TOTAL=0
KEPT=0

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting log cleanup (delete files dated before ${CUTOFF})"

# Alle logs/ subdirs: direct onder DOCKER_ROOT (diepte 1) én onder container-dirs (diepte 2).
while IFS= read -r f; do
    # Pak de laatste YYYY-MM-DD uit de bestandsnaam (werkt voor debug_, debug_collect_, debug_cleanup_).
    d=$(basename "$f" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | tail -1)
    if [ -z "$d" ]; then
        # Geen leesbare datum in de naam -> val terug op mtime.
        if [ -n "$(find "$f" -mtime +${MAX_DAYS} 2>/dev/null)" ]; then
            sudo rm -f "$f" && TOTAL=$((TOTAL + 1))
        fi
        continue
    fi
    # ISO-datums vergelijken lexicaal = chronologisch.
    if [ "$d" \< "$CUTOFF" ]; then
        sudo rm -f "$f" && TOTAL=$((TOTAL + 1))
    else
        KEPT=$((KEPT + 1))
    fi
done < <(find "${DOCKER_ROOT}" -mindepth 1 -maxdepth 2 -type d -name "logs" \
             -exec find {} -maxdepth 1 -name "debug_*.log" \;)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done. Deleted ${TOTAL}, kept ${KEPT}."
