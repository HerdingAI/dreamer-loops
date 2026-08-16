#!/usr/bin/env bash
# Job 4 — ingest-cc: Claude Code session ingestion.
#
# Contract: no arguments. Sweeps the Claude Code projects directory, triages
# every session, summarises each one that qualifies, and writes one transcript
# page per session into vault/sources/transcripts/ with source_agent
# claude-code. Extraction stays the nightly/backfill jobs' business.
#
# It does NOT take the vault lock, for the same reason bin/ingest.sh does not:
# it is append-only into vault/sources/ via atomic writes, and a running
# backfill re-counts its queue every batch, so pages written here are picked up
# automatically.
#
# CC_LIMIT overrides cc_ingest.max_sessions_per_run for one run.
JOB=ingest-cc
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

log "start"

# 1. Triage. The scan prints stats on stdout and diagnostics on stderr.
LIMIT="${CC_LIMIT:-}"
SCAN=$("$PY" "$ROOT/scripts/convert_cc_sessions.py" ${LIMIT:+--limit "$LIMIT"}) \
  || die "scan failed"
ACCEPTED=$(printf '%s' "$SCAN" | "$PY" -c \
  'import json,sys; print(json.load(sys.stdin).get("accepted",0))')
log "scan: $SCAN"

if [[ "${ACCEPTED:-0}" -eq 0 ]]; then
  log "no new sessions — no-op"
  exit 0
fi

MAXT=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget'].get('max_turns_cc_ingest',10))")

written=0; refused=0; deferred=0
shopt -s nullglob
for prompt in "$LOGS"/.cc-input-*.md; do
  sid="$(basename "$prompt")"; sid="${sid#.cc-input-}"; sid="${sid%.md}"
  payload="$LOGS/.cc-input-$sid.json"
  [[ -f "$payload" ]] || { log "no payload for $sid — skipping"; continue; }
  result="$LOGS/.cc-result-$sid.json"

  # 2. Reconstruct (the LLM half), one session per call.
  if ! run_claude "$prompt" "$MAXT" "$result"; then
    # A usage-limit exit will hit every remaining session too, so stop rather
    # than burn the rest of the queue against a closed window. The session
    # stays `pending` in the ledger and the next run picks it up.
    log "summariser deferred on $sid — remaining sessions resume next run"
    deferred=$((deferred + 1))
    break
  fi

  # 3. Write the page — or refuse it. apply_cc_session.py records a refusal in
  # the ledger with its reason, so the sweep does not re-buy it tomorrow.
  if "$PY" "$ROOT/scripts/apply_cc_session.py" \
        --payload "$payload" --summary "$result" >/dev/null; then
    written=$((written + 1))
    rm -f "$prompt" "$payload" "$result" "$result".raw.json*
  else
    refused=$((refused + 1))
    record_event "cc-refused" \
      "ingest-cc refused the page for session $sid — see cc-ingested.json"
    # Keep the reply that was refused. A refusal is a claim about the
    # summariser's output, and deleting the output makes that claim
    # unfalsifiable — the first live run refused three sessions and left
    # nothing to say why.
    mv -f "$result" "$LOGS/.cc-refused-$sid.json" 2>/dev/null || true
    rm -f "$prompt" "$payload" "$result".raw.json*
  fi
done
shopt -u nullglob

log "written=$written refused=$refused deferred=$deferred"

if [[ "$written" -gt 0 ]]; then
  reindex
  commit "ingest-cc $(TODAY): $written session(s) ingested"
fi

REMAIN=$("$PY" "$ROOT/scripts/make_batch.py" --count-remaining)
log "extraction queue after ingest: $REMAIN"
exit 0
