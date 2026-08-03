#!/usr/bin/env bash
# Job 2 — weekly-dream (§6.9). Sunday 03:00.
# One `claude -p` invocation PER LOOP so a usage-limit exit costs one loop, not
# the week (Principle 9 requires checkpointing; v2.0 had none here).
JOB=weekly-dream
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
acquire_lock
log "start"

# 0. Read last week's ✓/✗ marks before regenerating anything.
"$PY" "$ROOT/scripts/digest.py" ingest-marks >/dev/null && log "marks ingested"

# 1. Recover loops stranded in `researching` by a prior deferral.
RECOVERED=$("$PY" "$ROOT/scripts/vault.py" recover | wc -l)
if [[ "$RECOVERED" -gt 0 ]]; then
  log "recovered $RECOVERED stranded loop(s)"
  record_event "recovery" "$RECOVERED loop(s) left in 'researching' by a prior deferral were reset to open and re-selected first"
fi

if should_skip_research; then
  log "previous run exceeded the cost ceiling — skipping research this week"
  "$PY" "$ROOT/scripts/digest.py" build --quiet-reason \
      "Research skipped: the previous run exceeded the configured cost ceiling." >/dev/null
  clear_skip_research
  regen_catalog; commit "weekly-dream $(TODAY): research skipped (cost ceiling)"
  exit 0
fi

# 2. Select.
SELECTED=$("$PY" "$ROOT/scripts/vault.py" select | awk '{print $1}')
if [[ -z "$SELECTED" ]]; then
  log "no loop qualifies — quiet week"
  "$PY" "$ROOT/scripts/digest.py" build --quiet-reason \
      "No loop reached the recurrence threshold this week." >/dev/null
  regen_catalog; commit "weekly-dream $(TODAY): quiet week"
  exit 0
fi

MAXT=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget']['max_turns_weekly'])")

for LOOP in $SELECTED; do
  log "researching $LOOP"
  "$PY" "$ROOT/scripts/make_dream_prompt.py" --loop "$LOOP" \
        --out "$ROOT/logs/.prompt-dream-$LOOP.md" || { log "prompt failed for $LOOP"; continue; }

  # Mark in-flight so a crash is detectable and recoverable next week.
  "$PY" -c "
import sys; sys.path.insert(0,'scripts')
import vault as V
l=[x for x in V.load_loops() if x.id=='$LOOP'][0]; l.status='researching'; l.save()"

  if ! run_claude "$ROOT/logs/.prompt-dream-$LOOP.md" "$MAXT" "$ROOT/logs/.result-dream-$LOOP.json"; then
    log "deferred on $LOOP — remains researching, recovered next week"
    break
  fi
  if ! "$PY" "$ROOT/scripts/apply_conclusion.py" --loop "$LOOP" \
        --input "$ROOT/logs/.result-dream-$LOOP.json"; then
    # No conclusion was written, so grading here would score a stale page from
    # an earlier run and a "researched" commit would put a false success in git
    # history (observed live 2026-08-02: L0012). apply_conclusion has already
    # reset the loop to open and staged a digest event; commit that honestly
    # and move on — same quarantine-and-continue contract as backfill.sh.
    log "apply failed for $LOOP — no conclusion written, loop reset to open"
    record_event "apply-failure" "$LOOP research produced no usable conclusion (apply_conclusion failed); the loop was reset to open and the model's prose preserved for next week"
    commit "weekly-dream $(TODAY): $LOOP apply FAILED (no conclusion written)"
    continue
  fi

  # Grade the conclusion we just wrote. A page can cite correctly, lint clean,
  # and still decide nothing — the rubric is the only automatic detector of
  # that, and it is worth nothing if it only ever runs when someone remembers.
  # Structural only: a low score means "probably not worth your ten minutes",
  # never "wrong".
  QSCORE=$("$PY" "$ROOT/scripts/grade_conclusions.py" --json --loop "$LOOP" \
      2>/dev/null | "$PY" -c '
import json,sys
try:
    rows=json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
if not rows: print(""); raise SystemExit
r=sorted(rows,key=lambda x:x["created"])[-1]
print(f'"'"'{r["score"]:.2f}|{",".join(r["failed"])}'"'"')' || true)
  if [[ -n "$QSCORE" ]]; then
    SC="${QSCORE%%|*}"; FAILS="${QSCORE#*|}"
    log "conclusion quality $LOOP: $SC${FAILS:+ (fails: $FAILS)}"
    "$PY" -c "
import sys; sys.path.insert(0,'$ROOT/scripts')
import digest as G
G.stage('quality', {'loop':'$LOOP','score':float('$SC'),'failed':'''$FAILS'''})"
    if "$PY" -c "import sys; sys.exit(0 if float('$SC') < 0.7 else 1)"; then
      log "WARN low-quality conclusion for $LOOP ($SC) — surfaced in digest"
      record_event "quality" "$LOOP conclusion scored $SC on the structural rubric (fails: $FAILS) — read it before trusting it"
    fi
  fi
  commit "weekly-dream $(TODAY): $LOOP researched"
done

# 3. Refresh merge proposals — the conservative bias rule only self-heals if
#    something actually offers the split back to the owner.
#    It now runs an LLM judge over embedding-derived candidates, so it is a
#    paid leg and its output is worth logging rather than discarding.
MERGE_OUT=$("$PY" "$ROOT/scripts/merge_proposals.py" refresh 2>&1) \
  && log "merge proposals refreshed: $(printf '%s' "$MERGE_OUT" | tr -d '\n ')" \
  || log "ERROR: merge proposal refresh failed — false splits will not self-heal this week"

# A judge outage degrades to the token-overlap rule rather than to nothing, but
# a run that judged nothing must not read as a run that found nothing.
JERR=$(printf '%s' "$MERGE_OUT" | "$PY" -c '
import json,sys
try: print(json.load(sys.stdin).get("judge_errors", 0))
except Exception: print(0)' 2>/dev/null || echo 0)
if [[ "${JERR:-0}" -gt 0 ]]; then
  log "WARN merge judge failed on $JERR pair(s) — surfaced in digest"
  record_event "merge" "the Stage-B judge failed on $JERR candidate pair(s) this week; those fell back to token overlap, so near-duplicates with little shared wording may have been missed"
fi

# 4. Lint, digest, catalog.
"$PY" "$ROOT/scripts/vault.py" lint || log "lint reported problems (see above)"
"$PY" "$ROOT/scripts/digest.py" build >/dev/null && log "digest written"
reindex
regen_catalog
commit "weekly-dream $(TODAY): digest"
log "done"
