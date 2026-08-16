#!/usr/bin/env bash
# thread-backfill — ONE-TIME operational script. NOT installed in cron.
#
# Backfills living threads (CLAUDE.md rule 15) for loops minted before the
# thread pipeline existed. Selection: every non-archived loop WITHOUT a
# Thread section that holds >=1 transcript occurrence. Each selected loop's
# transcript occurrences are enqueued oldest-to-newest into fold-pending.json
# (scripts/fold_pending.py backfill-enqueue, duplicate-safe), then the normal
# restricted drain runs with a backfill-specific cap: thread.backfill_per_run
# fold PASSES per run (~326 passes at current vault size, so the drain takes
# several runs).
#
# Resumable and idempotent by construction: a loop that has grown a thread is
# never re-selected, enqueueing an already-queued occurrence is a no-op, and
# a partially folded loop's remaining occurrences stay queued and resume
# through the drain. When zero threadless loops remain and the queue is
# empty, this script — and config `thread.backfill_per_run` — are safe to
# delete.
#
# Rule-15 carve-out: this one-time initialization may touch paused and
# decision-only pages; rule 2 binds every night thereafter.
JOB=thread-backfill
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
acquire_lock
log "start"

LIMIT=$("$PY" -c "import yaml;print((yaml.safe_load(open('$ROOT/config.yaml')).get('thread') or {}).get('backfill_per_run',60))")

qlen() {
  "$PY" -c "import json
try: print(len(json.load(open('$META/fold-pending.json'))))
except FileNotFoundError: print(0)"
}

# 1. Select + enqueue (deterministic half). Prints {"loops": N, "enqueued": M}.
ENQ=$("$PY" "$ROOT/scripts/fold_pending.py" backfill-enqueue) \
  || die "backfill selection failed"
log "selection: $(printf '%s' "$ENQ" | tr -d '\n')"

Q_BEFORE=$(qlen)
if [[ "$Q_BEFORE" -eq 0 ]]; then
  log "drain complete — no threadless loop and an empty queue; this script and config thread.backfill_per_run are safe to delete"
  record_event "info" "thread-backfill: 0 folds applied, 0 loops remaining threadless — drain complete"
  exit 0
fi

# 2. Bounded restricted drain (bin/_common.sh). The queue is FIFO and
# fold_pending.batch preserves its order, so each loop's history folds
# oldest-first; entries over the cap stay queued for the next run.
drain_fold_pending "$LIMIT"

Q_AFTER=$(qlen)
# Entries cleared this run = successful folds plus any dropped junk entries
# (the drain logs both individually); the delta is the honest per-run count.
FOLDS=$((Q_BEFORE - Q_AFTER))

REMAIN=$("$PY" -c "
import sys; sys.path.insert(0, '$ROOT/scripts')
import fold_pending as FP
print(len(FP.threadless_loops()))")

record_event "info" "thread-backfill: $FOLDS folds applied, $REMAIN loops remaining threadless"
regen_catalog
commit "thread-backfill $(TODAY): $FOLDS fold(s) applied, $REMAIN loop(s) still threadless"

if [[ "${REMAIN:-1}" -eq 0 && "$(qlen)" -eq 0 ]]; then
  log "drain complete — this script and config thread.backfill_per_run are safe to delete"
else
  log "done: $FOLDS folds applied, $REMAIN threadless remaining — run again to continue the drain"
fi
exit 0
