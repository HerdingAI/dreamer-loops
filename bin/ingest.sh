#!/usr/bin/env bash
# Standardized export ingestion (owner-requested 2026-08-02).
#
# Contract: drop a claude.ai export — the zip, or the unzipped folder, or a
# bare conversations.json — anywhere under to_ingest/, then run this script
# (no arguments). It finds every drop, converts each through the dedupe
# ledger (safe to re-run; already-seen conversations are skipped), and moves
# the processed drop into to_ingest/processed/<timestamp>-<name>/ so the
# inbox only ever contains unprocessed material.
#
# It does NOT take the vault lock: the converter is append-only into
# vault/sources/ via atomic writes, so it is safe to run while a backfill or
# nightly job holds the lock — and the running backfill re-counts its queue
# every batch, so freshly converted transcripts are picked up automatically.
# Extraction itself stays the nightly/backfill jobs' responsibility.
JOB=ingest
# _common.sh only defines helpers; the lock is a function we deliberately
# do not call (see contract above).
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

INBOX="$ROOT/to_ingest"
DONE="$INBOX/processed"
mkdir -p "$INBOX" "$DONE"

shopt -s nullglob
found=0

process() { # $1 = path handed to the converter, $2 = drop to archive
  local src="$1" drop="$2"
  found=1
  log "ingesting: $drop"
  if "$PY" "$ROOT/scripts/convert_claude_export.py" "$src" 2>&1 | tee -a "$LOGS/$JOB.log"; then
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    mv "$drop" "$DONE/$stamp-$(basename "$drop")"
    # The sibling zip of an unzipped folder (or vice versa) is the same drop.
    local twin="${drop%.zip}"; [[ "$twin" == "$drop" ]] && twin="$drop.zip"
    [[ -e "$twin" ]] && mv "$twin" "$DONE/$stamp-$(basename "$twin")"
    log "done: archived to processed/$stamp-$(basename "$drop")"
  else
    log "ERROR: conversion failed for $drop — left in place for retry"
    return 1
  fi
}

rc=0
for z in "$INBOX"/*.zip; do
  # Prefer the unzipped twin if the owner already extracted it.
  [[ -d "${z%.zip}" ]] && continue
  process "$z" "$z" || rc=1
done
for d in "$INBOX"/*/; do
  d="${d%/}"
  [[ "$d" == "$DONE" ]] && continue
  if [[ -f "$d/conversations.json" ]]; then
    process "$d/conversations.json" "$d" || rc=1
  fi
done
for j in "$INBOX"/*.json; do
  process "$j" "$j" || rc=1
done

if [[ "$found" -eq 0 ]]; then
  log "inbox empty — nothing to ingest"
fi

REMAIN=$("$PY" "$ROOT/scripts/make_batch.py" --count-remaining)
log "extraction queue after ingest: $REMAIN"
log "run ./bin/backfill.sh to extract now, or let the nightly job take it"
exit "$rc"
