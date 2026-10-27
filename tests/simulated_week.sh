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
mkdir -p "$WORK"/vault/{loops,goals,conclusions,concepts,archive,digests,sources/transcripts,inbox/resurfacings,.vault-meta}

# U6 — the sim runs with a FROZEN tag vocabulary so the extraction tag path is
# exercised end to end: the fixtures emit one vocabulary tag (must land in
# frontmatter) and one invented tag (must be dropped at apply, CLAUDE.md rule 4).
cat > "$WORK/vault/.vault-meta/tag-vocabulary.json" <<'EOS'
{"frozen_on": "2026-08-01", "tags": ["note-taking", "operations"]}
EOS

python3 - "$SRC/config.yaml" "$WORK/config.yaml" "$WORK" <<'PYEOF'
import sys, yaml
src, dst, root = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = yaml.safe_load(open(src))
cfg["paths"] = {
    "root": root, "vault": f"{root}/vault", "inbox": f"{root}/vault/inbox",
    "resurfacings": f"{root}/vault/inbox/resurfacings",
    "sources": f"{root}/vault/sources/transcripts",
    "loops": f"{root}/vault/loops", "goals": f"{root}/vault/goals",
    "conclusions": f"{root}/vault/conclusions",
    "concepts": f"{root}/vault/concepts", "archive": f"{root}/vault/archive",
    "digests": f"{root}/vault/digests", "meta": f"{root}/vault/.vault-meta",
    "logs": f"{root}/logs",
}
# Outside the repo, as ~/.claude/projects is in production.
cfg.setdefault("corpora", {})["claude_code_sessions"] = f"{root}-cc-projects"
cfg["decay"]["go_live_date"] = "2026-08-01"
cfg["matching"]["recurrence_min"] = 2
cfg["extraction"]["batch_size"] = 1
yaml.safe_dump(cfg, open(dst, "w"), sort_keys=False)
PYEOF

# --- qmd isolation -------------------------------------------------------
# The jobs run real `qmd update`/`qmd embed`/`qmd status` unless shadowed, and
# qmd resolves the SHARED index — a sandbox test must never touch it. A stub
# first on PATH absorbs every call: `status` prints the minimal healthy shape
# the healthcheck parsers accept (zero pending, all configured collections
# fresh), everything else succeeds silently.
#
# PATH order subtlety (same problem tests/test_reindex.py solves): sourcing
# bin/_common.sh PREPENDS every nvm node bin dir carrying qmd — but only when
# that dir is not already on PATH. So we put those nvm dirs on PATH ourselves
# first, then put the stub dir in front of them; _common.sh then leaves the
# order alone and the stub wins.
mkdir -p "$WORK/stubbin"
cat > "$WORK/stubbin/qmd" <<'EOS'
#!/usr/bin/env bash
# Sandbox qmd stub (simulated_week.sh) — the real index is out of reach.
# Touch "<stubdir>/qmd-stale" to model a broken index: status reports every
# collection long-stale AND update/embed fail, so the pre-gate reindex writes
# empty coverage and healthcheck's index-fresh assertion blocks research.
MARKER="$(dirname "$0")/qmd-stale"
AGE="1h"; [[ -f "$MARKER" ]] && AGE="999h"
case "${1:-}" in
  status)
    cat <<STATUS
QMD Status

Index: /sandbox/.qmd/index.sqlite
Size:  1.0 MB

Documents
  Total:    16 files indexed
  Vectors:  160 embedded
  Updated:  1m ago

Collections
  vault (qmd://vault/)
    Pattern:  **/*.md
    Files:    4 (updated $AGE ago)
  transcripts (qmd://transcripts/)
    Pattern:  **/*.md
    Files:    4 (updated $AGE ago)
  conclusions (qmd://conclusions/)
    Pattern:  **/*.md
    Files:    4 (updated $AGE ago)
  wisdom (qmd://wisdom/)
    Pattern:  **/*.md
    Files:    4 (updated $AGE ago)
STATUS
    ;;
  update|embed)
    [[ -f "$MARKER" ]] && exit 1
    ;;
  *) : ;; # anything else: silent success
esac
exit 0
EOS
chmod +x "$WORK/stubbin/qmd"
for _nodebin in "$HOME"/.nvm/versions/node/*/bin; do
  [ -x "$_nodebin/qmd" ] && case ":$PATH:" in
    *":$_nodebin:"*) ;;
    *) PATH="$_nodebin:$PATH" ;;
  esac
done
unset _nodebin
PATH="$WORK/stubbin:$PATH"
export PATH
if [[ "$(command -v qmd)" == "$WORK/stubbin/qmd" ]]; then
  ok "qmd stub active — sandbox runs cannot reach the real index"
else
  bad "qmd stub NOT first on PATH (resolves to $(command -v qmd))"
fi

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
contains "catalog carries a first_seen column (U9)" \
         "| id | title | status | first_seen | count | last_seen |" \
         vault/loops/_catalog.md

step "Tagging (U6): vocabulary tags accepted, invented tags dropped"
contains "vocabulary tag landed in new-loop frontmatter" "- note-taking" \
         vault/loops/L0001.md
if grep -rq "not-a-real-tag" vault/loops/ 2>/dev/null; then
  bad "out-of-vocabulary tag reached a loop page"
else
  ok "out-of-vocabulary tag never reached a loop page"
fi
contains "invalid tag was dropped with a logged reason" \
         "not in the frozen vocabulary — dropped" logs/week.log

step "Eighth night (completes L0003's second occurrence)"
DREAMER_TODAY="2026-08-09" ./bin/nightly-extract.sh >>logs/week.log 2>&1
L3_COUNT=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='L0003'][0].recurrence_count)")
check "L0003 accreted two occurrences" "2" "$L3_COUNT"

step "Living thread (U8): folds accrue on the matched loop"
contains "thread section exists on the accreting loop" \
         "## Thread (derived — hypothesis, not evidence)" vault/loops/L0001.md
contains "trajectory line for the second occurrence" "- 2026-08-05 — " \
         vault/loops/L0001.md
contains "trajectory line for the third occurrence" "- 2026-08-07 — " \
         vault/loops/L0001.md
contains "now claims carry the via-thread derived marker" " via thread)" \
         vault/loops/L0001.md
absent "fold did not copy transcript prose verbatim (rule 10)" \
       "hand-editable" vault/loops/L0001.md
FOLD_Q=$(python3 -c "
import json,sys
try: print(len(json.load(open('vault/.vault-meta/fold-pending.json'))))
except FileNotFoundError: print(0)")
check "fold queue fully drained after the nightly runs" "0" "$FOLD_Q"

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
# U9 repair: the decision-only transition used to rebuild the body from
# default_body, wiping the Thread and Theme sections (observed live: L0003).
contains "decision-only transition preserved the living thread" \
         "## Thread (derived — hypothesis, not evidence)" vault/loops/L0003.md

# U9 dream integration: the prompt surfaces the thread AFTER the primary
# occurrences, framed as rule-13 derived content — and the researching dream
# returned a rebuilt Now that apply_conclusion folded back (trajectory intact).
DREAM_PROMPT_ORDER=$(python3 - logs/.prompt-dream-L0001.md <<'PYEOF'
import sys
t = open(sys.argv[1]).read()
i = t.find("### Occurrences")
j = t.find("### What Dreamer currently holds (derived — re-test, do not trust)")
print("YES" if 0 <= i < j else "NO")
PYEOF
)
check "dream prompt carries the derived thread block after the occurrences" \
      "YES" "$DREAM_PROMPT_ORDER"
contains "derived block is framed as a rule-13 hypothesis" \
         "never citable as evidence" logs/.prompt-dream-L0001.md
contains "dream's rebuilt Now landed on the researched loop's thread" \
         "Refreshed after research" vault/loops/L0001.md
contains "thread trajectory survived the Now rebuild" "- 2026-08-05 — " \
         vault/loops/L0001.md
# Baseline for the rule-2 zero-diff assertion below: L0003 is terminal now
# and nothing in the next two nights' input concerns it.
L3_MD5_BEFORE=$(md5sum vault/loops/L0003.md | cut -d' ' -f1)

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
contains "reopened loop's new occurrence was folded into its thread" \
         "- 2026-08-10 — " vault/loops/L0001.md

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

step "Living thread: unrelated nights leave a terminal loop byte-identical"
# Two nights ran since the dream (a reopening for L0001, a resurfacing for
# L0002). Neither concerned L0003, so rule 2 demands a zero diff — the fold
# machinery must not have touched it, and a resurfacing must not have queued
# a fold anywhere.
L3_MD5_AFTER=$(md5sum vault/loops/L0003.md | cut -d' ' -f1)
check "terminal loop byte-identical across two unrelated nights" \
      "$L3_MD5_BEFORE" "$L3_MD5_AFTER"
FOLD_Q2=$(python3 -c "
import json,sys
try: print(len(json.load(open('vault/.vault-meta/fold-pending.json'))))
except FileNotFoundError: print(0)")
check "resurfacing night queued no folds (resurfacings never fold)" "0" "$FOLD_Q2"

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

step "Quiet night keeps the index-fresh rescue window sliding"
# Age last_reindex far past health.index_stale_hours, then run a night with
# nothing to extract. The no-op branch must still reindex — otherwise a quiet
# stretch lets the rescue record expire, index-fresh blocks research on the
# static wisdom collection, and the block sustains itself.
python3 - <<'PYEOF'
import json
p = 'vault/.vault-meta/run-state.json'
try:
    d = json.load(open(p))
except FileNotFoundError:
    d = {}
d.setdefault('last_reindex', {})['at'] = '2020-01-01T00:00:00'
d['last_reindex'].setdefault('collections',
                             ['vault', 'transcripts', 'conclusions', 'wisdom'])
json.dump(d, open(p, 'w'), indent=2)
PYEOF
DREAMER_TODAY="2026-08-13" ./bin/nightly-extract.sh >>logs/week.log 2>&1
contains "night 2026-08-13 was a no-op (nothing left to extract)" \
         "nightly: no-op 2026-08-13" <(git log --oneline -5)
REINDEX_AGE=$(python3 - <<'PYEOF'
import datetime, json
d = json.load(open('vault/.vault-meta/run-state.json'))
at = datetime.datetime.fromisoformat(d['last_reindex']['at'])
print(int((datetime.datetime.now() - at).total_seconds()))
PYEOF
)
if [[ "$REINDEX_AGE" -lt 300 ]]; then
  ok "no-op night refreshed last_reindex (rescue window slides on quiet nights)"
else
  bad "no-op night left last_reindex stale (age ${REINDEX_AGE}s) — a quiet week freezes the rescue window"
fi

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

step "Claude Code session ingestion (ingest-cc)"
python3 - "$WORK-cc-projects" <<'PYEOF'
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1]) / "-sandbox-demo"
root.mkdir(parents=True, exist_ok=True)
old = time.time() - 48 * 3600


def write(sid, entrypoint, turns):
    recs = [{"type": "ai-title", "aiTitle": "Ranking loops by recurrence"}]
    for role, text in turns:
        recs.append({"type": role, "isSidechain": False, "isMeta": False,
                     "sessionId": sid, "entrypoint": entrypoint,
                     "cwd": "/sandbox/demo", "timestamp": "2026-08-11T09:00:00.000Z",
                     "message": {"role": "user" if role == "user" else "assistant",
                                 "content": text if role == "user"
                                 else [{"type": "text", "text": text}]}})
    f = root / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    os.utime(f, (old, old))


# A real interactive session: three substantial owner turns. The pasted block
# and the key must not survive into the page.
body = "\n".join(f"row_{i} = compute({i})" for i in range(20))
write("sess-real", "cli", [
    ("user", "I keep coming back to whether recurrence should be recency "
             "weighted or a flat count. " + "Thinking out loud here. " * 30),
    ("assistant", "Two options, weighted or flat."),
    ("user", f"Here is what I tried:\n```python\n{body}\n```\n"
             "and it still ranks the stale ones first. " + "More detail. " * 40),
    ("assistant", "The half-life is doing the work."),
    ("user", "Right, but I have not settled it. " + "Still unsure. " * 40),
])

# A headless run. Same shape, same volume — only the entrypoint differs.
write("sess-headless", "sdk-cli", [
    ("user", "Extract loops from tonight's batch. " + "Instructions. " * 60),
    ("assistant", "Done."),
    ("user", "Continue. " * 60),
    ("assistant", "Done."),
    ("user", "Continue again. " * 60),
])
PYEOF

BEFORE_Q=$(python3 scripts/make_batch.py --count-remaining \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["remaining"])')
DREAMER_TODAY="2026-08-13" ./bin/ingest-cc.sh >>logs/week.log 2>&1

CC_PAGES=$(grep -rl "source_agent: claude-code" vault/sources/transcripts 2>/dev/null | wc -l)
check "one page per interactive session, headless one ignored" "1" "$CC_PAGES"

CC_PAGE=$(grep -rl "source_agent: claude-code" vault/sources/transcripts | head -1)
contains "page carries the derived abstract" "## Session abstract (derived)" "$CC_PAGE"
contains "page declares its turns reconstructed" "## Human (reconstructed)" "$CC_PAGE"
absent   "pasted code did not reach the page" 'row_0 = compute(0)' "$CC_PAGE"
absent   "no fenced code block on the page" '```' "$CC_PAGE"

REASON=$(python3 -c "
import json;d=json.load(open('vault/.vault-meta/cc-ingested.json'))
print(d['sess-headless']['status'], '|', 'entrypoint' in d['sess-headless']['reason'])")
check "headless session rejected with a recorded reason" "rejected | True" "$REASON"

AFTER_Q=$(python3 scripts/make_batch.py --count-remaining \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["remaining"])')
check "the new page entered the extraction queue" "$((BEFORE_Q + 1))" "$AFTER_Q"

DREAMER_TODAY="2026-08-13" ./bin/ingest-cc.sh >>logs/week.log 2>&1
CC_PAGES2=$(grep -rl "source_agent: claude-code" vault/sources/transcripts | wc -l)
check "a second sweep is a no-op, not a second page" "1" "$CC_PAGES2"

DIRTY=$(git status --porcelain | wc -l)
check "ingest-cc left the tree clean (it commits its own work)" "0" "$DIRTY"

step "Health gate: unhealthy index defers research honestly, then recovers"
# The only research-eligible loop left is $MAL_ID (open, recurrence 2, no
# conclusion). Break the index: the stub reports every collection long-stale
# and fails update, so the pre-gate reindex writes empty coverage and
# index-fresh blocks the research leg.
touch "$WORK/stubbin/qmd-stale"
CONC_HG=$(ls vault/conclusions/*.md 2>/dev/null | wc -l)
DREAMER_TODAY="2026-08-16" ./bin/weekly-dream.sh >>logs/week.log 2>&1
check "blocked weekly-dream exits cleanly" "0" "$?"
HG_DIGEST=$(ls vault/digests/2026-*.md | tail -1)
contains "digest still produced, naming the deferral" "Research deferred" "$HG_DIGEST"
MAL_HG=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='$MAL_ID'][0].status)")
check "eligible loop untouched while blocked" "open" "$MAL_HG"
check "no conclusion written while blocked" "$CONC_HG" \
      "$(ls vault/conclusions/*.md 2>/dev/null | wc -l)"
SUBJECTS=$(git log --format=%s)
grep -qF "research blocked (healthcheck)" <<<"$SUBJECTS" \
  && ok "block committed honestly" \
  || bad "no 'research blocked (healthcheck)' commit"
# The index recovers. weekly-dream reindexes and re-checks health BEFORE its
# gate, so the very next run unblocks itself — no other job's schedule needed.
rm -f "$WORK/stubbin/qmd-stale"
DREAMER_TODAY="2026-08-17" ./bin/weekly-dream.sh >>logs/week.log 2>&1
MAL_HG2=$(python3 -c "
import sys;sys.path.insert(0,'scripts')
import vault as V;print([l for l in V.load_loops() if l.id=='$MAL_ID'][0].status)")
check "research ran once health cleared" "paused" "$MAL_HG2"
SUBJECTS=$(git log --format=%s)
grep -qF "$MAL_ID researched" <<<"$SUBJECTS" \
  && ok "recovered run researched the parked loop" \
  || bad "no '$MAL_ID researched' commit after recovery"

step "Failing fold is bounded, ordered, and honest"
# Two fixture loops with REAL transcripts. F queues two occurrences
# (oldest-first), G queues one. On the fail night the fold responder violates
# the JSON contract for F's folds only (DREAMER_FAKE_FOLD_FAIL): the failed
# entry must stay queued with its attempt counted, F's LATER occurrence must
# not fold in that run (drain-side failed-loop set), G must still fold, and
# the refusal must surface once with its reply preserved per-loop.
FG_IDS=$(python3 - <<'PY'
import sys, datetime
from pathlib import Path
sys.path.insert(0, 'scripts')
import vault as V


def transcript(date, slug):
    rel = (Path('vault/sources/transcripts') / date[:4] / date[5:7]
           / f'{date}--{slug}.md')
    rel.parent.mkdir(parents=True, exist_ok=True)
    rel.write_text(f'---\ntype: transcript\ndate: {date}\n---\n\n'
                   f'# {slug}\n\n## Human\n\nStill circling the question.\n',
                   encoding='utf-8')
    return f"[[{str(rel.relative_to('vault')).removesuffix('.md')}]]"


o1 = transcript('2026-08-13', 'foldfail-a')
o2 = transcript('2026-08-14', 'foldfail-b')
og = transcript('2026-08-14', 'foldok-a')
f = V.create_loop('Fold failure fixture loop', o1, datetime.date(2026, 8, 13))
V.add_occurrence(f, o2, datetime.date(2026, 8, 14))
g = V.create_loop('Fold success fixture loop', og, datetime.date(2026, 8, 14))
print(f.id, g.id)
PY
)
read -r F_ID G_ID <<<"$FG_IDS"
DREAMER_TODAY="2026-08-18" DREAMER_FAKE_FOLD_FAIL="$F_ID" \
  ./bin/nightly-extract.sh >>logs/week.log 2>&1
ATT=$(python3 -c "
import json
q = json.load(open('vault/.vault-meta/fold-pending.json'))
e = [x for x in q if x['loop_id'] == '$F_ID']
print(len(e), e[0].get('attempts', 0), e[1].get('attempts', 0))")
check "failed entry stays queued with one attempt, later one unattempted" \
      "2 1 0" "$ATT"
absent "failing loop got no fold that run (same-loop ordering)" \
       "## Thread (derived" "vault/loops/$F_ID.md"
contains "a different loop's fold still succeeded" "- 2026-08-14 — " \
         "vault/loops/$G_ID.md"
EVN=$(python3 -c "
import json
d = json.load(open('vault/.vault-meta/run-state.json'))
print(sum(1 for e in d.get('events', [])
          if 'apply refused the fold for $F_ID' in e.get('detail', '')))")
check "refusal surfaced as exactly one degraded event" "1" "$EVN"
[[ -f "logs/.result-fold-refused-$F_ID.json" ]] \
  && ok "refused reply preserved at its per-loop path" \
  || bad "logs/.result-fold-refused-$F_ID.json missing"
# The responder recovers: the entry folds on the next night and the
# trajectory lands in chronological order.
DREAMER_TODAY="2026-08-19" ./bin/nightly-extract.sh >>logs/week.log 2>&1
TRAJ=$(python3 - <<PY
import sys, re
sys.path.insert(0, 'scripts')
import vault as V
sec = V.thread_section(V.load_loop('$F_ID').body) or ''
dates = re.findall(r'^- (\d{4}-\d{2}-\d{2}) ', sec, re.M)
print(len(dates), 'sorted' if dates == sorted(dates) else 'unsorted')
PY
)
check "both folds landed after recovery, in chronological order" \
      "2 sorted" "$TRAJ"
FQ=$(python3 -c "
import json
q = json.load(open('vault/.vault-meta/fold-pending.json'))
print(sum(1 for x in q if x['loop_id'] == '$F_ID'))")
check "failing loop's queue drained after recovery" "0" "$FQ"

# --- oversized prompt delivery (execve E2BIG) -----------------------------
# Linux caps a SINGLE argv element at MAX_ARG_STRLEN (32 pages = 128 KB), so
# `claude -p "$(cat prompt)"` fails execve with E2BIG on a large prompt: bash
# reports 126 and run_claude recorded that as a usage-limit deferral — an
# honest-looking log line for a bug that recurs every run. The real CLI is
# not needed to pin this: the stub records what it received, and the
# pre-fix code never reaches the stub at all.
step "Oversized prompt reaches the CLI on stdin, not argv"
BIGP="$WORK/logs/.prompt-e2big.md"
python3 -c "open('$BIGP', 'w').write('x' * 200000)"
cat > "$WORK/bin/stub-claude" <<'EOS'
#!/usr/bin/env bash
# Stands in for the CLI: keeps whatever arrived on stdin, then prints the
# --output-format json envelope run_claude parses.
cat > "$STUB_SEEN"
printf '{"result": "{}", "total_cost_usd": 0.01}\n'
EOS
chmod +x "$WORK/bin/stub-claude"
# DREAMER_FAKE_CLAUDE is exported for the rest of this file and short-circuits
# run_claude before it ever execs a CLI — the exact path under test. Blanking
# it (the wrapper tests for non-empty) sends this one call down the real
# invocation path, into the stub below.
BIGRC=$(cd "$WORK" && JOB=e2big CLAUDE_BIN="$WORK/bin/stub-claude" \
        DREAMER_FAKE_CLAUDE= STUB_SEEN="$WORK/logs/.e2big-seen.txt" \
        bash -c 'source bin/_common.sh
                 run_claude "'"$BIGP"'" 4 "'"$WORK"'/logs/.result-e2big.json" \
                            restricted
                 echo "rc=$?"' </dev/null 2>/dev/null | tail -1)
check "run_claude survives a 200 KB prompt (no E2BIG deferral)" "rc=0" "$BIGRC"
check "the whole prompt reached the CLI" "200000" \
      "$(wc -c < "$WORK/logs/.e2big-seen.txt" 2>/dev/null || echo 0)"
BIGDEF=$(python3 -c "
import json
d = json.load(open('$WORK/vault/.vault-meta/run-state.json'))
print(sum(1 for x in d.get('deferrals', []) if x.get('job') == 'e2big'))")
# --- The Worried Friend (Goals, Assess, MCP Writes, Health & Digest) ------
step "The Worried Friend — Goal Lifecycle & Nightly Assessment"

python3 - <<PY
import datetime as dt, sys
sys.path.insert(0, '$WORK/scripts')
import goals as G
import questions as Q

g = G.commit_goal(
    title="Chess Ladder",
    finish_line="Puzzle ladder shows 1600",
    target_date=dt.date(2026, 12, 1),
    why="Club team selection",
    plan_steps=["- [ ] Complete Puzzle drill set 1", "- [ ] Complete Endgame drill set 1"],
    owner_quote="I want to commit to the chess puzzle ladder",
    session_id="sess-001",
    ref=dt.date(2026, 8, 1),
)
Q.record_owner_write(
    tool="commit_goal",
    target_type="goal",
    target_id=g.id,
    owner_quote="I want to commit to the chess puzzle ladder",
    session_id="sess-001",
    params={"title": "Chess Ladder"},
    confirmation="Committed goal G0001: Chess Ladder",
    at=dt.datetime(2026, 8, 1, 10, 0, 0),
)
PY

check "goal G0001 file created" "true" "$([ -f "$WORK/vault/goals/G0001.md" ] && echo true || echo false)"
check "goals catalog created" "true" "$([ -f "$WORK/vault/goals/_catalog.md" ] && echo true || echo false)"

# Create a transcript with human turn mention on Day 1
mkdir -p "$WORK/vault/sources/transcripts/2026/08"
cat > "$WORK/vault/sources/transcripts/2026/08/2026-08-03--chess-mention.md" <<'TR1'
---
type: transcript
title: "Chess Discussion"
---

Human: I want to commit to the chess puzzle ladder. Also I was thinking about the tournament date.

Assistant: We have your goal committed.
TR1

(cd "$WORK" && DREAMER_TODAY=2026-08-03 bash bin/nightly-assess.sh)
G_MENTION=$(python3 -c "import sys; sys.path.insert(0, '$WORK/scripts'); import goals as G; g = G.load_goal('G0001'); print(g.last_mention.isoformat(), g.last_movement.isoformat())")
check "nightly-assess updated mention clock while movement remained unchanged" "2026-08-03 2026-08-01" "$G_MENTION"

# Create a transcript with movement on Day 2
cat > "$WORK/vault/sources/transcripts/2026/08/2026-08-04--chess-movement.md" <<'TR2'
---
type: transcript
title: "Chess Practice"
---

Human: I completed 40 puzzles today from drill set 1. Mark step 1 done from today session.

Assistant: Great job! Step completed.
TR2

(cd "$WORK" && DREAMER_TODAY=2026-08-04 bash bin/nightly-assess.sh)
G_MOVE=$(python3 -c "import sys; sys.path.insert(0, '$WORK/scripts'); import goals as G; g = G.load_goal('G0001'); print(g.last_movement.isoformat())")
check "nightly-assess updated movement clock" "2026-08-04" "$G_MOVE"

# Complete step 1 with citation
python3 - <<PY
import datetime as dt, sys
sys.path.insert(0, '$WORK/scripts')
import goals as G
import questions as Q

G.complete_step(
    "G0001",
    1,
    citation="[[sources/transcripts/2026/08/2026-08-04--chess-movement]]",
    owner_quote="Mark step 1 done from today session",
    session_id="sess-002",
    ref=dt.date(2026, 8, 4),
)
Q.record_owner_write(
    tool="complete_step",
    target_type="goal",
    target_id="G0001",
    owner_quote="Mark step 1 done from today session",
    session_id="sess-002",
    params={"step_num": 1},
    confirmation="Step 1 completed",
    at=dt.datetime(2026, 8, 4, 12, 0, 0),
)
PY

STEP_DONE=$(python3 -c "import sys; sys.path.insert(0, '$WORK/scripts'); import goals as G; g = G.load_goal('G0001'); print('[x]' in g.body)")
check "step 1 completed in goal body" "True" "$STEP_DONE"

# Run healthcheck
(cd "$WORK" && python3 scripts/healthcheck.py --json > "$WORK/logs/health-friend.json")
GOAL_HEALTH=$(python3 -c "import json; d = json.load(open('$WORK/logs/health-friend.json')); goal_ass = [a for a in d['assertions'] if 'goal' in a['name'] or 'step' in a['name'] or 'owner-writes' in a['name']]; print(all(a['ok'] for a in goal_ass))")
check "all goal and owner-write healthcheck assertions pass" "True" "$GOAL_HEALTH"

# Build weekly digest and check The Friend section
DIGEST_FILE=$(cd "$WORK" && python3 scripts/digest.py build)
contains "digest contains The Friend section" "The Friend" "$DIGEST_FILE"
contains "digest contains Movement subsection" "### Movement" "$DIGEST_FILE"
contains "digest contains Convergence subsection" "### Convergence" "$DIGEST_FILE"
contains "digest contains Housekeeping subsection" "### Housekeeping" "$DIGEST_FILE"
contains "digest contains Owner-writes spot-check" "Owner-writes spot-check" "$DIGEST_FILE"

step "Friend: a quiet night, a forged quote, and speaking first (spec §9.4 items 3, 4, 7, 8)"
GOAL_PAGE="$WORK/vault/goals/G0001.md"
contains "an assessed transcript is checkpointed" "2026-08-04--chess-movement" "$WORK/vault/.vault-meta/assessed.json"

# Nights with unrelated input (the rest of the sandbox's transcripts, drained a
# batch at a time), then a night with no input at all. None may touch the page.
BEFORE=$(sha256sum "$GOAL_PAGE" | cut -d' ' -f1)
for _ in 1 2 3 4 5 6 7 8; do
  (cd "$WORK" && DREAMER_TODAY=2026-08-05 bash bin/nightly-assess.sh) > "$WORK/logs/assess-quiet.out" 2>&1
  grep -qF "detectors only, no LLM call" "$WORK/logs/assess-quiet.out" && break
done
check "a night with nothing leaves the goal page byte-identical" "$BEFORE" "$(sha256sum "$GOAL_PAGE" | cut -d' ' -f1)"
contains "a quiet night makes no LLM call" "detectors only, no LLM call" "$WORK/logs/assess-quiet.out"

(cd "$WORK" && python3 - <<'PY'
import datetime as dt, sys
sys.path.insert(0, "scripts")
import questions as Q
Q.record_owner_write(
    tool="set_status", target_type="goal", target_id="G0001",
    owner_quote="please abandon the Chess goal entirely", session_id="sess-forged",
    params={"status": "abandoned"}, confirmation="G0001 abandoned",
    at=dt.datetime(2026, 7, 30, 12, 0, 0),
)
PY
)
(cd "$WORK" && DREAMER_TODAY=2026-08-06 bash bin/nightly-assess.sh)
FORGED=$(cd "$WORK" && python3 -c "
import sys; sys.path.insert(0, 'scripts'); import questions as Q
w = [w for w in Q.load_owner_writes() if w['session_id'] == 'sess-forged'][0]
q = [q for q in Q.load_questions() if q['kind'] == 'unverified-write']
print(w['verified'], len(q), q[0]['id'] == w.get('question_id'))")
check "a forged quote is quarantined and lands in the question queue" "False 1 True" "$FORGED"

# Ten days after the last mention (2026-08-04) the silence ping is due.
(cd "$WORK" && DREAMER_TODAY=2026-08-14 bash bin/nightly-assess.sh)
BRIEF1=$(cd "$WORK" && DREAMER_TODAY=2026-08-14 CLAUDE_CODE_ENTRYPOINT=cli python3 scripts/session_brief.py)
BRIEF2=$(cd "$WORK" && DREAMER_TODAY=2026-08-14 CLAUDE_CODE_ENTRYPOINT=cli python3 scripts/session_brief.py)
HEADLESS=$(cd "$WORK" && DREAMER_TODAY=2026-08-14 CLAUDE_CODE_ENTRYPOINT=sdk-cli python3 scripts/session_brief.py)
case "$BRIEF1" in *"still on this?"*) ok "session start surfaces the silence ping" ;; *) bad "session start surfaces the silence ping — got: $BRIEF1" ;; esac
check "the ping is not re-asked at the next session" "" "$BRIEF2"
check "a headless session is told nothing" "" "$HEADLESS"

# Well past the escalation window: questions accumulate, the goal does not move.
(cd "$WORK" && DREAMER_TODAY=2026-09-20 bash bin/nightly-assess.sh)
G_STATUS=$(cd "$WORK" && python3 -c "import sys; sys.path.insert(0, 'scripts'); import goals as G; print(G.load_goal('G0001').status)")
check "weeks of silence never change the goal's status" "active" "$G_STATUS"

printf '\n\033[1m== Result ==\033[0m\n'
printf '  passed: %s\n  failed: %s\n  sandbox: %s\n' "$PASS" "$FAIL" "$WORK"
[[ "$FAIL" -eq 0 ]] || exit 1
