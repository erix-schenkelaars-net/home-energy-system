#!/bin/bash
# Docker container health monitor for pi5new.
# Alerts on state changes only (no spam when container stays down).
# Cron (sudo crontab -e on pi5new):
#   */15 * * * *  /home/pi/docker/check-containers.sh
#   0 7   * * *   /home/pi/docker/check-containers.sh --summary

MAILTO="debian-pi5new@schenkelaars.net"
STATE_FILE="/home/pi/docker/.container_health_state"
HOST=$(hostname -s)
NOW=$(date '+%Y-%m-%d %H:%M')
SUMMARY_MODE="${1:-}"

# ---------------------------------------------------------------------------
# Collect current container states
# ---------------------------------------------------------------------------
declare -A STATUS
while IFS='|' read -r name status; do
    [[ -n "$name" ]] && STATUS["$name"]="$status"
done < <(docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null)

if [[ ${#STATUS[@]} -eq 0 ]]; then
    echo -e "check-containers: docker ps returned nothing at ${NOW}" \
        | mail -s "[ALERT] Docker unreachable on ${HOST}" "$MAILTO"
    exit 1
fi

# Classify containers
DOWN_NAMES=()
UP_NAMES=()
for name in "${!STATUS[@]}"; do
    st="${STATUS[$name]}"
    if [[ "$st" =~ ^Up ]] && [[ ! "$st" =~ unhealthy ]]; then
        UP_NAMES+=("$name")
    else
        DOWN_NAMES+=("$name")
    fi
done

# ---------------------------------------------------------------------------
# Load previous down-list from state file
# ---------------------------------------------------------------------------
PREV_DOWN=()
if [[ -f "$STATE_FILE" ]] && [[ -s "$STATE_FILE" ]]; then
    mapfile -t PREV_DOWN < "$STATE_FILE"
fi

# ---------------------------------------------------------------------------
# Diff: newly down / recovered
# ---------------------------------------------------------------------------
NEWLY_DOWN=()
for name in "${DOWN_NAMES[@]}"; do
    already=false
    for prev in "${PREV_DOWN[@]}"; do
        [[ "$prev" == "$name" ]] && already=true && break
    done
    $already || NEWLY_DOWN+=("$name")
done

RECOVERED=()
for prev in "${PREV_DOWN[@]}"; do
    now_up=false
    for name in "${UP_NAMES[@]}"; do
        [[ "$name" == "$prev" ]] && now_up=true && break
    done
    $now_up && RECOVERED+=("$prev")
done

# Save current down-list as new state
printf '%s\n' "${DOWN_NAMES[@]}" > "$STATE_FILE"

# ---------------------------------------------------------------------------
# Build a sorted container overview for email bodies
# ---------------------------------------------------------------------------
overview() {
    for name in $(printf '%s\n' "${!STATUS[@]}" | sort); do
        printf '  %-35s %s\n' "$name" "${STATUS[$name]}"
    done
}

# ---------------------------------------------------------------------------
# Alert: newly failed containers
# ---------------------------------------------------------------------------
if [[ ${#NEWLY_DOWN[@]} -gt 0 ]]; then
    {
        echo "ALERT on ${HOST} — $(date)"
        echo ""
        echo "Containers that just went DOWN:"
        for name in "${NEWLY_DOWN[@]}"; do
            echo "  - ${name}: ${STATUS[$name]}"
        done

        for name in "${NEWLY_DOWN[@]}"; do
            echo ""
            echo "--- Last 40 log lines: ${name} ---"
            docker logs --tail 40 "$name" 2>&1 | sed 's/^/  /'
        done

        if [[ ${#DOWN_NAMES[@]} -gt ${#NEWLY_DOWN[@]} ]]; then
            echo ""
            echo "Also still DOWN (known):"
            for name in "${DOWN_NAMES[@]}"; do
                skip=false
                for n in "${NEWLY_DOWN[@]}"; do [[ "$n" == "$name" ]] && skip=true && break; done
                $skip || echo "  - ${name}: ${STATUS[$name]}"
            done
        fi

        echo ""
        echo "All containers:"
        overview
    } | mail -s "[ALERT] Container DOWN on ${HOST}" "$MAILTO"
fi

# ---------------------------------------------------------------------------
# Recovery: containers that came back up
# ---------------------------------------------------------------------------
if [[ ${#RECOVERED[@]} -gt 0 ]]; then
    {
        echo "RECOVERY on ${HOST} — $(date)"
        echo ""
        echo "Containers back UP:"
        for name in "${RECOVERED[@]}"; do
            echo "  - ${name}: ${STATUS[$name]}"
        done
        echo ""
        echo "All containers:"
        overview
    } | mail -s "[RECOVERY] Container UP on ${HOST}" "$MAILTO"
fi

# ---------------------------------------------------------------------------
# Daily summary (--summary flag)
# ---------------------------------------------------------------------------
if [[ "$SUMMARY_MODE" == "--summary" ]]; then
    if [[ ${#DOWN_NAMES[@]} -eq 0 ]]; then
        subj="[OK] All containers healthy on ${HOST}"
    else
        subj="[WARN] ${#DOWN_NAMES[@]} container(s) DOWN on ${HOST}"
    fi
    {
        echo "Daily container check — ${NOW}"
        echo ""
        if [[ ${#DOWN_NAMES[@]} -gt 0 ]]; then
            echo "DOWN:"
            for name in "${DOWN_NAMES[@]}"; do
                echo "  - ${name}: ${STATUS[$name]}"
            done
            echo ""
        fi
        echo "All containers:"
        overview
    } | mail -s "$subj" "$MAILTO"
fi
