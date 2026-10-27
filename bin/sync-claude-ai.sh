#!/usr/bin/env bash
# Job: sync-claude-ai (daily 18:00). Converts the claude.ai sync drops that the
# Dreamer Sync Chrome extension left in to_ingest/ (claude-ai-sync-*.json) into
# transcripts, and records the count in the sync state file.
#
# Fetching is NOT done here. The extension runs inside the owner's everyday
# Chrome, through the native host (scripts/claude_ai_host.py), which writes the
# drops and the state file (docs/claude-ai-sync.md, docs/claude-ai-sync-state.md).
#
# Exit status. Non-zero ONLY when this job's own work fails: it could not get
# the vault lock in time, or a conversion failed. It is deliberately 0 when
# there is nothing to convert, and 0 when the last sync failed with a login
# problem or a block: that is the state file's job to say, and the
# `source.claude-ai` freshness axis reads it. If this job also failed on a
# login problem, one cause would raise two alarms and bury the useful one.
# (A job that never runs at all still goes stale on `job.sync-claude-ai`.)
#
# Why the vault lock. The converter rewrites the page of a chat that grew. If an
# extraction (nightly-extract, backfill, night-cycle) is mid-flight, it can read
# the old page, the converter then rewrites it (and, because the page is not yet
# in `done`, un-marks nothing), and mark_extracted afterwards records the NEW
# message_count: the new turns are never extracted. So the conversion runs only
# while holding wiki.lock, the lock every extraction job holds for its whole
# run. We WAIT for it (claude_ai_sync.lock_wait_seconds) instead of skipping, and
# if the wait runs out the drops stay in place for tomorrow, the job logs why
# and exits non-zero. It never converts without the lock.
JOB=sync-claude-ai
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
arm_heartbeat

INBOX="$ROOT/to_ingest"
DONE="$INBOX/processed"

shopt -s nullglob
drops=("$INBOX"/claude-ai-sync-*.json)
if (( ${#drops[@]} == 0 )); then
  log "no claude-ai-sync drops — nothing to convert"
  "$PY" -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))" \
      "$META/claude-ai-sync.json" 2>/dev/null | sed "s/^/[$JOB] state: /" || true
  exit 0
fi

WAIT=$("$PY" -c "import yaml;print(int(yaml.safe_load(open('$ROOT/config.yaml'))['claude_ai_sync']['lock_wait_seconds']))") \
  || die "cannot read claude_ai_sync.lock_wait_seconds from config.yaml"

exec 9>"$LOCKFILE"
log "${#drops[@]} drop(s) waiting; waiting up to ${WAIT}s for the vault lock ($LOCKFILE)"
if ! flock -w "$WAIT" 9; then
  log "ERROR: vault lock still held after ${WAIT}s (an extraction is running) — drops left in place for the next run, nothing converted"
  exit 1
fi
echo "$JOB:$$:$(date -Is)" >&9 || true
export DREAMER_LOCK_HELD=1   # same re-entrancy contract as acquire_lock

mkdir -p "$DONE"
rc=0
converted=0
for d in "${drops[@]}"; do
  log "converting: $d"
  if out=$("$PY" "$ROOT/scripts/convert_claude_export.py" "$d" 2>>"$LOGS/$JOB.log"); then
    n=$(printf '%s' "$out" | "$PY" -c 'import json,sys; s=json.load(sys.stdin); print(s["emitted"])') || n=0
    converted=$((converted + n))
    mv "$d" "$DONE/$(date +%Y%m%d-%H%M%S)-$(basename "$d")"
    log "done: $(basename "$d") ($n transcript(s)), archived to processed/"
  else
    log "ERROR: conversion failed for $d — left in place for retry"
    rc=1
  fi
done

(( converted > 0 )) && mark_produced
state=$("$PY" "$ROOT/scripts/claude_ai_host.py" --set-converted "$converted") \
  || { log "ERROR: could not record converted=$converted in the sync state"; rc=1; }
log "state: ${state:-none} (converted this run: $converted)"
exit "$rc"
