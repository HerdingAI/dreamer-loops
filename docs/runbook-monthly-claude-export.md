# Monthly official claude.ai export

The daily sync (`docs/claude-ai-sync.md`) is incremental: it fetches chats whose
`updated_at` passed its cursor. The official export is the safety net. Do this
about once a month, and any time `source.claude-ai` has been stale for days.

The export is also the fallback when the sync cannot run. The sync reads
claude.ai's unofficial web endpoints, which may change or break without
notice, and when claude.ai shows a bot check or challenge it records `blocked`
and stops: it never tries to get past it. The extension's permissions
(`alarms`, `nativeMessaging`, `cookies`, `storage`, and `https://claude.ai/*`)
and the data it moves (your own conversation JSON and the login cookie's expiry
date, never its value, sent over native messaging to `to_ingest/` on the same
machine and to no other host) are described in `docs/claude-ai-sync.md`. With
`claude_ai_sync.extension_id` empty the sync is off and its freshness axes read
`info` / `disabled`; this export is then the only feed.

## Steps

1. claude.ai, Settings, Privacy, Export data. Chrome emails a link. **Download
   within 24 hours**, the link expires.
2. The export now arrives as split per-category zips. What each is for:
   - `conversations-000.zip`: the transcripts. **The only one that feeds
     `bin/ingest.sh`.** (If there are `conversations-001...`, drop them too.)
   - design_chats: a different format. Needs an adapter that does not exist yet;
     do not drop it in `to_ingest/`.
   - memories, projects, artifacts: not transcripts. Keep them if wanted;
     they do not go through the converter.
3. Put the zip in `to_ingest/` and run `bin/ingest.sh` (no arguments). The
   converter dedupes on `uuid` + `updated_at` through `.vault-meta/ingested.json`,
   so re-runs and overlap with the daily sync are harmless, and a chat that grew
   is replaced with only its new turns queued for extraction.
4. Extraction happens in the nightly job as usual (`bin/backfill.sh` to force).

## What it catches that the daily sync does not

- Edits to old chats that did not move `updated_at` past the cursor (if claude.ai
  ever does that), and anything the sync skipped: temporary chats are skipped
  by design, and a `schema_changed` run delivered nothing.
- Chats deleted on claude.ai: the sync never sees a deletion. They remain in the
  vault (sources are immutable provenance); the reconcile step lists them.
- Project knowledge docs, memories and artifacts: the sync only fetches chats.

## Reconcile (after step 3)

Compare the export with the ledger. Run from the repo root:

```python
python3 - <<'PY'
import json, zipfile, sys
sys.path.insert(0, "scripts")
import dreamer_common as dc
z = zipfile.ZipFile("to_ingest/processed/<the archived conversations-000.zip>")
name = next(n for n in z.namelist() if n.endswith("conversations.json"))
export = {c["uuid"]: c for c in json.load(z.open(name))}
ledger = dc.read_json(dc.p("meta") / "ingested.json", default={})
missing  = [u for u in export if u not in ledger]
stale    = [u for u in export if u in ledger and ledger[u]["updated_at"] != export[u]["updated_at"]]
deleted  = [u for u in ledger if u not in export]
print(f"export {len(export)}  ledger {len(ledger)}")
print(f"in export, not ingested: {len(missing)}  (empty chats are skipped by design)")
print(f"updated_at differs:      {len(stale)}  (should be 0 right after ingest)")
print(f"in ledger, not in export (deleted since): {len(deleted)}")
PY
```

Read the numbers: `not ingested` should be only chats with no renderable text;
anything else is a converter skip, check `logs/ingest.log` for `SKIP bad record`.
`updated_at differs` must be 0 immediately after ingest. A sync cursor older than
the newest `updated_at` in the export is fine; one that is ahead of it means the
sync skipped chats: lower `cursor` in `vault/.vault-meta/claude-ai-sync.json`
and click the extension icon (the ledger dedupes the overlap).
