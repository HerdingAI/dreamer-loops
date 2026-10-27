#!/usr/bin/env bash
# Job: nightly-assess (§4, §7.1). Nightly leg following nightly-extract.
JOB=nightly-assess
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

run_healthcheck "before assess leg"

if leg_blocked "assess"; then
  log "assess leg is blocked by healthcheck — skipping"
  record_event "degraded" "assess leg blocked by healthcheck; work resumes when health passes"
  exit 0
fi

acquire_lock
log "start"

# Settle owner-write verification first, and regardless of goal count: owner
# writes also resolve loops. A write past the verify lag with no matching
# Human turn is quarantined and comes back as an open question (§5.4).
# The fold-quarantine sweep rides along: entries are synced when they are
# quarantined, and this catches any that predate that (§5.6).
"$PY" -c "import sys, json; sys.path.insert(0, '$ROOT/scripts'); import questions as Q; print(json.dumps({'writes': Q.reconcile_owner_writes(), 'fold_quarantine': Q.sync_fold_quarantine()}))" \
  >> "$LOGS/$JOB.log" 2>&1 || record_event "degraded" "owner-write reconciliation failed; see the $JOB log"

# Check active goals count (§4.1: with 0 active goals, clean no-op, evented line, exit 0, no LLM call)
ACTIVE_COUNT=$("$PY" -c "import sys; sys.path.insert(0, '$ROOT/scripts'); import goals as G; print(G.active_goals_count())" 2>/dev/null || echo 0)

if [[ "$ACTIVE_COUNT" -eq 0 ]]; then
  log "0 active goals — assess is a clean no-op"
  regen_catalog
  commit "nightly-assess: no active goals $(TODAY)"
  exit 0
fi

PROMPT_FILE="$LOGS/.prompt-$JOB.md"
RESULT_FILE="$LOGS/.result-$JOB.json"
APPLIED_FILE="$LOGS/.applied-$JOB.json"
BATCH_FILE="$LOGS/.batch-$JOB.txt"

# Next unassessed transcripts, by ledger (vault/.vault-meta/assessed.json).
"$PY" "$ROOT/scripts/apply_assessment.py" select --out "$PROMPT_FILE" --list "$BATCH_FILE" >/dev/null \
  2>>"$LOGS/$JOB.log" || die "assess batch selection failed"

if [[ -s "$BATCH_FILE" ]]; then
  MAXT=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget'].get('max_turns_assess',30))" 2>/dev/null || echo 30)
  if ! run_claude "$PROMPT_FILE" "$MAXT" "$RESULT_FILE"; then
    log "deferred — assess leg deferred, will retry next run"
    commit "nightly-assess: deferred $(TODAY)"
    exit 0
  fi
else
  # Nothing new to read is exactly when silence matters: skip the model, but
  # still run the detectors over the clocks.
  log "no unassessed transcripts — detectors only, no LLM call"
  echo '{"attachments": [], "unattached": [], "observations": []}' > "$RESULT_FILE"
fi

# Apply assess payload
"$PY" "$ROOT/scripts/apply_assessment.py" apply --input "$RESULT_FILE" > "$APPLIED_FILE" 2>>"$LOGS/$JOB.log" || die "apply_assessment failed"

# Only a batch that was applied is checkpointed; a deferral or a failed apply
# leaves it selected again tomorrow.
"$PY" "$ROOT/scripts/apply_assessment.py" mark --list "$BATCH_FILE" 2>>"$LOGS/$JOB.log" || die "assess checkpoint failed"

# Drain assessment folds for the attachments that were actually applied
MAXT_FOLD=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget'].get('max_turns_assess_fold',8))" 2>/dev/null || echo 8)
FOLD_LIST="$LOGS/.assess-folds-$JOB.tsv"

"$PY" - "$APPLIED_FILE" "$LOGS" "$FOLD_LIST" <<'PYEOF'
import json, os, sys
from pathlib import Path
root = Path(os.environ.get("ROOT", "."))
sys.path.insert(0, str(root / "scripts"))
import apply_assessment as AA
import goals as G
import vault as V

res_file, logs_dir, out_tsv = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
try:
    data = json.loads(res_file.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)

lines = []
for n, att in enumerate(data.get("applied") or []):
    gid, transcript_link, date_str = att["goal_id"], att["transcript"], att["date"]
    tr_path = V._resolve_wikilink(transcript_link)
    if G.load_goal(gid) is None or not tr_path:
        continue
    # Indexed: one goal can take several attachments in a night, and a shared
    # filename would let the second prompt overwrite the first before it ran.
    fold_prompt = logs_dir / f".prompt-assess-fold-{gid}-{n}.md"
    fold_result = logs_dir / f".result-assess-fold-{gid}-{n}.json"
    AA.build_assessment_fold_prompt(gid, transcript_link, str(tr_path), fold_prompt)
    lines.append(f"{gid}\t{transcript_link}\t{date_str}\t{fold_prompt}\t{fold_result}")

out_tsv.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
PYEOF

if [[ -f "$FOLD_LIST" ]]; then
  while IFS=$'\t' read -r F_GID F_TR_LINK F_DATE F_PROMPT F_RESULT; do
    [[ -z "$F_GID" ]] && continue
    if run_claude "$F_PROMPT" "$MAXT_FOLD" "$F_RESULT" restricted; then
      "$PY" "$ROOT/scripts/apply_assessment.py" apply-fold --goal "$F_GID" --attachment "$F_TR_LINK" \
        --date "$F_DATE" --input "$F_RESULT" >> "$LOGS/$JOB.log" 2>&1 \
        || log "WARN assess-fold apply refused or failed for $F_GID"
    else
      log "WARN assess-fold failed or deferred for $F_GID"
    fi
  done < "$FOLD_LIST"
  rm -f "$FOLD_LIST"
fi

regen_catalog
commit "nightly-assess $(TODAY): $(jq -r '"attachments=\(.applied_attachments) clocks=\(.clocks_updated) questions=\(.questions_enqueued)"' "$APPLIED_FILE" 2>/dev/null || echo done)"
log "done"
