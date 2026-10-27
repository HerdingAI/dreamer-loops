# claude.ai sync state contract

`bin/sync-claude-ai.sh` (job `sync-claude-ai`, daily 18:00) writes
`vault/.vault-meta/claude-ai-sync.json` after EVERY attempt, success or not.
Monitoring reads only this file and the job heartbeat; how the job fetches is
not part of the contract.

```json
{"last_attempt": "<iso>", "last_ok": "<iso or null>",
 "status": "ok|needs_login|blocked|schema_changed|error",
 "detail": "<short text>", "cursor": "<iso updated_at high-water mark>",
 "fetched": 3, "converted": 2, "pending": false}
```

| Field | Meaning |
|---|---|
| `last_attempt` | When the latest run started or finished, whether or not it worked |
| `last_ok` | Last fully successful run; `null` if none. Never overwritten by a failure |
| `status` | Outcome of the LATEST attempt |
| `detail` | Free text for the owner's log. Never published |
| `cursor` | Newest chat `updated_at` fetched so far |
| `fetched`, `converted` | Counts for the latest run. The native host (`scripts/claude_ai_host.py`) writes `fetched` and sets `converted` to 0 when it commits a drop; `bin/sync-claude-ai.sh` then sets `converted` to the number of transcripts it wrote from the waiting drops. A failure writes both as 0 |
| `login_expires_at` | Optional. UTC expiry of the claude.ai login cookie in the everyday Chrome, sent by the extension on every hourly `status` call (the date only, never the cookie value) and written only when it changes. `null` = Chrome holds no claude.ai login. Absent = an extension too old to report it, and judged as before |
| `pending` | Optional. `true` when the last run hit `max_per_run` and chats are still waiting; the host then reports the sync as due again at the next hourly tick. Readers may ignore it |

Writers: the host owns every field except `converted` after the commit, and
never moves `cursor` backwards or changes `last_ok` on a failure.

## What the extension reads and sends

The state above comes from an extension that reads claude.ai's unofficial web
endpoints, which may change or break without notice (`schema_changed`). When
claude.ai shows a bot check or challenge the sync records `blocked` and stops;
it never tries to get past it, and the monthly official export is the fallback.

Permissions in `extension/dreamer-sync/manifest.json`:

- `alarms`: the hourly "is a run due" tick.
- `nativeMessaging`: the only channel to the local host.
- `cookies`: `lastActiveOrg` (organization ID for the chat URLs) and the
  `sessionKey` cookie's expiry date.
- `storage`: the last outcome, for the toolbar badge.
- `https://claude.ai/*`: where the chat requests go.

What leaves the browser: the conversation JSON of the owner's own chats, and
the login cookie's expiry DATE only, never its value. Both go over native
messaging to this machine (`to_ingest/` and this state file). Nothing is sent
to any other host.

## Verdicts (`scripts/freshness.py`)

With `claude_ai_sync.extension_id` empty in `config.yaml` the sync is not set
up, and both axes below report status `info` with reason `disabled`, whatever
state files exist.

Two axes, both in the existing status vocabulary, so the Postgres `health`
schema and any display need no change.

- `job.sync-claude-ai` (kind `job`): heartbeat rule like every other job.
- `source.claude-ai` (kind `source`):

| State file | `status` | `reason` starts with |
|---|---|---|
| missing | `never` | `no sync state recorded` |
| not a JSON object (corrupt) | `stale` | `sync state file unreadable` |
| `status` = `ok`, `last_ok` null | `never` | `no last ok sync recorded` |
| `status` = `ok`, `last_ok` within window | `ok` | `last ok sync 1.0h ago` |
| `status` = `ok`, `last_ok` older than the window | `stale` | `last ok sync 40.0h ago` |
| `status` = `ok`, `login_expires_at` within `freshness.claude_ai_login_warn_days` | `stale` | `claude.ai login expires in 2.0 days` |
| `status` = `ok`, `login_expires_at` in the past | `stale` | `claude.ai login expired` |
| `status` = `ok`, `login_expires_at` = `null` | `stale` | `no claude.ai login in Chrome` |
| `needs_login` | `stale` | `needs re-login` |
| `blocked` | `stale` | `blocked by claude.ai` |
| `schema_changed` | `stale` | `claude.ai response format changed` |
| `error` | `stale` | `last attempt errored` |
| any other value | `stale` | `unrecognised sync status` |

A non-`ok` latest status is never ok, however recent `last_ok` is. The
reason is built from the table above, not from `detail`. Reasons also carry
`cursor ...; fetched N last run` when present. The window is
`freshness.claude_ai_sync_max_age_hours` (36) in `config.yaml`.

`source.claude-export` (the monthly manual export, 10-day window) is kept: it
is the fallback if the sync is blocked for good, and `digest.py` uses the same
window for its banner.
