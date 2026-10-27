#!/usr/bin/env bash
# frontier — the nightly knowledge-frontier pass. Chained after
# nightly-assess on the 19:00 cron line (bin/install-cron.sh).
#
# One pass under frontier.lock, in this order:
#   1  frontier_sync.py pull      aired / failed lessons, taps, follow-up cancels, cursor
#   2  reindex frontier, then frontier_map.py run-batch   (ONE batch a night)
#   3  frontier_gaps.py run       pre-mortem for the goals that are due
#   4  frontier_lessons.py run    research, verify, compose, check
#   5  frontier_sync.py push      release within the ceiling, send the known list
#   6  reindex frontier, frontier_publish.py, commit
#
# Locking: holds frontier.lock for the whole run and wiki.lock only inside each step's
# apply (and the commit), so the owner's MCP writes and the other cron jobs never wait
# behind a model call. A step that cannot get wiki.lock exits 2: it is deferred, nothing
# is marked, and the run goes on.
#
# Failure policy: a step that fails (nonzero rc, or a status saying degraded, unreachable
# or failed) records a `degraded` event and the independent later steps still run. A map
# failure never blocks taps or push; a show outage never blocks the map. A usage-limit exit
# (rule 9) stops the remaining MODEL steps cleanly; pull, push and publish still run.
JOB=frontier
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
arm_heartbeat

run_healthcheck "before frontier"

if leg_blocked "frontier"; then
  log "frontier leg is blocked by healthcheck — skipping"
  record_event "degraded" "frontier leg blocked by healthcheck; work resumes when health passes"
  mark_skipped
  exit 0
fi

acquire_frontier_lock
log "start"

# classify <step> — reads a step's JSON on stdin; prints `ok`, `stopped` (usage limit) or
# `degraded <detail>`. Details name statuses and counts only, never text from a reply.
classify() {
  "$PY" -c '
import json, sys
step = sys.argv[1]
try:
    d = json.load(sys.stdin)
except ValueError:
    print("degraded unreadable status"); sys.exit(0)
def bad(msg): print("degraded " + msg)
if step in ("pull", "push"):
    st = d.get("status", "")
    if st not in ("ok", "not configured", "disabled"):
        bad(st or "no status")
    elif step == "push" and (d.get("conflicts_new") or d.get("rejected")):
        bad("%d conflict(s), %d rejected lesson(s)" % (len(d.get("conflicts_new") or []), len(d.get("rejected") or [])))
    else:
        print("ok")
elif step == "map":
    st = d.get("status", "")
    print("ok" if st in ("done", "drained") else "stopped" if st == "stopped" else "degraded map " + (st or "no status"))
elif step == "gaps":
    sts = [g.get("status", "") for g in d.get("goals") or []]
    print("stopped" if "stopped" in sts else
          "degraded pre-mortem " + ",".join(sorted({s for s in sts if s in ("deferred", "failed")}))
          if {"deferred", "failed"} & set(sts) else "ok")
elif step == "lessons":
    # A "skipped" result is the queue working (goal paused, item moved); one that ran out of
    # attempts records its own event. A run skipped for the cost ceiling is said (rule 9).
    sts = {r.get("status", "") for r in d.get("results") or []}
    print("stopped" if d.get("stopped") else
          "degraded research skipped (%s)" % d["skipped"] if d.get("skipped") else
          "degraded lessons " + ",".join(sorted(sts & {"deferred", "failed"}))
          if sts & {"deferred", "failed"} else "ok")
else:
    bad("unknown step")
' "$1"
}

# run_step <name> <label> <script> [args] — runs the step, leaves its stdout in OUT and
# its verdict in VERDICT (ok | stopped | deferred | degraded | failed). Never aborts the run.
run_step() {
  local name="$1" label="$2" script="$3"
  shift 3
  OUT=$("$PY" "$ROOT/scripts/$script" "$@" 2>>"$LOGS/$JOB.log")
  local rc=$?
  VERDICT=ok
  if [[ $rc -eq 2 ]]; then
    log "$label: deferred — wiki.lock stayed busy; nothing was marked, runs again next time"
    VERDICT=deferred
    OUT='{}'
    return 0
  elif [[ $rc -ne 0 ]]; then
    log "$label: FAILED (rc $rc, see $LOGS/$JOB.log)"
    record_event "degraded" "frontier $label failed (exit $rc); see the $JOB log"
    VERDICT=failed
    OUT='{}'
    return 0
  fi
  log "$label: $OUT"
  local verdict
  verdict=$(printf '%s' "$OUT" | classify "$name")
  case "$verdict" in
    ok) ;;
    stopped)
      log "$label: usage limit — model steps stop for tonight, the work resumes next run"
      VERDICT=stopped ;;
    degraded*)
      log "$label: ${verdict#degraded }"
      record_event "degraded" "frontier $label: ${verdict#degraded }"
      VERDICT=degraded ;;
  esac
  return 0
}

jlen() { "$PY" -c 'import json,sys; print(len(json.loads(sys.argv[1]).get(sys.argv[2]) or []))' "$1" "$2" 2>/dev/null || echo 0; }

STOP_MODEL=0

# Taps and delivery state, before anything else (follow-ups head the queue).
run_step pull "pull" frontier_sync.py pull
PULL="$OUT"

# One map batch. A stale index would hide items earlier batches wrote and duplicate
# them, so a failed refresh skips the batch (reindex_collection records the event).
MAPPED=0
if reindex_collection frontier; then
  run_step map "map batch" frontier_map.py run-batch
  [[ "$VERDICT" == stopped ]] && STOP_MODEL=1
  MAPPED=$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1]).get("mapped", 0))' "$OUT" 2>/dev/null || echo 0)
else
  log "map batch: skipped — the qmd index could not be refreshed"
fi

# Pre-mortems (model).
if [[ $STOP_MODEL -eq 0 ]]; then
  run_step gaps "pre-mortem" frontier_gaps.py run
  [[ "$VERDICT" == stopped ]] && STOP_MODEL=1
else
  log "pre-mortem: deferred — usage limit"
fi

# Lessons (model).
LESSONS='{}'
if [[ $STOP_MODEL -eq 0 ]]; then
  run_step lessons "lessons" frontier_lessons.py run
  [[ "$VERDICT" == stopped ]] && STOP_MODEL=1
  LESSONS="$OUT"
else
  log "lessons: deferred — usage limit"
fi

# Release within the ceiling and deliver. Needs no model, so a usage limit never blocks it.
run_step push "push" frontier_sync.py push
PUSH="$OUT"

# Refresh the index with tonight's pages, project to Postgres, commit.
reindex_collection frontier || log "WARN frontier index not refreshed — retrieval may be stale"
"$PY" "$ROOT/scripts/frontier_publish.py" >> "$LOGS/frontier-publish.log" 2>&1 \
  || log "WARN frontier_publish exited non-zero (see $LOGS/frontier-publish.log)"

AIRED=$(jlen "$PULL" aired); TAPS=$(jlen "$PULL" taps_applied); RELEASED=$(jlen "$PUSH" released)
RESEARCHED=$(jlen "$LESSONS" results)
if [[ "$AIRED" -gt 0 || "$TAPS" -gt 0 || "$RELEASED" -gt 0 || "$MAPPED" -gt 0 ]]; then
  mark_produced
fi
commit_locked "frontier $(TODAY): aired=$AIRED taps=$TAPS mapped=$MAPPED researched=$RESEARCHED released=$RELEASED" \
  || log "WARN commit skipped; the next run carries it"
log "done"
exit 0
