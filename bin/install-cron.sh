#!/usr/bin/env bash
# Install the scheduled jobs (§6.9). Idempotent.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve node's bin dir the same way bin/_common.sh does: glob over installed
# nvm versions and take the first one that carries qmd. Never pin a version —
# a hardcoded path silently breaks the wisdom route on the next node upgrade.
# If nothing matches (node/qmd installed another way), the PATH line below is
# emitted without it: put the directory holding node/qmd on cron's PATH
# yourself by editing the installed block.
NODE_BIN=""
for _nodebin in "$HOME"/.nvm/versions/node/*/bin; do
  if [ -x "$_nodebin/qmd" ]; then NODE_BIN="$_nodebin"; break; fi
done
unset _nodebin

read -r -d '' BLOCK <<EOF || true
# --- dreamer (managed by bin/install-cron.sh) ---
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${NODE_BIN:+:$NODE_BIN}
# Night cycle — backfill then dream, every 3 hours on the hour 20:00-05:00
# (owner decision 2026-08-02). Both legs live in one wrapper because they share
# the vault lock; co-scheduled, the second would always lose and skip.
0 20,23,2,5 * * * flock -n /tmp/dreamer-night.lock $ROOT/bin/night-cycle.sh >> $ROOT/logs/night.log 2>&1

# claude.ai sync conversion: the Dreamer Sync Chrome extension leaves drops in
# to_ingest/; this converts them before the 19:00 extraction. It WAITS for the
# vault lock (claude_ai_sync.lock_wait_seconds) so it never rewrites a page
# under a running extraction. Fetching itself is the extension's job.
0 18 * * * flock -n /tmp/dreamer-sync-claude-ai.lock $ROOT/bin/sync-claude-ai.sh >> $ROOT/logs/sync-claude-ai.log 2>&1

# Claude Code session ingestion, ahead of extraction so anything it writes is
# in the queue the 19:00 run selects from. Takes no vault lock (append-only
# into vault/sources via atomic writes), but is scheduled clear of the others
# anyway so its summariser calls do not contend for the usage window.
30 18 * * * flock -n /tmp/dreamer-ingest-cc.lock $ROOT/bin/ingest-cc.sh >> $ROOT/logs/ingest-cc.log 2>&1

# Inbox extraction, ahead of the night window so it never contends with it,
# then the goal assessor. Chained on one line rather than given its own time:
# drift is measured in loops, so the assessor must see tonight's extraction,
# and a fixed offset would silently lose that order the night extraction runs
# long. The assessor runs whether or not extraction succeeded.
# The knowledge-frontier pass (taps, map, pre-mortems, lessons, release) is
# chained after the assessor the same way, with a semicolon: it reads what extraction and
# assessment just wrote, and it runs whether or not either of them succeeded. It
# holds its own frontier.lock for the run and takes wiki.lock only around each apply.
0 19 * * *  flock -n /tmp/dreamer-nightly.lock $ROOT/bin/nightly-extract.sh >> $ROOT/logs/nightly.log 2>&1; flock -n /tmp/dreamer-assess.lock $ROOT/bin/nightly-assess.sh >> $ROOT/logs/assess.log 2>&1; flock -n /tmp/dreamer-frontier.lock $ROOT/bin/frontier.sh >> $ROOT/logs/frontier.log 2>&1

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

# Health watchdog, hourly and independent of the night pipeline. It ONLY
# compares health.checked_at against health.checked_max_age_hours — no
# assertion registry, no model calls — and when stale it appends a degraded
# event AND writes to logs/watchdog.log, a channel outside the pipeline it
# watches. Offset to :12 so it never coincides with the on-the-hour jobs.
# The freshness publish is chained AFTER it, as a separate command, so the
# watchdog itself stays a pure staleness check. Hourly publishing is what lets
# a reader tell "laptop off" (snapshot old) from "axis stale" (snapshot fresh).
# The board publish (content, opt-in; a no-op unless enabled) runs between them
# so the health snapshot that follows reports it.
12 * * * * $ROOT/scripts/healthcheck.py --watchdog >> $ROOT/logs/watchdog.log 2>&1; $ROOT/scripts/board_publish.py >> $ROOT/logs/board-publish.log 2>&1; $ROOT/scripts/freshness_publish.py >> $ROOT/logs/freshness-publish.log 2>&1; $ROOT/scripts/frontier_publish.py >> $ROOT/logs/frontier-publish.log 2>&1
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
if [ -n "$NODE_BIN" ]; then
  echo "NOTE: cron needs explicit PATH for node/qmd — resolved to $NODE_BIN."
else
  echo "NOTE: no nvm node install with qmd was found — edit the PATH line in"
  echo "      the installed block to include the directory holding node/qmd."
fi
echo "Jobs are guarded by flock, so an overrun never overlaps its successor."
