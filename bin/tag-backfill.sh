#!/usr/bin/env bash
# tag-backfill — ONE-TIME operational script. NOT installed in cron.
#
# Retro-tags the loops minted before the vocabulary freeze (~107 untagged of
# ~150 live). Each run selects up to config `tagging.backfill_per_run`
# untagged, non-archived loops, asks the LLM to choose tags strictly from the
# frozen vocabulary (skills/tag-backfill/PROMPT.md), and applies the result
# through scripts/apply_tags.py — which never overwrites an existing tag list
# and hard-fails without a frozen vocabulary.
#
# Resumable by construction: selection re-queries the vault every run, so a
# deferral or partial run loses nothing. When zero untagged loops remain the
# script exits cleanly ("drain complete"), after which it — and the `tagging:`
# block in config.yaml — are safe to delete.
#
# Rule-15 carve-out: this one-time initialization may touch paused and
# decision-only pages; rule 2 binds every night thereafter.
JOB=tag-backfill
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
acquire_lock

log "start"

# Hard gate, checked BEFORE spending an LLM call: a backfill without a frozen
# vocabulary is meaningless (apply_tags.py enforces the same at apply time).
"$PY" - <<'PYEOF' || die "no frozen tag vocabulary (vault/.vault-meta/tag-vocabulary.json) — freeze one first: scripts/propose_tags.py freeze --tags ..."
import os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
import propose_tags
sys.exit(0 if propose_tags.vocabulary() else 1)
PYEOF

LIMIT=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml')).get('tagging',{}).get('backfill_per_run',40))")

# 1. Select up to LIMIT untagged, non-archived loops. Prints the TOTAL number
# of untagged loops on stdout; the batch itself goes to a file.
BATCH="$LOGS/.tag-backfill-batch.json"
UNTAGGED=$("$PY" - "$BATCH" "$LIMIT" <<'PYEOF'
import json, os, re, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
import vault as V

out, limit = sys.argv[1], int(sys.argv[2])

def excerpt(loop, heading):
    m = re.search(rf"^## {heading}\s*$(.*?)(?=^## |\Z)", loop.body or "",
                  re.M | re.S)
    return (m.group(1).strip() if m else "")

def theme(loop):
    # Prefer the free-prose theme note (the pre-vocabulary substitute for
    # tags); fall back to the statement. Trimmed: this is a classification
    # hint, not the page.
    return (excerpt(loop, "Theme") or excerpt(loop, "Statement"))[:300]

# load_loops() already excludes the archive directory and merge redirects;
# the status guard is belt-and-braces against a stray archived page in loops/.
untagged = [l for l in V.load_loops() if not l.tags and l.status != "archived"]
batch = [{"id": l.id, "title": l.title, "theme": theme(l)}
         for l in untagged[:limit]]
json.dump({"batch": batch}, open(out, "w"), indent=2)
print(len(untagged))
PYEOF
) || die "selection failed"

if [[ "${UNTAGGED:-0}" -eq 0 ]]; then
  log "drain complete — zero untagged loops remain; this script can be deleted"
  record_event "info" "tag-backfill: drain complete, 0 untagged loops remain"
  rm -f "$BATCH"
  exit 0
fi
log "untagged=$UNTAGGED, batching up to $LIMIT this run"

# 2. Build the prompt: template + frozen vocabulary + the batch.
PROMPT="$LOGS/.tag-backfill-prompt.md"
"$PY" - "$BATCH" "$PROMPT" <<'PYEOF' || die "prompt build failed"
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
import propose_tags

batch_file, out = sys.argv[1], sys.argv[2]
root = os.environ["ROOT"]
template = open(os.path.join(root, "skills/tag-backfill/PROMPT.md"),
                encoding="utf-8").read()
vocab = sorted(propose_tags.vocabulary() or [])
if not vocab:
    raise SystemExit("vocabulary vanished between the gate and the build")
batch = json.load(open(batch_file))["batch"]

marker = "## The batch"
vocab_block = "\n".join(f"- `{t}`" for t in vocab) + "\n\n"
template = template.replace(marker, vocab_block + marker, 1)
lines = [template.rstrip(), ""]
for e in batch:
    lines.append(f"### {e['id']}")
    lines.append(f"- title: {e['title']}")
    lines.append(f"- theme: {e['theme'] or '(none recorded)'}")
    lines.append("")
open(out, "w", encoding="utf-8").write("\n".join(lines))
PYEOF

# 3. The LLM half. Same turn budget as the cc-ingest summariser and for the
# same reason: the batch is inlined, the reply is JSON, no tools are needed.
#
# RESTRICTED run: this job needs NO tools at all — loop titles/themes are
# private content and must not leak into web queries (CLAUDE.md rule 12).
# run_claude's `restricted` mode (thread-fold unit) removes web, shell and
# edit permission entirely; the prompt's output contract (JSON only, no
# research) is now backed by the permission layer instead of standing alone.
MAXT=$("$PY" -c "import yaml;print(yaml.safe_load(open('$ROOT/config.yaml'))['budget'].get('max_turns_cc_ingest',10))")
RESULT="$LOGS/.tag-backfill-result.json"
if ! run_claude "$PROMPT" "$MAXT" "$RESULT" restricted; then
  # Usage-limit deferral (record_deferral already logged it). Selection
  # re-queries next run, so nothing is lost.
  log "tagger deferred — the batch resumes on the next run"
  exit 0
fi

# 4. Apply. apply_tags.py validates every tag against the frozen vocabulary,
# never overwrites an existing tag list, and fails loudly on malformed JSON.
STATS=$("$PY" "$ROOT/scripts/apply_tags.py" --input "$RESULT") \
  || die "apply_tags refused the batch — reply kept at $RESULT for inspection"
TAGGED=$(printf '%s' "$STATS" | "$PY" -c 'import json,sys;print(json.load(sys.stdin).get("tagged",0))')
log "apply: $(printf '%s' "$STATS" | tr '\n' ' ')"

REMAIN=$("$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.environ["ROOT"], "scripts"))
import vault as V
print(sum(1 for l in V.load_loops()
          if not l.tags and l.status != "archived"))
PYEOF
)
record_event "info" "tag-backfill: $TAGGED tagged, $REMAIN remaining"
rm -f "$PROMPT" "$BATCH" "$RESULT" "$RESULT".raw.json*

if [[ "$TAGGED" -gt 0 ]]; then
  commit "tag-backfill $(TODAY): $TAGGED loop(s) tagged, $REMAIN remaining"
fi

if [[ "${REMAIN:-1}" -eq 0 ]]; then
  log "drain complete — this script and config tagging: are safe to delete"
else
  log "done: $TAGGED tagged, $REMAIN remaining — run again to continue the drain"
fi
exit 0
