#!/usr/bin/env bash
# frontier-backfill — maps the existing corpus into the knowledge-frontier map
# Run ONCE after install, between night jobs, then it is a no-op:
# the nightly bin/frontier.sh maps new input incrementally.
#
#   bin/frontier-backfill.sh [batches]
#
# Loops over batches of frontier.map_batch_chars of owner text until the corpus
# is drained, or until the optional [batches] count is reached. Resumable: the
# ledger (vault/.vault-meta/frontier-map-ledger.json) is marked per transcript
# once its concepts apply, so a rerun continues where this one stopped.
#
# Locking: holds frontier.lock for the whole run, and wiki.lock only
# around each apply (inside frontier_map.py) and each commit.
#
# Each batch starts by refreshing the qmd index and embedding the `frontier`
# collection so it can match everything earlier batches wrote (a failed refresh
# stops the run), and ends with a git commit. A
# usage-limit exit (rule 9) or a busy/degraded dependency stops the run cleanly
# with that batch unmarked; the rest resumes next run.
JOB=frontier-backfill
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
arm_heartbeat

MAX_BATCHES="${1:-0}"
[[ "$MAX_BATCHES" =~ ^[0-9]+$ ]] || die "usage: frontier-backfill.sh [batches]"

acquire_frontier_lock
log "start (batches: $([[ "$MAX_BATCHES" -gt 0 ]] && echo "$MAX_BATCHES" || echo "until drained"))"

# Unresolved loops are the known unknowns. Idempotent, so every run may do it.
SEED=$("$PY" "$ROOT/scripts/frontier_map.py" seed-loops 2>>"$LOGS/$JOB.log")
RC=$?
if [[ $RC -eq 2 ]]; then
  log "deferred — wiki.lock stayed busy; nothing was seeded or mapped, run again later"
  exit 0
elif [[ $RC -ne 0 ]]; then
  die "seed-loops failed (see $LOGS/$JOB.log)"
fi
log "seed-loops: $SEED"

json_field() { "$PY" -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"; }

N=0
while :; do
  if [[ "$MAX_BATCHES" -gt 0 && "$N" -ge "$MAX_BATCHES" ]]; then
    log "batch limit ($MAX_BATCHES) reached — run again to continue"
    break
  fi
  # Matching against a stale index would miss what earlier batches wrote and
  # duplicate those items, so a failed refresh stops the run (it resumes next run).
  if ! reindex_collection frontier; then
    log "deferred — the qmd index could not be refreshed; batch $((N + 1)) NOT run"
    commit_locked "frontier-backfill $(TODAY): deferred after $N batch(es)" || true
    break
  fi

  OUT=$("$PY" "$ROOT/scripts/frontier_map.py" run-batch 2>>"$LOGS/$JOB.log")
  RC=$?
  if [[ $RC -ne 0 ]]; then
    commit_locked "frontier-backfill $(TODAY): batch $((N + 1)) failed (rc $RC)" || true
    die "run-batch exited $RC (see $LOGS/$JOB.log)"
  fi
  STATUS=$(printf '%s' "$OUT" | json_field status)
  log "batch $((N + 1)): $OUT"

  case "$STATUS" in
    done)
      N=$((N + 1))
      commit_locked "frontier-backfill $(TODAY): batch $N $(printf '%s' "$OUT" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("mapped=%s created=%s attached=%s dropped=%s deferred=%s" % (
    d["mapped"], d["items_created"], d["items_attached"], d["dropped_quotes"], d["deferred"]))')" \
        || log "WARN commit skipped; the next batch carries it"
      ;;
    drained)
      log "corpus drained — every transcript is mapped or skipped"
      commit_locked "frontier-backfill $(TODAY): drained" || true
      break
      ;;
    stopped)
      log "deferred — usage limit; the batch is NOT marked and resumes next run"
      commit_locked "frontier-backfill $(TODAY): deferred after $N batch(es)" || true
      break
      ;;
    deferred)
      log "deferred — $(printf '%s' "$OUT" | json_field reason); the batch is NOT marked and resumes next run"
      commit_locked "frontier-backfill $(TODAY): deferred after $N batch(es)" || true
      break
      ;;
    *)
      die "run-batch returned an unknown status: $OUT"
      ;;
  esac
done
log "done: $N batch(es)"
exit 0
