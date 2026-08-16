#!/usr/bin/env bash
# Phase 0.5a/0.5b backfill (§6.8). Repeated invocations of the extraction skill
# over batches, resuming from the extracted-ledger checkpoint.
JOB=backfill
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
acquire_lock

BATCHES="${1:-999}"
log "start (up to $BATCHES batch(es))"

for ((i=1; i<=BATCHES; i++)); do
  REMAIN=$("$PY" "$ROOT/scripts/make_batch.py" --count-remaining | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["remaining"])')
  if [[ "$REMAIN" -eq 0 ]]; then log "checkpoint reads complete — backfill done"; break; fi
  log "batch $i/$BATCHES — $REMAIN transcript(s) remaining"

  BATCH_JSON=$("$PY" "$ROOT/scripts/make_batch.py" ${BATCH_SIZE:+--limit $BATCH_SIZE} \
               --out "$ROOT/logs/.prompt-$JOB.md")
  COUNT=$(printf '%s' "$BATCH_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("count",0))')
  [[ "$COUNT" -eq 0 ]] && break

  MAXT=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget']['max_turns_backfill'])")
  if ! run_claude "$ROOT/logs/.prompt-$JOB.md" "$MAXT" "$ROOT/logs/.result-$JOB.json"; then
    log "usage limit reached — stopping cleanly; next invocation resumes here"
    commit "backfill: deferred at batch $i"
    exit 0
  fi

  if ! "$PY" "$ROOT/scripts/apply_extraction.py" --input "$ROOT/logs/.result-$JOB.json" \
        --skip-resurfacings > "$ROOT/logs/.applied-$JOB.json"; then
    # DoD 6.8: "Batch runs never abort the whole backfill on a single bad
    # transcript (logged skip, processing continues)." This used to `exit 1`,
    # which is worse than it looks: the batch is only marked on success, so the
    # next run re-selects the same transcripts and can fail on them forever —
    # a permanent stall costing a full batch's spend on every retry. Observed
    # live on batch 10 (2026-08-02) after the model emitted unclosed JSON.
    #
    # Quarantine by name rather than dropping silently: marking the batch
    # extracted is what lets the run advance, so without the record these
    # transcripts would vanish from the queue with nothing pointing at them.
    log "apply failed on batch $i — quarantining $COUNT transcript(s) and continuing"
    printf '%s' "$BATCH_JSON" | "$PY" -c '
import json, sys, datetime, pathlib
sys.path.insert(0, "'"$ROOT"'/scripts")
from dreamer_common import p, read_json, atomic_write_json
batch = json.load(sys.stdin).get("batch", [])
path = p("meta") / "quarantine.json"
data = read_json(path, default={"batches": []}) or {"batches": []}
data["batches"].append({
    "at": datetime.datetime.now().isoformat(timespec="seconds"),
    "job": "backfill",
    "reason": "apply_extraction could not parse the model output",
    "transcripts": batch,
})
atomic_write_json(path, data)
print(f"quarantined {len(batch)} transcript(s) -> {path}")' || log "WARN could not write quarantine record"
    record_event "quarantine" "$COUNT transcript(s) in backfill batch $i produced unparseable extraction output and were skipped; see vault/.vault-meta/quarantine.json to re-run them"
    "$PY" "$ROOT/scripts/make_batch.py" --limit "$COUNT" --mark-batch >/dev/null
    commit "backfill batch $i: quarantined (unparseable extraction)"
    continue
  fi
  "$PY" "$ROOT/scripts/make_batch.py" --limit "$COUNT" --mark-batch >/dev/null

  regen_catalog
  # Per-batch commit: this boundary is what makes the calibration-gate rewind
  # remedy possible (§6.8). Without it, contaminated counts cannot be undone.
  commit "backfill batch $i: $(jq -r '"created=\(.created) matched=\(.matched)"' "$ROOT/logs/.applied-$JOB.json" 2>/dev/null || echo done)"
done

# Fold the backfill's matched occurrences into their loops' living threads
# (CLAUDE.md rule 15). After the batch loop, not inside it: the queue is
# durable, so a deferral mid-backfill simply leaves the folds for the next
# invocation (or the nightly job) to drain.
drain_fold_pending
regen_catalog
commit "backfill $(TODAY): thread folds applied"

reindex
log "done"
