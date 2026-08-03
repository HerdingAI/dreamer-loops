#!/usr/bin/env bash
# Manual dashboard refresh. The jobs rebuild it on every commit and cron backs
# that up weekly; this is the "I want to look right now" path.
#
#   bin/dashboard.sh            regenerate vault/dashboard.html, print the path
#   bin/dashboard.sh --open     regenerate, then open it in the browser
#   bin/dashboard.sh --serve    live view on localhost, regenerated per request
#
# Deliberately does NOT source _common.sh: no lock, no logging, no commit. The
# dashboard is a read-only projection, so a manual refresh must be safe to run
# at any moment — including while a job holds the vault lock mid-research.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DREAMER_PYTHON:-python3}"

case "${1:-}" in
  --serve)
    shift
    exec "$PY" "$ROOT/scripts/dashboard.py" --serve "$@"
    ;;
  --open)
    OUT="$("$PY" "$ROOT/scripts/dashboard.py")"
    echo "$OUT"
    # xdg-open detaches; failure to find a browser is not a failed refresh.
    xdg-open "$OUT" >/dev/null 2>&1 || echo "(no browser handler — open $OUT yourself)"
    ;;
  -h|--help)
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    "$PY" "$ROOT/scripts/dashboard.py" "$@"
    ;;
esac
