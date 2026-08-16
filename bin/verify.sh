#!/usr/bin/env bash
# Post-backfill acceptance sweep (steps 3-4 of the 2026-08-02 plan).
#
# Runs every deterministic gate in one pass so corpus growth cannot silently
# degrade quality. Deliberately NOT part of backfill.sh: this must be runnable
# on demand, and a failing gate should stop the operator, not a batch loop.
#
# Usage: ./bin/verify.sh [--skip-golden]
#   --skip-golden   omit the LLM-judge golden set (the only paid gate)
JOB=verify
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

SKIP_GOLDEN=0
[[ "${1:-}" == "--skip-golden" ]] && SKIP_GOLDEN=1

fails=0
gate() { # name, condition-exit-code
  if [[ "$2" -eq 0 ]]; then log "PASS  $1"; else log "FAIL  $1"; fails=$((fails+1)); fi
}

log "=== post-backfill acceptance sweep ==="

# 1. Re-embed. Retrieval quality is meaningless against a stale index, and a
#    silent staleness here would make every downstream number a lie.
reindex; gate "reindex (config qmd.collections, update + embed)" $?

# 2. Catalog — the file every matching run reads first (CLAUDE.md rule 11).
regen_catalog; gate "catalog regenerated" $?

# 3. Structural integrity of the vault.
"$PY" "$ROOT/scripts/vault.py" lint | tee "$LOGS/.verify-lint.txt"
# The linter prints "0 problem(s)", not "0 problems" — matching the plural
# form meant this gate could never pass, and reported FAIL on a clean vault.
grep -q "^0 problem" "$LOGS/.verify-lint.txt"
gate "vault lint clean" $?

# 4. Unit tests.
test_rc=0
for f in "$ROOT"/tests/test_*.py; do
  "$PY" "$f" >/dev/null 2>&1 || { log "  unit failures in $(basename "$f")"; test_rc=1; }
done
gate "unit tests" "$test_rc"

# 5. Retrieval recall on the owner's own speech-to-text (the gate his
#    instruction created — clean probes alone hid a 5/14 failure).
"$PY" "$ROOT/scripts/probe_recall.py" | tee "$LOGS/.verify-recall.txt"
grep -q "GATE PASS" "$LOGS/.verify-recall.txt"
gate "retrieval recall >= 80% on raw transcript openings" $?

# 6. Conclusion quality rubric (structural only — never a correctness claim).
"$PY" "$ROOT/scripts/grade_conclusions.py" | tail -20

# 7. Golden set. Matching is the gate most at risk from corpus growth: more
#    loops means more near-neighbours and more chance of a false merge.
if [[ "$SKIP_GOLDEN" -eq 0 ]]; then
  "$PY" "$ROOT/scripts/golden_set.py" run --judge llm | tee "$LOGS/.verify-golden.txt"
  gate "golden set (see output for the >=80% adversarial gate)" $?
else
  log "SKIP  golden set (--skip-golden)"
fi

log "=== sweep complete: $fails gate(s) failed ==="
exit "$fails"
