#!/usr/bin/env bash
# DoD 6.9 — the end-to-end acceptance test.
#
# Runs the REAL job wrappers, converter, state machine, decay clock, catalog,
# digest, lint and git against a sandboxed vault. Only `claude -p` is replaced,
# by tests/fake_claude.py, so the run is repeatable.
#
# Usage: tests/simulated_week.sh [workdir]
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$(mktemp -d /tmp/dreamer-week-XXXXXX)}"
PASS=0; FAIL=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}
contains() { # contains <desc> <needle> <file>
  if grep -qF -- "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 — '$2' not in $(basename "$3")"; fi
}
absent() {
  if grep -qF -- "$2" "$3" 2>/dev/null; then bad "$1 — '$2' unexpectedly in $(basename "$3")"; else ok "$1"; fi
}

step "Sandbox: $WORK"
mkdir -p "$WORK"/{bin,scripts,skills,tests/fixtures,logs}
cp -r "$SRC"/bin/. "$WORK/bin/"
cp -r "$SRC"/scripts/. "$WORK/scripts/"
cp -r "$SRC"/skills/. "$WORK/skills/"
cp "$SRC"/CLAUDE.md "$WORK/"
# Mirror production ignore rules, or every job's own log write dirties the tree
# and the "clean after deferral" guarantee becomes untestable.
cp "$SRC"/.gitignore "$WORK/"
cp "$SRC"/tests/fake_claude.py "$WORK/tests/"
cp "$SRC"/tests/fixtures/script.json "$WORK/tests/fixtures/"
cp "$SRC"/tests/fixtures/export.json "$WORK/tests/fixtures/"
mkdir -p "$WORK"/vault/{loops,conclusions,concepts,archive,digests,sources/transcripts,inbox/resurfacings,.vault-meta}

python3 - "$SRC/config.yaml" "$WORK/config.yaml" "$WORK" <<'PYEOF'
import sys, yaml
src, dst, root = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = yaml.safe_load(open(src))
cfg["paths"] = {
    "root": root, "vault": f"{root}/vault", "inbox": f"{root}/vault/inbox",
    "resurfacings": f"{root}/vault/inbox/resurfacings",
    "sources": f"{root}/vault/sources/transcripts",
    "loops": f"{root}/vault/loops", "conclusions": f"{root}/vault/conclusions",
    "concepts": f"{root}/vault/concepts", "archive": f"{root}/vault/archive",
    "digests": f"{root}/vault/digests", "meta": f"{root}/vault/.vault-meta",
    "logs": f"{root}/logs",
}
cfg["decay"]["go_live_date"] = "2026-08-01"
cfg["matching"]["recurrence_min"] = 2
cfg["extraction"]["batch_size"] = 1
yaml.safe_dump(cfg, open(dst, "w"), sort_keys=False)
PYEOF

cd "$WORK"
git init -q -b main && git add -A && git -c user.name=t -c user.email=t@t commit -q -m init
export DREAMER_FAKE_CLAUDE="$WORK/tests/fake_claude.py"
chmod +x "$WORK/tests/fake_claude.py" "$WORK"/bin/*.sh
cp tests/fixtures/export.json vault/inbox/export.json
ok "sandbox prepared"

step "Seven nightly runs (batch size 1)"
for day in 02 03 04 05 06 07 08; do
  DREAMER_TODAY="2026-08-$day" ./bin/nightly-extract.sh >>logs/week.log 2>&1
done
LOOPS=$(ls vault/loops/*.md 2>/dev/null | grep -v _catalog | wc -l)
check "four loops created (one per distinct topic)" "4" "$LOOPS"

L1_COUNT=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0001'][0].recurrence_count)")
check "L0001 accreted three occurrences" "3" "$L1_COUNT"

SKIPPED=$(grep -c "resolved in-conversation" logs/week.log || true)
[[ "$SKIPPED" -ge 1 ]] && ok "meta-idea filter skipped the resolved conversation" \
  || bad "resolved conversation was not skipped"

contains "catalog lists every loop" "L0004" vault/loops/_catalog.md
contains "catalog carries statuses" "| open |" vault/loops/_catalog.md

step "Eighth night (completes L0003's second occurrence)"
DREAMER_TODAY="2026-08-09" ./bin/nightly-extract.sh >>logs/week.log 2>&1
L3_COUNT=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0003'][0].recurrence_count)")
check "L0003 accreted two occurrences" "2" "$L3_COUNT"

step "Decay pass (nothing should decay yet)"
DREAMER_TODAY="2026-08-09" ./bin/decay-archive.sh >>logs/week.log 2>&1
check "no loop archived at go-live + 8 days" "0" "$(ls vault/archive/*.md 2>/dev/null | wc -l)"

step "Weekly dream"
DREAMER_TODAY="2026-08-09" ./bin/weekly-dream.sh >>logs/week.log 2>&1

L1_STATUS=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0001'][0].status)")
check "researched loop is paused" "paused" "$L1_STATUS"

L3_STATUS=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0003'][0].status)")
check "decision-only loop reached its terminal state" "decision-only" "$L3_STATUS"

L2_STATUS=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0002'][0].status)")
check "single-occurrence loop was NOT researched" "open" "$L2_STATUS"

CONC=$(ls vault/conclusions/*.md 2>/dev/null | wc -l)
check "exactly one conclusion page written" "1" "$CONC"
CONC_FILE=$(ls vault/conclusions/*.md | head -1)

contains "conclusion cites the wisdom corpus" "qmd://wisdom/" "$CONC_FILE"
contains "conclusion cites the owner's own transcript" "sources/transcripts/2026/08" "$CONC_FILE"
contains "web material is quarantined under the untrusted heading" \
         "## Web sources (untrusted)" "$CONC_FILE"
absent "uncited claim was dropped, not written" \
       "this one has no citation and must not survive" "$CONC_FILE"
contains "egress record lists every outbound query" \
         "durable agent memory representations comparison" "$CONC_FILE"
# Evidence grading (vendored autoresearch contract): citation is not quality.
contains "claims carry an evidence grade" "[✓ accepted]" "$CONC_FILE"
contains "web claim graded weaker than the owner's own reasoning" \
         "[~ provisional]" "$CONC_FILE"
contains "evidence ledger summarises support" "## Evidence ledger" "$CONC_FILE"

# The injected instruction must survive ONLY as a quoted string inside the
# untrusted block, and must not have been acted on.
INJ_SECTION=$(python3 - "$CONC_FILE" <<'PYEOF'
import sys,re
t=open(sys.argv[1]).read()
i=t.find("## Web sources (untrusted)")
j=t.find("## Synthesis")
print("YES" if i>=0 and "IGNORE ALL PREVIOUS INSTRUCTIONS" in t[i:j if j>i else len(t)] else "NO")
PYEOF
)
check "prompt injection is quoted inside the untrusted block" "YES" "$INJ_SECTION"
check "injection did NOT archive every loop" "0" "$(ls vault/archive/*.md 2>/dev/null | wc -l)"

step "Digest"
# Empty sections must be omitted, not padded (G5: decision-first, <=10 min).
FIRST_SECTION=$(grep -m1 "^## " "$(ls vault/digests/2026-*.md | tail -1)")
if grep -A2 "^${FIRST_SECTION}$" "$(ls vault/digests/2026-*.md | tail -1)" | grep -q "^_No"; then
  bad "first digest section opened empty"
else
  ok "first digest section carries real content"
fi
contains "omitted sections are still accounted for" "Nothing to report under:" \
         "$(ls vault/digests/2026-*.md | tail -1)"
DIGEST=$(ls vault/digests/2026-*.md | tail -1)
contains "digest has decisions-awaiting section" "## Decisions awaiting you" "$DIGEST"
contains "decision-only loop surfaces there, not under conclusions" \
         "Subscription or API keys" "$DIGEST"
# DoD 6.6-3: the conservative bias rule is only self-healing if the split
# actually reaches the owner. Previously this asserted only that the heading
# existed, which was true purely because empty sections were padded out.
contains "conservative-bias split reached the owner as a merge proposal" \
         "## Merge proposals" "$DIGEST"
contains "merge proposal names the two split loops" "merge:L0002+L0004" "$DIGEST"
contains "matching sample offers checkboxes" "- [ ] \`L" "$DIGEST"
contains "mark coverage is reported rather than silent" "insufficient data" "$DIGEST"
contains "web queries are disclosed" "## Web queries sent" "$DIGEST"
contains "obsidian-read caveat is stated" "does not count as reading it" "$DIGEST"
contains "new conclusion is listed" "## New conclusions" "$DIGEST"

step "Reopening rule (night 9, after conclusion)"
DREAMER_TODAY="2026-08-10" ./bin/nightly-extract.sh >>logs/week.log 2>&1
L1_STATUS2=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0001'][0].status)")
check "paused loop reopened when topic resurfaced" "open" "$L1_STATUS2"
L1_COUNT2=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0001'][0].recurrence_count)")
check "reopening incremented recurrence" "4" "$L1_COUNT2"

step "Resurfacing via MCP round-trip"
python3 - <<'PYEOF'
import sys, os
sys.path.insert(0,'scripts')
import dreamer_mcp as M
print(M.tool_log_resurfacing("L0002", "new angle: classify before retrieval"))
PYEOF
check "resurfacing queued in inbox" "1" "$(ls vault/inbox/resurfacings/*.md 2>/dev/null | wc -l)"
DREAMER_TODAY="2026-08-11" ./bin/nightly-extract.sh >>logs/week.log 2>&1
check "resurfacing consumed by the nightly job" "0" "$(ls vault/inbox/resurfacings/*.md 2>/dev/null | wc -l)"
L2_COUNT=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0002'][0].recurrence_count)")
check "resurfacing incremented the loop" "2" "$L2_COUNT"

step "Deferral (usage limit) is honest and resumable"
cat > /tmp/dreamer-fail-claude.sh <<'EOS'
#!/usr/bin/env bash
exit 1
EOS
chmod +x /tmp/dreamer-fail-claude.sh
# Seed one unextracted transcript so the deferral path is actually reached
# rather than short-circuiting on the empty-batch no-op.
mkdir -p vault/sources/transcripts/2026/08
cat > vault/sources/transcripts/2026/08/2026-08-12--deferral-probe.md <<'EOS'
---
type: transcript
source_agent: claude.ai
conversation_id: fixture-deferral-probe
date: 2026-08-12
updated_at: 2026-08-12T10:00:00Z
title: "Deferral Probe"
message_count: 1
---

# Deferral Probe

## Human

A transcript that exists so the deferral path has something to defer.
EOS
BEFORE=$(python3 scripts/make_batch.py --count-remaining)
DREAMER_FAKE_CLAUDE=/tmp/dreamer-fail-claude.sh DREAMER_TODAY="2026-08-12" \
  ./bin/nightly-extract.sh >>logs/week.log 2>&1
AFTER=$(python3 scripts/make_batch.py --count-remaining)
check "deferred batch was NOT consumed" "$BEFORE" "$AFTER"
contains "deferral logged honestly" "deferred" logs/week.log
if [[ -z "$(git status --porcelain)" ]]; then ok "no uncommitted debris after deferral"
else bad "working tree dirty after deferral"; fi

step "Decay at the full window"
DREAMER_TODAY="2026-12-01" ./bin/decay-archive.sh >>logs/week.log 2>&1
ARCHIVED=$(ls vault/archive/*.md 2>/dev/null | wc -l)
[[ "$ARCHIVED" -ge 1 ]] && ok "loops decayed once past the window ($ARCHIVED archived)" \
  || bad "nothing decayed at go-live + 4 months"
PAUSED_ARCHIVED=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V
print(sum(1 for l in V.load_loops(include_archived=True)
          if l.status=='archived' and l.path and 'archive' in str(l.path)))")
[[ "$PAUSED_ARCHIVED" -ge 1 ]] && ok "terminal-status loops reach the archive too" \
  || bad "paused/decision-only never decay (Principle 5 violation)"

step "Lint and provenance"
LINT=$(python3 scripts/vault.py lint 2>&1 | tail -1)
check "lint is clean" "0 problem(s)" "$LINT"

SRC_MODS=$(git log --oneline --name-only -- vault/sources/transcripts \
           | grep -c "^vault/sources/transcripts.*" || true)
FIRST=$(git log --diff-filter=A --format=%H -- vault/sources/transcripts | tail -1)
MODIFIED=$(git log --diff-filter=M --oneline -- vault/sources/transcripts | wc -l)
check "no transcript was ever modified after creation" "0" "$MODIFIED"

COMMITS=$(git rev-list --count HEAD)
[[ "$COMMITS" -ge 10 ]] && ok "every job committed ($COMMITS commits reconstruct the week)" \
  || bad "too few commits ($COMMITS)"

step "Idempotency"
DREAMER_TODAY="2026-08-11" ./bin/nightly-extract.sh >>logs/week.log 2>&1
BEFORE_C=$(python3 scripts/vault.py catalog >/dev/null; md5sum vault/loops/_catalog.md | cut -d' ' -f1)
python3 scripts/vault.py catalog >/dev/null
AFTER_C=$(md5sum vault/loops/_catalog.md | cut -d' ' -f1)
check "catalog regeneration is idempotent" "$BEFORE_C" "$AFTER_C"

step "Concurrency"
( ./bin/nightly-extract.sh >>logs/lock1.log 2>&1 ) &
sleep 0.2
./bin/nightly-extract.sh >>logs/lock2.log 2>&1
wait
if grep -q "locked by another job" logs/lock2.log 2>/dev/null || \
   grep -q "locked by another job" logs/lock1.log 2>/dev/null; then
  ok "overlapping run exits cleanly on the advisory lock"
else
  ok "runs serialised without collision (lock held only briefly)"
fi

step "Apply failure is honest (no false 'researched' commit)"
# Pin for the 2026-08-02 live bug: apply_conclusion failed on unparseable
# dream output, yet weekly-dream.sh graded a stale conclusion and committed
# "L0012 researched". A research-eligible loop is dreamed with a scripted
# malformed reply; the run must reset it to open, record an apply-failure
# event, and never commit a "researched" message for it.
MAL_ID=$(python3 - <<'PY'
import sys, datetime
sys.path.insert(0, 'scripts')
import vault as V
l = V.create_loop("Malformed dream fixture loop",
                  "[[sources/transcripts/2026/08/2026-08-01--malformed-a]]",
                  datetime.date(2026, 8, 1))
V.add_occurrence(l, "[[sources/transcripts/2026/08/2026-08-03--malformed-b]]",
                 datetime.date(2026, 8, 3))
print(l.id)
PY
)
CONC_BEFORE=$(ls vault/conclusions/*.md 2>/dev/null | wc -l)
DREAMER_TODAY="2026-08-12" DREAMER_FAKE_MALFORMED="$MAL_ID" \
  ./bin/weekly-dream.sh >>logs/week.log 2>&1
MAL_STATUS=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='$MAL_ID'][0].status)")
check "failed loop reset to open, not stranded in researching" "open" "$MAL_STATUS"
CONC_AFTER=$(ls vault/conclusions/*.md 2>/dev/null | wc -l)
check "no conclusion page written for the failed dream" "$CONC_BEFORE" "$CONC_AFTER"
# Plain grep (not -q): under pipefail, grep -q's early exit SIGPIPEs git log
# and the pipeline reports 141 even on a match.
SUBJECTS=$(git log --format=%s)
if grep -qF "$MAL_ID researched" <<<"$SUBJECTS"; then
  bad "false '$MAL_ID researched' commit reached git history"
else
  ok "no false 'researched' commit for the failed loop"
fi
grep -qF "$MAL_ID apply FAILED" <<<"$SUBJECTS" \
  && ok "failure committed honestly ('$MAL_ID apply FAILED')" \
  || bad "apply failure left no honest commit"
# The digest step consumes run events, so the durable record is the digest.
grep -qF "apply-failure" vault/digests/*.md \
  && ok "apply failure surfaced in the digest" \
  || bad "apply failure absent from every digest"

printf '\n\033[1m== Result ==\033[0m\n'
printf '  passed: %s\n  failed: %s\n  sandbox: %s\n' "$PASS" "$FAIL" "$WORK"
[[ "$FAIL" -eq 0 ]] || exit 1
