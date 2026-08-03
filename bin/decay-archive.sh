#!/usr/bin/env bash
# Job 3 — decay-archive (§6.9). Sunday 02:45, BEFORE weekly-dream.
JOB=decay-archive
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
acquire_lock
log "start"

"$PY" - <<'PYEOF'
import sys, json
sys.path.insert(0, "scripts")
import vault as V, digest as G
archived = V.run_decay()
for loop in archived:
    # Job 2 generates the digest file at 03:00; this job runs at 02:45, so it
    # stages into pending.json exactly as Job 1 does rather than writing a
    # digest that does not exist yet.
    G.stage("archived", {"id": loop.id, "title": loop.title})
print(json.dumps({"archived": [l.id for l in archived]}))
PYEOF

regen_catalog
commit "decay-archive $(TODAY)"
log "done"
