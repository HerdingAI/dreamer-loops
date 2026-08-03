#!/usr/bin/env bash
# Install the three scheduled jobs (§6.9). Idempotent.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

read -r -d '' BLOCK <<EOF || true
# --- dreamer (managed by bin/install-cron.sh) ---
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.nvm/versions/node/v24.14.0/bin
# Night cycle — backfill then dream, every 3 hours on the hour 20:00-05:00
# (owner decision 2026-08-02). Both legs live in one wrapper because they share
# the vault lock; co-scheduled, the second would always lose and skip.
0 20,23,2,5 * * * flock -n /tmp/dreamer-night.lock $ROOT/bin/night-cycle.sh >> $ROOT/logs/night.log 2>&1

# Inbox extraction, ahead of the night window so it never contends with it.
0 19 * * *  flock -n /tmp/dreamer-nightly.lock $ROOT/bin/nightly-extract.sh >> $ROOT/logs/nightly.log 2>&1

# Decay stays weekly and must precede the dream (spec §6.9 orders Job 3 before
# Job 2). With dreams running all night, "before" now means before the window
# opens, not 02:45.
30 19 * * 0 flock -n /tmp/dreamer-decay.lock   $ROOT/bin/decay-archive.sh   >> $ROOT/logs/decay.log   2>&1

# Dashboard backstop, weekly. Jobs already regenerate it on every commit, so
# this is not the main refresh path — it only catches drift the jobs never see
# (live MCP resurfacings bumping recurrence on a week when nothing else ran).
# Sunday 06:00: after the night window closes at 05:00, so it reads a settled
# vault. For anything sooner, run bin/dashboard.sh -- it regenerates on demand.
# (No backticks in this heredoc: it is unquoted, so they would run as commands.)
0 6 * * 0 $ROOT/scripts/dashboard.py >> $ROOT/logs/dashboard.log 2>&1
# --- end dreamer ---
EOF

TMP=$(mktemp)
crontab -l 2>/dev/null | sed '/# --- dreamer (managed/,/# --- end dreamer ---/d' > "$TMP" || true
printf '%s\n' "$BLOCK" >> "$TMP"
crontab "$TMP"
rm -f "$TMP"
echo "installed:"
crontab -l | sed -n '/# --- dreamer/,/# --- end dreamer ---/p'
echo
echo "NOTE: cron needs explicit PATH for node/qmd — set above. Jobs are also"
echo "guarded by flock, so an overrun never overlaps its successor."
