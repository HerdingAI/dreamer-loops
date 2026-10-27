# claude.ai daily sync

A daily, incremental copy of your own claude.ai chats into Dreamer.

```
everyday Chrome (logged in to claude.ai)
  Dreamer Sync extension (MV3, extension/dreamer-sync/)
     hourly alarm + browser start: ask the host "status" -> due?
     list chats -> fetch each (spaced) -> send to host
          | native messaging (stdin/stdout, length-prefixed JSON)
  scripts/claude_ai_host.py   (launched by bin/claude-ai-host)
     spools chats; on "commit" writes ONE atomic drop
       to_ingest/claude-ai-sync-<UTC>.json   (export-shaped list)
     and updates vault/.vault-meta/claude-ai-sync.json
          |
  18:00 cron: bin/sync-claude-ai.sh
     waits for wiki.lock, converts drops (scripts/convert_claude_export.py,
     the dedupe ledger is .vault-meta/ingested.json), records `converted`
          |
  19:00 nightly-extract picks the new transcripts up
```

## Why an extension

Headless automation tends to meet a Cloudflare challenge (HTTP 403), while
requests from a real, logged-in browser do not. The sync therefore runs inside
your everyday Chrome, as you, and does not disguise automation.

Three limits to know before relying on it:

- **Terms of service.** Reading your chats through the website's own endpoints
  is not a sanctioned API. Check that it fits the terms that apply to your
  claude.ai account before you enable the sync. The official export is always
  the supported path, and the sync stays off until you set
  `claude_ai_sync.extension_id`.
- **Unofficial endpoints.** The extension reads claude.ai's own web-app
  endpoints (the chat list and chat detail calls the website makes). They are
  not a documented or supported API and may change or break without notice. A
  changed response is reported as `schema_changed`; nothing is written.
- **Bot checks.** When claude.ai answers with a bot check or challenge, the sync
  records `blocked` and stops. It never tries to get past it (no user-agent
  spoofing, virtual displays or cookie replay). The monthly official export is
  the fallback (`docs/runbook-monthly-claude-export.md`).

## The extension

- Permissions (`extension/dreamer-sync/manifest.json`), nothing broader:
  - `alarms`: the hourly tick that asks the host whether a run is due.
  - `nativeMessaging`: the only channel to the local host
    (`scripts/claude_ai_host.py`); it is how chats reach `to_ingest/`.
  - `cookies`: reads two claude.ai cookies. `lastActiveOrg` gives the
    organization ID that goes into the chat URLs, and `sessionKey` is read only
    for its expiry date (see "What leaves the browser").
  - `storage`: `chrome.storage.local` keeps the last outcome, shown by the
    toolbar badge.
  - host permission `https://claude.ai/*`: the chat requests are made from the
    extension with your logged-in session, and to no other site.
- Extension ID: the manifest carries no pinned `key`, so Chrome derives the ID
  from the folder you load. Load the unpacked extension, copy the ID Chrome
  shows on `chrome://extensions` into `config.yaml` as
  `claude_ai_sync.extension_id`, then run `bin/install-sync-extension.sh`. It
  validates the ID and allows exactly that one origin to talk to the host. The
  example config leaves the ID empty; until it is set the sync's freshness axes
  read `info` / `disabled`.
- Triggers: an hourly alarm and `runtime.onStartup` ask the host for `status`;
  the host says `due` when `last_ok` is older than `due_hours` (or a capped run
  left `pending` chats; after a failure it waits `retry_after_failure_hours`).
  Clicking the toolbar icon forces a run.
- All tunables (`quiet_hours`, `max_per_run`, `spacing_seconds`, ...) live in
  `config.yaml` under `claude_ai_sync:` and reach the extension in the host's
  status reply.
- Pure logic is in `lib.js` (no `chrome.*`); `background.js` is the shell.
  Tests: `node --test 'extension/dreamer-sync/*.test.js'`.

### What leaves the browser

- The JSON of your own conversations (the chat list and each chat's messages),
  handed to the native host.
- The login cookie's expiry DATE, never its value, so the host can warn before
  the login lapses.

The host writes both to this machine only: drops go to `to_ingest/` and the
state to `vault/.vault-meta/claude-ai-sync.json`, via native messaging. The
extension talks to claude.ai and to the local host and to nothing else; nothing
is sent to any other host.

### How a run avoids losing a chat

1. Page the list (newest first) until a page reaches the cursor. A short page
   does not end paging, only an empty page or the cursor does (the server may
   clamp `limit`). A list that is not sorted `updated_at` descending is
   `schema_changed`.
2. Drop temporary chats. Keep chats newer than the cursor.
3. Order oldest first. Chats updated within `quiet_hours` are deferred (they may
   still be in progress), and the run takes at most `max_per_run`. Never split a
   group of chats sharing one `updated_at`.
4. The cursor is the newest `updated_at` among the chats actually delivered.
   Everything deferred is strictly newer, so the next run lists it again. The
   host refuses a commit whose chat count differs from what it received, and
   never moves the cursor backwards.
5. Any failure sends `report` and no `commit`: the cursor and `last_ok` stay put
   and the spool is discarded; the next run refetches.

The cursor starts, on first run, at the newest `updated_at` in
`.vault-meta/ingested.json` (that ledger holds claude.ai chats only; Claude Code
and desktop sessions use `cc-ingested.json`).

### Failure states

| `status` | Cause | What to do |
|---|---|---|
| `needs_login` | no `lastActiveOrg` cookie, HTTP 401/403 with JSON, redirect to login | log in to claude.ai in everyday Chrome, click the extension icon |
| `blocked` | HTML/Cloudflare challenge response | nothing automatic. Open claude.ai in Chrome and use it normally; click the icon later. Use the monthly export meanwhile |
| `schema_changed` | a required key missing in a list item, chat or message, or list order changed | the API changed: fix `lib.js` validators/converter. Nothing was written |
| `error` | anything else (network, repeated 429/5xx, host rejected data) | see `logs/claude-ai-host.log`; the next due tick retries |

`source.claude-ai` (freshness) turns stale on any non-`ok` status, or when
`last_ok` is older than `freshness.claude_ai_sync_max_age_hours`. The extension
also shows a red `!` badge, and `chrome.storage.local.lastResult` (inspect via the
service worker console) holds the last outcome. If Chrome cannot start the host
at all, nothing can be reported: the state goes stale and the axis says so.

### Re-login

Log in to claude.ai in the everyday Chrome, then click the Dreamer Sync icon.
The run is forced, succeeds, and the axis returns to ok.

The login expires after a while. The extension reports its expiry every hour,
and `source.claude-ai` goes `stale` ("claude.ai login expires in N days")
`freshness.claude_ai_login_warn_days` before it lapses, so the freshness
display asks for the re-login while the sync still works. To renew early: log out of claude.ai,
log back in, click the icon. A logout is also seen on the next hourly tick
("no claude.ai login in Chrome"), without waiting for the next due run.

## The cron job

`bin/sync-claude-ai.sh` (18:00) converts waiting drops. It holds `wiki.lock`
while converting, waiting up to `claude_ai_sync.lock_wait_seconds` for a running
extraction to finish (otherwise a grown chat could be rewritten between an
extraction's read and its `mark_extracted`, and the new turns lost). If the wait
runs out it converts nothing, leaves the drops, logs why and exits non-zero. It
exits 0 when there is nothing to convert, whatever the sync status: login
problems are reported by the `source.claude-ai` axis, not by this job.

## Install

See `bin/install-sync-extension.sh` (writes the Chrome host manifest, prints the
one-time steps) and `bin/install-cron.sh` (adds the 18:00 line). Run both from
the live install, since the host manifest records the absolute path of
`bin/claude-ai-host`.

## Alongside the official export

The daily sync is the primary feed; the monthly official export is the safety
net: `docs/runbook-monthly-claude-export.md`.
