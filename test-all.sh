#!/bin/bash
# test-all.sh — run pub test suites for all services that have one
# Output goes to stdout AND to ~/docker/test-logs/<datetime>_<service>.log
# Usage:
#   ./test-all.sh                              # test all
#   ./test-all.sh battery_optimizer read_p1   # test specific ones

set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$DIR/test-logs"
mkdir -p "$LOGDIR"

PASS=0
FAIL=0
SKIP=0
TIMESTAMP="$(date +%Y-%m-%d_%H-%M-%S)"

# Auto-discover all subdirectories that have a test_*pub*.py
mapfile -t ALL < <(
  find "$DIR" -mindepth 1 -maxdepth 1 -type d \
    -exec sh -c 'ls "$1"/test_*pub*.py 2>/dev/null | grep -q .' _ {} \; -print \
  | xargs -I{} basename {} \
  | sort
)

if [[ ${#ALL[@]} -eq 0 ]]; then
  echo "No directories with test_*pub*.py found under $DIR"
  exit 1
fi

TARGETS=("${@:-${ALL[@]}}")

for svc in "${TARGETS[@]}"; do
  svcdir="$DIR/$svc"

  # Find test file(s) matching test_*pub*.py
  mapfile -t TESTS < <(find "$svcdir" -maxdepth 1 -name 'test_*pub*.py' 2>/dev/null | sort)

  if [[ ${#TESTS[@]} -eq 0 ]]; then
    echo ""
    echo "══════════════════════════════════════════"
    echo "  SKIP  $svc  (no test_*pub*.py)"
    echo "══════════════════════════════════════════"
    (( SKIP++ )) || true
    continue
  fi

  LOGFILE="$LOGDIR/${TIMESTAMP}_${svc}.log"

  echo ""
  echo "══════════════════════════════════════════"
  echo "  TEST  $svc  →  $(basename "$LOGFILE")"
  echo "══════════════════════════════════════════"

  if python -m pytest "${TESTS[@]}" -v --tb=short 2>&1 | tee "$LOGFILE"; then
    echo "  ✓  $svc PASSED"
    (( PASS++ )) || true
  else
    echo "  ✗  $svc FAILED"
    (( FAIL++ )) || true
  fi
done

echo ""
echo "══════════════════════════════════════════"
printf "  Results: %d passed  %d failed  %d skipped\n" "$PASS" "$FAIL" "$SKIP"
echo "  Logs:    $LOGDIR/"
echo "══════════════════════════════════════════"

[[ $FAIL -eq 0 ]]
