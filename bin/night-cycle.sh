#!/usr/bin/env bash
# Night cycle — runs every 3 hours on the hour, 20:00 → 05:00 local
# (owner decision 2026-08-02). One wrapper, two legs, in sequence.
#
# Why a wrapper instead of two cron lines at the same minute: both legs take
# the same advisory vault lock, so co-scheduling them means whichever starts
# second exits with a logged skip. Backfill runs long, so the dream leg would
# have starved permanently while the crontab looked correct.
#
# Why backfill is bounded here: an unbounded backfill would hold the whole
# three-hour slot and the dream leg would never get a turn. Bounded, the queue
# still drains fast (BATCHES x 15 transcripts per cycle, four cycles a night)
# and research runs every cycle alongside it.
#
# Each leg is resumable and idempotent on its own, so a cycle killed midway
# costs at most the batch in flight — the next cycle picks up from the ledger.
#
# Manual run: ./bin/night-cycle.sh [backfill-batches]
JOB=night-cycle
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

BATCHES="${1:-${BACKFILL_BATCHES:-8}}"

log "=== night cycle start (backfill up to $BATCHES batch(es), then dream) ==="

# Leg 1 — backfill. Deliberately not fatal: a backfill that defers on the
# usage window is the normal, designed outcome, and the dream leg should still
# get its turn on whatever window is left.
REMAIN=$("$PY" "$ROOT/scripts/make_batch.py" --count-remaining \
  | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["remaining"])' 2>/dev/null || echo "?")
if [[ "$REMAIN" == "0" ]]; then
  log "backfill: queue already drained — skipping leg 1"
else
  log "backfill: $REMAIN transcript(s) queued"
  "$ROOT/bin/backfill.sh" "$BATCHES" || log "backfill leg exited non-zero — continuing to dream leg"
fi

# Leg 2 — dream. Its own guards decide whether there is anything worth
# researching: nothing at or above recurrence_min means a cheap quiet-week
# digest and exit, so running it every cycle costs little when the queue of
# eligible loops is empty.
log "dream: starting"
"$ROOT/bin/weekly-dream.sh" || log "dream leg exited non-zero"

log "=== night cycle done ==="
