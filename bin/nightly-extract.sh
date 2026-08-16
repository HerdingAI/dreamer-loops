#!/usr/bin/env bash
# Job 1 — nightly-extract (§6.9). Nightly 02:00.
JOB=nightly-extract
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

# Health check BEFORE acquire_lock: healthcheck takes the same wiki.lock for
# its run-state write, and flock is per open-file-description, so a child
# process can never re-acquire the lock this shell already holds. Non-zero is
# degraded, never fatal.
run_healthcheck

acquire_lock
log "start"

# 1. Ingest anything sitting in the inbox (export ZIPs, loose transcripts).
shopt -s nullglob
for zip in "$ROOT"/vault/inbox/*.zip "$ROOT"/vault/inbox/*.json; do
  log "converting $(basename "$zip")"
  "$PY" "$ROOT/scripts/convert_claude_export.py" "$zip" || log "converter failed on $zip"
  mv "$zip" "$ROOT/vault/sources/" 2>/dev/null || rm -f "$zip"
done
shopt -u nullglob

# 2. Select the batch. Empty batch => near-zero-cost no-op.
BATCH_JSON=$("$PY" "$ROOT/scripts/make_batch.py" ${BATCH_SIZE:+--limit $BATCH_SIZE} \
             --out "$ROOT/logs/.prompt-$JOB.md")
COUNT=$(printf '%s' "$BATCH_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("count",0))')

if [[ "$COUNT" -eq 0 ]]; then
  log "inbox empty and no unextracted transcripts — no-op"
  # Still drain any queued resurfacings: they are work even when no transcript is.
  echo '{"candidates":[]}' | "$PY" "$ROOT/scripts/apply_extraction.py" >/dev/null || true
  # Folds left queued by a capped or deferred night are still pending NEW
  # input (rule 14) — drain them even on a transcript-free night. Loops with
  # nothing queued show a zero diff, which is what rule 2 demands.
  drain_fold_pending
  # Reindex even when nothing changed: it is near-free on an unchanged corpus
  # and it refreshes last_reindex, which index-fresh's rescue window reads.
  # Without this, a quiet stretch lets the rescue record age out, the static
  # wisdom collection (whose qmd age tracks content change, not last scan)
  # looks stale, research gets blocked — and the block sustains itself.
  reindex || log "WARN reindex failed on no-op night (event recorded)"
  regen_catalog; commit "nightly: no-op $(TODAY)"; exit 0
fi

log "batch of $COUNT transcript(s)"

# 3. Extraction + matching (the LLM half).
MAXT=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget']['max_turns_nightly'])")
if ! run_claude "$ROOT/logs/.prompt-$JOB.md" "$MAXT" "$ROOT/logs/.result-$JOB.json"; then
  log "deferred — batch NOT marked extracted, will retry next run"
  commit "nightly: deferred $(TODAY)"
  exit 0
fi

# 4. Apply (the deterministic half).
"$PY" "$ROOT/scripts/apply_extraction.py" --input "$ROOT/logs/.result-$JOB.json" \
    > "$ROOT/logs/.applied-$JOB.json" || die "apply failed"

# 5. Only now mark the batch consumed — a failed apply must be retryable.
"$PY" "$ROOT/scripts/make_batch.py" --limit "$COUNT" --mark-batch >/dev/null

# 6. Fold tonight's matched occurrences into their loops' living threads
# (CLAUDE.md rule 15). Restricted LLM calls; a deferral or the fold_per_run
# cap leaves the remainder queued for tomorrow.
drain_fold_pending

reindex
regen_catalog
commit "nightly-extract $(TODAY): $(jq -r '"created=\(.created) matched=\(.matched)"' "$ROOT/logs/.applied-$JOB.json" 2>/dev/null || echo done)"
log "done"
