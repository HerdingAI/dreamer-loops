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
}

# --- claude -p wrapper ----------------------------------------------------
# A usage-limit exit is a NORMAL outcome that defers work to the next run
# (Principle 9). It must be logged honestly and must not look like success.
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
run_claude() {
  local prompt_file="$1" max_turns="$2" out_file="$3"
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
  "$CLAUDE_BIN" -p "$(cat "$prompt_file")" \
      --output-format json \
      ${model:+--model "$model"} \
      --max-turns "$max_turns" \
      --permission-mode acceptEdits \
      --allowedTools "Bash(qmd:*)" "mcp__qmd" "WebSearch" "WebFetch" \
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
record_event() {
  "$PY" - "$META/run-state.json" "$1" "$2" <<'PYEOF'
import json,sys,os,datetime
path,kind,detail=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(path)) if os.path.exists(path) else {}
d.setdefault("events",[]).append(
    {"at":datetime.datetime.now().isoformat(timespec="seconds"),
     "job":os.environ.get("JOB","?"),"kind":kind,"detail":detail})
json.dump(d,open(path,"w"),indent=2)
PYEOF
}

record_deferral() {
  "$PY" - "$META/run-state.json" "$1" <<'PYEOF'
import json,sys,datetime,os
path,code=sys.argv[1],sys.argv[2]
d=json.load(open(path)) if os.path.exists(path) else {}
d.setdefault("deferrals",[]).append(
    {"at":datetime.datetime.now().isoformat(timespec="seconds"),
     "exit_code":int(code),"job":os.environ.get("JOB","?")})
json.dump(d,open(path,"w"),indent=2)
PYEOF
}

record_cost() {
  [[ -f "$1" ]] || return 0
  "$PY" - "$META/run-state.json" "$1" "$ROOT/config.yaml" <<'PYEOF'
import json,sys,os,datetime,yaml
state,costfile,cfgfile=sys.argv[1],sys.argv[2],sys.argv[3]
cost=float(open(costfile).read().strip() or 0)
cfg=yaml.safe_load(open(cfgfile))
raw_ceiling=cfg["budget"].get("cost_ceiling_per_run")
ceiling=None if raw_ceiling in (None,"","null","unlimited") else float(raw_ceiling)
d=json.load(open(state)) if os.path.exists(state) else {}
d.setdefault("costs",[]).append(
    {"at":datetime.datetime.now().isoformat(timespec="seconds"),
     "job":os.environ.get("JOB","?"),"cost":round(cost,4)})
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
    # There is no mid-run abort under `claude -p` (cost is reported after the
    # run), so exceeding the ceiling makes the NEXT run skip research instead.
    # LATCH, do not assign. weekly-dream calls run_claude once per loop, so a
    # plain assignment lets the last cheap loop clear a breach the expensive
    # ones set. Observed live: 4 breaches, final state False. Cleared only by
    # clear_skip_research() once the skip has actually been honored.
    d["skip_research_next_run"] = bool(d.get("skip_research_next_run")) or (cost > ceiling)
    if cost > ceiling:
        print(f"[cost] run cost {cost} exceeded ceiling {ceiling}: next run will skip research")
json.dump(d,open(state,"w"),indent=2)
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

clear_skip_research() {
  "$PY" - "$META/run-state.json" <<'PYEOF'
import json,os,sys
p=sys.argv[1]
d=json.load(open(p)) if os.path.exists(p) else {}
d["skip_research_next_run"]=False
json.dump(d,open(p,"w"),indent=2)
PYEOF
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
  local rc=0
  for c in vault transcripts conclusions; do
    if (cd "$ROOT" && qmd update -c "$c" 2>&1); then
      log "qmd re-index ok: $c"
    else
      log "ERROR: qmd update -c $c FAILED (see log)"
      record_event "degraded" "qmd re-index failed for collection '$c' — retrieval may be stale"
      rc=1
    fi
  done
  return $rc
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
