#!/usr/bin/env bash
# Shared job harness (§6.9). Sourced by every job wrapper.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT
PY="${DREAMER_PYTHON:-python3}"

# qmd ships via nvm, whose PATH setup only exists in an interactive login
# shell. cron and any headless run start without it, so `command -v qmd` fails
# and the wisdom route silently degrades to web-only. Prepend every plausible
# node bin dir before anything tries to reach qmd.
for _nodebin in "$HOME"/.nvm/versions/node/*/bin; do
  [ -x "$_nodebin/qmd" ] && case ":$PATH:" in
    *":$_nodebin:"*) ;;
    *) PATH="$_nodebin:$PATH" ;;
  esac
done
unset _nodebin
export PATH
LOGS="$ROOT/logs"
META="$ROOT/vault/.vault-meta"
mkdir -p "$LOGS" "$META"

JOB="${JOB:-dreamer}"
export JOB

# Logical date. DREAMER_TODAY makes the simulated week deterministic AND makes
# `git log` reconstruct it, which is the DoD 6.9 provenance claim.
TODAY() { printf '%s' "${DREAMER_TODAY:-$(date -I)}"; }

log() { printf '[%s] [%s] %s\n' "$(date -Is)" "$JOB" "$*"; }
die() { log "FATAL: $*"; exit 1; }

# --- advisory vault lock (wiki-lock) -------------------------------------
LOCKFILE="$META/wiki.lock"
acquire_lock() {
  exec 9>"$LOCKFILE"
  if ! flock -n 9; then
    log "vault is locked by another job — exiting cleanly (no work lost)"
    exit 0
  fi
  echo "$JOB:$$:$(date -Is)" >&9 || true
  # Tell child processes the lock is already held. flock is per
  # open-file-description, so a record_* python child re-flocking wiki.lock
  # would block against THIS shell's held fd 9 — with this set,
  # dreamer_common.state_lock skips its own flock; the parent's held lock
  # already serializes every write made under it.
  export DREAMER_LOCK_HELD=1
}

# --- health spine ----------------------------------------------------------
# Runs scripts/healthcheck.py OUTSIDE the vault lock (flock is per
# open-file-description, so a child process can never re-acquire the lock this
# shell already holds — call it BEFORE acquire_lock). Non-zero means the
# health record could not be written: degraded, logged, never fatal.
# run_healthcheck [detail] — `detail` replaces the default parenthetical in
# the log line, e.g. "before dream leg".
run_healthcheck() {
  local detail="${1:-(health record may be stale)}"
  "$PY" "$ROOT/scripts/healthcheck.py" \
    || log "healthcheck exited non-zero $detail — continuing"
}

# --- claude -p wrapper ----------------------------------------------------
# A usage-limit exit is a NORMAL outcome that defers work to the next run
# (Principle 9). It must be logged honestly and must not look like success.
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
# run_claude <prompt_file> <max_turns> <out_file> [restricted]
#
# The optional 4th argument selects the tool surface:
#   (absent)     — the research surface: qmd + web, acceptEdits. For jobs
#                  whose contract permits egress (CLAUDE.md rules 5/12).
#   restricted   — output-only: headless deny-by-default (no acceptEdits, no
#                  allowlist) PLUS an explicit disallow list covering shell,
#                  edit, web, the read/search tools and both MCP servers, and
#                  max-turns capped at 2. For jobs whose prompt inlines
#                  private vault content that must never leak into a query
#                  (rule 12) — tag-backfill and the thread-fold drain.
run_claude() {
  local prompt_file="$1" max_turns="$2" out_file="$3" tool_mode="${4:-full}"
  local raw="$out_file.raw.json"

  if [[ "${DREAMER_FAKE_CLAUDE:-}" != "" ]]; then
    # Deterministic substitute used by the simulated-week acceptance test.
    "$DREAMER_FAKE_CLAUDE" "$prompt_file" > "$out_file"
    local fake_code=$?
    # Cost accounting must be reachable here too, or the ceiling path is
    # structurally untestable: it would sit behind a branch the tests never take.
    if [[ -n "${DREAMER_FAKE_COST:-}" ]]; then
      echo "$DREAMER_FAKE_COST" > "$out_file.cost"
      record_cost "$out_file.cost"
    fi
    return $fake_code
  fi

  set +e
  # acceptEdits alone blocks Bash, which silently kills the qmd research leg —
  # found live 2026-08-02 when a dream conclusion reported "qmd blocked at the
  # permission layer" for every collection. Allow exactly what the jobs need:
  # qmd search plus the web tools the web/mixed routes are contractually
  # permitted (CLAUDE.md rules 5/12). Nothing broader: this process runs
  # unattended with vault write access.
  # Model comes from config (budget.model, default sonnet). Jobs previously
  # passed no --model and silently inherited whatever the CLI default was, so
  # the model doing unattended extraction could change under us without a
  # single line changing in this repo.
  local model
  model=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget'].get('model') or '')" 2>/dev/null || true)
  local -a perm_args
  if [[ "$tool_mode" == "restricted" ]]; then
    # Output-only. No --permission-mode acceptEdits (so no edit permission),
    # no --allowedTools (so nothing is pre-approved and headless denies by
    # default), PLUS an explicit disallow list so the contract still holds if
    # a future CLI default changes underneath us. Read/Glob/Grep/LS are
    # denied too: a restricted prompt inlines everything it may see, and a
    # read tool would let it browse private vault content the caller never
    # inlined. Both MCP servers (qmd, dreamer) are shut for the same reason.
    perm_args=(--disallowedTools "Bash" "Edit" "Write" "NotebookEdit" "Task"
               "WebSearch" "WebFetch" "Read" "Glob" "Grep" "LS"
               "mcp__qmd" "mcp__dreamer")
    # Restricted jobs are contractually single-shot: the input is inlined and
    # the reply is one JSON object. Tool attempts still happen (the model may
    # try a denied Read and needs a turn to recover from the rejection), and a
    # 2-turn cap turned that recoverable nudge into is_error/stop_reason=
    # tool_use (observed live, 2026-08-16). The security property is the deny
    # list, not the cap — keep a small recovery margin.
    if [[ "$max_turns" -gt 4 ]]; then max_turns=4; fi
  else
    perm_args=(--permission-mode acceptEdits
               --allowedTools "Bash(qmd:*)" "mcp__qmd" "WebSearch" "WebFetch")
  fi
  "$CLAUDE_BIN" -p "$(cat "$prompt_file")" \
      --output-format json \
      ${model:+--model "$model"} \
      --max-turns "$max_turns" \
      "${perm_args[@]}" \
      > "$raw" 2>>"$LOGS/$JOB.log"
  local code=$?
  set -e

  if [[ $code -ne 0 ]]; then
    log "claude exited $code — treating as usage-limit/deferral, work resumes next run"
    record_deferral "$code"
    record_event "deferral" "$JOB deferred (exit $code); the work was not lost and resumes on the next scheduled run"
    return $code
  fi

  # --output-format json wraps the reply; pull out .result and the cost.
  "$PY" - "$raw" "$out_file" <<'PYEOF'
import json,sys
raw,out=sys.argv[1],sys.argv[2]
try:
    d=json.load(open(raw))
except Exception:
    open(out,"w").write(open(raw).read()); sys.exit(0)
if isinstance(d,dict):
    txt=d.get("result") or d.get("content") or ""
    if isinstance(txt,list):
        txt="".join(b.get("text","") for b in txt if isinstance(b,dict))
    open(out,"w").write(txt if isinstance(txt,str) else json.dumps(txt))
    cost=d.get("total_cost_usd") or d.get("cost_usd")
    if cost is not None:
        open(sys.argv[1]+".cost","w").write(str(cost))
else:
    open(out,"w").write(json.dumps(d))
PYEOF
  record_cost "$raw.cost"
  return 0
}

# Run-level events surface in the next digest. Without this channel a deferred
# night or a recovered loop is written to run-state.json and silently forgotten,
# which is the failure the honest-deferral rule exists to prevent.
#
# WRITE SAFETY (all record_* helpers + clear_skip_research): every write is a
# read-merge-atomic-replace under wiki.lock via dreamer_common.update_run_state
# — an unlocked in-place json.dump raced healthcheck's locked writer (ingest-cc
# holds no job lock), and whichever wrote last silently dropped the other's
# record. Re-entrancy: jobs call these while HOLDING wiki.lock on fd 9, and
# flock is per open-file-description, so the child must not re-flock —
# acquire_lock exports DREAMER_LOCK_HELD=1 and state_lock skips its own flock
# under it. On lock contention (another process held it past the short retry)
# or unparseable state the write is refused loudly: WARN in the log, nothing
# wiped, job continues (`|| log` keeps set -e contexts alive).
record_event() {
  # Central dedup (dreamer_common.append_event_deduped): an identical
  # (job, kind, detail) event already pending is not appended again. The
  # digest that drains events is weekly while some callers fire per entry
  # per run (the fold drain's skip events were the live case), so an
  # undeduped repeat stacks identical lines into one digest — same contract
  # as healthcheck.write_health and the watchdog.
  "$PY" - "$META/run-state.json" "$1" "$2" <<'PYEOF' \
    || log "WARN record_event failed — event NOT recorded (see stderr above)"
import datetime, os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
from dreamer_common import append_event_deduped, update_run_state
path, kind, detail = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
event = {"at": datetime.datetime.now().isoformat(timespec="seconds"),
         "job": os.environ.get("JOB", "?"), "kind": kind, "detail": detail}
update_run_state(path, lambda d: append_event_deduped(d, event))
PYEOF
}

record_deferral() {
  "$PY" - "$META/run-state.json" "$1" <<'PYEOF' \
    || log "WARN record_deferral failed — deferral NOT recorded (see stderr above)"
import datetime, os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
from dreamer_common import update_run_state
path, code = Path(sys.argv[1]), sys.argv[2]
def mutate(d):
    d.setdefault("deferrals", []).append(
        {"at": datetime.datetime.now().isoformat(timespec="seconds"),
         "exit_code": int(code), "job": os.environ.get("JOB", "?")})
update_run_state(path, mutate)
PYEOF
}

record_cost() {
  [[ -f "$1" ]] || return 0
  "$PY" - "$META/run-state.json" "$1" "$ROOT/config.yaml" <<'PYEOF' \
    || log "WARN record_cost failed — cost NOT recorded (see stderr above)"
import datetime, os, sys
from pathlib import Path
import yaml
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
from dreamer_common import update_run_state
state, costfile, cfgfile = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
cost = float(open(costfile).read().strip() or 0)
cfg = yaml.safe_load(open(cfgfile))
raw_ceiling = cfg["budget"].get("cost_ceiling_per_run")
ceiling = None if raw_ceiling in (None, "", "null", "unlimited") else float(raw_ceiling)
def mutate(d):
    d.setdefault("costs", []).append(
        {"at": datetime.datetime.now().isoformat(timespec="seconds"),
         "job": os.environ.get("JOB", "?"), "cost": round(cost, 4)})
# Cost is ALWAYS recorded — the digest's run stats and any spend question you
# ever ask depend on it. Only the gate is optional.
#
# Default is unlimited (ceiling is None). Under a subscription, `total_cost_usd`
# is API-equivalent notional pricing, not money: the subscription is already
# paid, embedding and reranking run locally for free, and the real limiting
# resource is the rolling usage window — which run_claude already handles by
# treating a non-zero exit as a clean, resumable deferral. Gating research on a
# notional dollar figure meant an expected, finite backfill cost silently
# disabled the weekly dream (observed: batches at $6-26 against a $5 ceiling
# would have muted research for the entire backfill window, defeating G2).
#
# Set a number here to restore the gate — meaningful if jobs ever move to API
# keys (spec P2), where the figure becomes real money.
    if ceiling is not None:
        # There is no mid-run abort under `claude -p` (cost is reported after
        # the run), so exceeding the ceiling makes the NEXT run skip research
        # instead. LATCH, do not assign. weekly-dream calls run_claude once per
        # loop, so a plain assignment lets the last cheap loop clear a breach
        # the expensive ones set. Observed live: 4 breaches, final state False.
        # Cleared only by clear_skip_research() once the skip has actually been
        # honored.
        d["skip_research_next_run"] = bool(d.get("skip_research_next_run")) or (cost > ceiling)
        if cost > ceiling:
            print(f"[cost] run cost {cost} exceeded ceiling {ceiling}: next run will skip research")
update_run_state(state, mutate)
PYEOF
}

should_skip_research() {
  "$PY" - "$META/run-state.json" <<'PYEOF'
import json,os,sys
p=sys.argv[1]
d=json.load(open(p)) if os.path.exists(p) else {}
sys.exit(0 if d.get("skip_research_next_run") else 1)
PYEOF
}

# True (exit 0) when scripts/healthcheck.py has blocked the named leg.
# Jobs consult this next to should_skip_research and defer cleanly — a blocked
# leg is a health verdict, not a crash.
leg_blocked() {
  "$PY" - "$META/run-state.json" "$1" <<'PYEOF'
import json,os,sys
p,leg=sys.argv[1],sys.argv[2]
d=json.load(open(p)) if os.path.exists(p) else {}
sys.exit(0 if leg in ((d.get("health") or {}).get("blocked_legs") or []) else 1)
PYEOF
}

clear_skip_research() {
  "$PY" - "$META/run-state.json" <<'PYEOF' \
    || log "WARN clear_skip_research failed — flag NOT cleared (see stderr above)"
import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
from dreamer_common import update_run_state
def mutate(d):
    d["skip_research_next_run"] = False
update_run_state(Path(sys.argv[1]), mutate)
PYEOF
}

# --- living thread: fold-pending drain (CLAUDE.md rule 15) ----------------
# One RESTRICTED (output-only) LLM fold per queued {loop, occurrence}.
# scripts/fold_pending.py selects the batch without consuming it and
# scripts/apply_thread.py removes an entry ONLY after a successful page
# write, so a deferral or the fold_per_run cap leaves the remainder queued
# for the next run (rule 9). Never fatal: a broken fold degrades the thread,
# not the night.
# drain_fold_pending [limit] — the optional limit overrides config
# thread.fold_per_run; thread-backfill passes thread.backfill_per_run so the
# one-time drain gets its own cap without touching the nightly one.
drain_fold_pending() {
  local limit="${1:-}" maxt
  if [[ -z "$limit" ]]; then
    limit=$("$PY" -c "import yaml;print((yaml.safe_load(open('$ROOT/config.yaml')).get('thread') or {}).get('fold_per_run',20))" 2>/dev/null || echo 20)
  fi
  maxt=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget'].get('max_turns_thread_fold',8))" 2>/dev/null || echo 8)
  local batch="$LOGS/.fold-batch-$JOB.json"
  if ! "$PY" "$ROOT/scripts/fold_pending.py" batch --limit "$limit" --out "$batch" 2>>"$LOGS/$JOB.log"; then
    log "WARN fold-pending batch selection failed — folds skipped this run"
    record_event "degraded" "thread-fold: queue read failed; folds skipped this run"
    return 0
  fi
  # Guard verdicts surface on the digest's event channel: an unsorted
  # occurrence list or a dead link is a page defect, not a quiet no-op.
  # record_event dedups identical (job, kind, detail) pending events
  # centrally, so a skip that repeats every drain lands in the digest once.
  while IFS=$'\t' read -r skip_reason; do
    [[ -n "$skip_reason" ]] && record_event "degraded" "thread-fold: entry skipped — $skip_reason"
  done < <("$PY" -c "import json,sys
for s in json.load(open(sys.argv[1])).get('skipped',[]): print(s['reason'])" "$batch")
  local n
  n=$("$PY" -c "import json,sys;print(len(json.load(open(sys.argv[1]))['ready']))" "$batch")
  if [[ "$n" -eq 0 ]]; then rm -f "$batch"; return 0; fi
  log "thread-fold: $n fold(s) to apply (cap $limit)"
  # One parse of the batch, one line per ready entry, in queue order. Fields
  # are joined on the unit separator (0x1f): loop ids, wikilinks and dates
  # can never contain it, and the transcript field is a repo file path — but
  # a path could in principle hold a tab, so a tab delimiter is not safe.
  # fd 8, not stdin: the loop body runs claude and python, and a child that
  # reads stdin would otherwise swallow the remaining entries. (fd 9 is the
  # vault lock.)
  # Per-run failed-loop set (mirrors fold_pending.batch()'s blocked-set): a
  # loop whose entry failed this run must not have a LATER occurrence folded
  # in the same run — that would write the trajectory out of chronology.
  # Skipped entries stay queued, no attempt increment beyond the failed one;
  # the one degraded event per newly-failed loop dedups centrally on repeats.
  local -A FAILED_LOOPS=()
  local F_LOOP F_OCC F_DATE F_PATH verdict
  while IFS=$'\x1f' read -r -u 8 F_LOOP F_OCC F_DATE F_PATH; do
    if [[ -n "${FAILED_LOOPS[$F_LOOP]:-}" ]]; then
      log "thread-fold: skipping later $F_LOOP entry this run (an earlier one failed — folds apply oldest-first)"
      continue
    fi
    # Count the try BEFORE spending it (FIX: unbounded retries). Past
    # thread.fold_max_attempts fold_pending.py quarantines the entry itself
    # (event emitted there) and this run must not fold past the hole.
    verdict=$("$PY" "$ROOT/scripts/fold_pending.py" attempt --loop "$F_LOOP" \
              --occurrence "$F_OCC" 2>>"$LOGS/$JOB.log") \
      || { verdict='{}'
           log "WARN attempt stamp failed for $F_LOOP (see log) — folding anyway, retry cap not advanced"; }
    if [[ "$verdict" == *'"quarantined": true'* ]]; then
      FAILED_LOOPS[$F_LOOP]=1
      log "thread-fold: $F_OCC quarantined (attempt cap) — later $F_LOOP entries stay queued"
      continue
    fi
    if ! "$PY" "$ROOT/scripts/fold_pending.py" prompt --loop "$F_LOOP" \
          --occurrence "$F_OCC" --transcript "$F_PATH" \
          --out "$LOGS/.prompt-fold.md" 2>>"$LOGS/$JOB.log"; then
      record_event "degraded" "thread-fold: prompt build failed for $F_LOOP — entry left queued"
      FAILED_LOOPS[$F_LOOP]=1
      continue
    fi
    if ! run_claude "$LOGS/.prompt-fold.md" "$maxt" "$LOGS/.result-fold.json" restricted; then
      log "thread-fold deferred at $F_LOOP — remaining folds stay queued"
      break
    fi
    if "$PY" "$ROOT/scripts/apply_thread.py" --loop "$F_LOOP" \
          --occurrence "$F_OCC" --date "$F_DATE" \
          --input "$LOGS/.result-fold.json" >>"$LOGS/$JOB.log" 2>&1; then
      log "thread-fold: folded $F_OCC into $F_LOOP"
    else
      # Per-loop artifact (mirrors ingest-cc's per-session refusal keep): a
      # refusal is a claim about the reply, and a later fold overwriting the
      # shared scratch file would make that claim unfalsifiable.
      mv -f "$LOGS/.result-fold.json" \
            "$LOGS/.result-fold-refused-$F_LOOP.json" 2>/dev/null || true
      record_event "degraded" "thread-fold: apply refused the fold for $F_LOOP (entry left queued; reply kept at logs/.result-fold-refused-$F_LOOP.json)"
      FAILED_LOOPS[$F_LOOP]=1
    fi
  done 8< <("$PY" -c "import json,sys
for e in json.load(open(sys.argv[1]))['ready']:
    print(e['loop_id'], e['occurrence'], e['date'], e['transcript'], sep=chr(0x1f))" "$batch")
  rm -f "$batch"
  return 0
}

# --- finalisation ---------------------------------------------------------
reindex() {
  if ! command -v qmd >/dev/null 2>&1; then
    # Do NOT fail silently. qmd missing from PATH is exactly how the wisdom
    # leg went dark: every conclusion written on 2026-08-01 cited zero books
    # because the research step could not reach the corpus, and nothing said so.
    log "ERROR: qmd not on PATH — index is STALE and the wisdom route is DEAD."
    log "       PATH=$PATH"
    record_event "degraded" "qmd unreachable: index not refreshed and the wisdom route is unavailable — conclusions this run are web-only"
    return 1
  fi
  # The collection list comes from config (qmd.collections) — the same list
  # the healthcheck's collections-covered assertion holds this function to.
  # Hardcoding it here is how `wisdom` silently fell out of the reindex.
  local collections
  collections=$("$PY" -c "import yaml;print(' '.join(yaml.safe_load(open('$ROOT/config.yaml')).get('qmd',{}).get('collections') or []))" 2>/dev/null || true)
  if [[ -z "$collections" ]]; then
    log "ERROR: config.yaml qmd.collections is missing or empty — nothing to reindex"
    record_event "degraded" "reindex: config.yaml qmd.collections missing or empty — index not refreshed"
    return 1
  fi
  local rc=0 c
  local -a updated=()
  for c in $collections; do
    if (cd "$ROOT" && qmd update -c "$c" 2>&1); then
      log "qmd re-index ok: $c"
      updated+=("$c")
      # Embed inline so vectors track the index they serve. Embed failure is
      # degraded, never fatal: lexical search stays fresh even when embedding
      # fails, and the remaining collections must still be processed.
      if (cd "$ROOT" && qmd embed -c "$c" 2>&1); then
        log "qmd embed ok: $c"
      else
        log "ERROR: qmd embed -c $c FAILED (see log)"
        record_event "degraded" "qmd embed failed for collection '$c' — vector search may answer from stale embeddings (lexical index is fresh)"
      fi
    else
      log "ERROR: qmd update -c $c FAILED (see log)"
      record_event "degraded" "qmd re-index failed for collection '$c' — retrieval may be stale"
      rc=1
    fi
  done
  # Coverage record for healthcheck's collections-covered and index-fresh
  # assertions. Only collections whose `qmd update` succeeded are listed —
  # written even when empty, so a fully failed reindex reads as no coverage
  # rather than inheriting last night's record.
  record_reindex ${updated[@]+"${updated[@]}"}
  return $rc
}

# Writes {"collections": [...], "at": "<iso>"} under `last_reindex` in
# run-state.json — locked read-merge-atomic-replace like the record_* helpers
# above (same race, same DREAMER_LOCK_HELD re-entrancy contract).
record_reindex() {
  "$PY" - "$META/run-state.json" "$@" <<'PYEOF' \
    || log "WARN record_reindex failed — coverage NOT recorded (see stderr above)"
import datetime, os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
from dreamer_common import update_run_state
path = Path(sys.argv[1])
collections = sys.argv[2:]
def mutate(d):
    d["last_reindex"] = {
        "collections": collections,
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
update_run_state(path, mutate)
PYEOF
}

regen_catalog() { "$PY" "$ROOT/scripts/vault.py" catalog >/dev/null && log "catalog regenerated"; }

# The dashboard is a read-only projection, so it can never fail the run that
# produces it — a broken chart is not a reason to lose a night's research.
# Refreshed on every commit rather than only at end-of-job: a run that defers
# mid-way still leaves a dashboard describing the state it actually reached.
regen_dashboard() {
  "$PY" "$ROOT/scripts/dashboard.py" >/dev/null 2>&1 \
    && log "dashboard regenerated" \
    || log "WARN dashboard regeneration failed (non-fatal)"
}

commit() {
  local msg="$1"
  cd "$ROOT" || return 1
  if [[ -z "$(git status --porcelain)" ]]; then
    log "nothing to commit"
    return 0
  fi
  # Stage only what a job is allowed to write. `git add -A` swept whatever the
  # owner happened to be editing into unrelated job commits — jobs run at 02:00
  # but also resume in-session, so a half-finished script edit could land inside
  # "nightly-extract: created=3". Vault pages and logs are the entire job
  # surface; anything else in the tree is the owner's and not ours to commit.
  git add -- vault logs
  if git diff --cached --quiet; then
    log "nothing to commit (job surface clean; other changes left alone)"
    return 0
  fi
  git -c user.name="dreamer" -c user.email="dreamer@localhost" \
      commit -q -m "$msg" && log "committed: $msg"
  # After, not before: the dashboard should describe the state that was just
  # committed. It is gitignored, so this never dirties the tree it just cleaned.
  regen_dashboard
}
