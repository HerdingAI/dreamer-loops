# Freshness display contract

Dreamer publishes its freshness snapshot to Postgres after every job and once
an hour. A display reads it; Dreamer never notifies anything.

- **Database:** a Postgres database you control, named by `PGDATABASE` in
  `~/.config/dreamer/postgres.env` (override the path with `DREAMER_PG_ENV`)
- **Schema:** `health`

Publishing is opt-in. With no env file the publisher does nothing, exits 0,
and the `publish.postgres` axis reports status `info` with reason `disabled`,
so an opt-in feature that is off never reads as stale. The env file holds
`PGHOST=`, `PGPORT=`, `PGDATABASE=`, `PGUSER=` and `PGPASSWORD=` (mode 600).
Run `bin/freshness-setup.sh` once after creating it.

Only operational metadata is published: axis ids, fixed labels, timestamps,
counts, verdicts and numeric reasons. No loop titles, goal text or quotes.

## One query

```sql
SELECT axis, kind, label, status, age_seconds, reason,
       laptop_reachable, snapshot_age_seconds, overall
  FROM health.v_status
 ORDER BY kind, axis;
```

| Column | Meaning |
|---|---|
| `status` | `ok`, `stale`, `never` (no record at all), `info` (reported, not judged) |
| `age_seconds` | Age of the clock that judges the axis, on Postgres's clock |
| `reason` | Short numeric explanation, e.g. `last ok run 31.2h ago (max 28h)` |
| `laptop_reachable` | `false` when no snapshot has arrived for 2 hours: the laptop is off or asleep |
| `snapshot_age_seconds` | How old the whole snapshot is |
| `overall` | `stale` when any judged axis is `stale` or `never` |

Read `laptop_reachable` first. When it is `false`, every `status` is the
last-known value, not the current one.

## Reading it safely

- Use autocommit or close the transaction after each read. Dreamer bounds its
  own lock waits, so an idle open transaction cannot hang a job, but it can
  make a publish time out and leave the snapshot one hour older.
- Poll no faster than once a minute; the data changes after a job (a handful
  of times a night) and once an hour.
- The schema is created by Dreamer. Do not add columns to these tables; build
  your own views or tables beside them.

## Axis ids

| Axis | Kind | Window |
|---|---|---|
| `source.claude-export` | source | 10 days |
| `source.claude-ai` | source | 36 hours; `info` / `disabled` while `claude_ai_sync.extension_id` is empty; `stale` with reason `needs re-login` etc. when the latest sync failed (see `claude-ai-sync-state.md`) |
| `source.cc-sessions` | source | 3 days |
| `source.owner-writes` | source | reported only |
| `job.ingest-cc` | job | 28 hours |
| `job.sync-claude-ai` | job | 28 hours; `info` / `disabled` while `claude_ai_sync.extension_id` is empty |
| `job.nightly-extract` | job | 28 hours |
| `job.nightly-assess` | job | 28 hours |
| `job.night-cycle` | job | 28 hours |
| `job.weekly-dream` | job | 28 hours |
| `job.decay-archive` | job | 8 days |
| `job.frontier` | job | 28 hours |
| `job.healthcheck` | job | 16 hours |
| `converge.extraction` | convergence | 2 days |
| `converge.assessment` | convergence | 2 days |
| `converge.frontier-map` | convergence | 3 days; transcripts the knowledge-frontier map has not caught up with (never mapped, or grown since). `info` / `map not started` until the map ledger exists; a chat the map gave up on is not counted (see `frontier-stuck-items`) |
| `converge.premortem` | convergence | `frontier.premortem_every_days` plus 3 days of slack (10 days as shipped); an active goal whose newest pre-mortem, or its creation when it has none, is older than that |
| `converge.folds` | convergence | 7 days |
| `converge.search-index` | convergence | 30 hours |
| `output.conclusion` | output | reported only |
| `output.digest` | output | reported only |
| `publish.postgres` | publish | 3 hours |
| `publish.board` | publish | 3 hours; `info` / `disabled` unless the owner enabled the board publisher |
| `publish.frontier` | publish | 3 hours; `info` / `disabled` unless `DREAMER_PUBLISH_FRONTIER=1` (the knowledge-map publisher) |
| `publish.lesson-sync` | publish | 28 hours; `info` / `disabled` while `frontier.show_url` is empty (the opt-in lesson sync); judged from the newest pull or push in the sync state file |

`threshold_hours` on each row carries the window, so a display never needs to
hardcode this table.

## Tables

| Table | Rows | Contents |
|---|---|---|
| `health.snapshot` | 1 | `published_at`, `generated_at`, `host`, `overall`, `stale_count`, `axis_count` |
| `health.axis` | 1 per axis | `kind`, `label`, `status`, `last_ran`, `last_produced`, `last_arrival`, `last_outcome`, `threshold_hours`, `backlog`, `reason`, `updated_at` |
| `health.job_run` | append-only | `job`, `started_at`, `finished_at`, `outcome` (`ok`, `failed`, `skipped`), `produced` |
| `health.assertion` | 1 per healthcheck assertion | `name`, `ok`, `severity`, `checked_at` |

## Axes

| Kind | Stale when |
|---|---|
| `job` | The job's last successful run is older than its window, or its last run failed |
| `source` | No new input for longer than the window |
| `convergence` | Input arrived and the downstream step has not processed it within the window |
| `output` | Never; reported for context |
| `publish` | Dreamer has not published successfully for 3 hours |

Windows live under `freshness:` in `config.yaml`.

## Operating it

| Question | Command |
|---|---|
| What does the display see? | `scripts/freshness_publish.py --show` |
| What does Dreamer compute locally? | `scripts/freshness.py --json` |
| Did the last publish work? | `tail logs/freshness-publish.log` |
| Recreate the view after changing `reachable_max_age_hours` | `scripts/freshness_publish.py --init` |
| First-time setup | `bin/freshness-setup.sh` |

Troubleshooting:

- **`publish: not configured`** — `~/.config/dreamer/postgres.env` is missing.
- **`publish: failed (OperationalError)`** — host unreachable, wrong
  credentials, or a statement hit the 10-second timeout. The next publish
  retries with a complete snapshot; nothing queues.
- **Every job axis reads `never`** — `vault/.vault-meta/heartbeats.json` was
  lost (it is git-ignored, so a fresh clone starts empty). Each axis recovers
  the next time its job runs.
- **`laptop_reachable` is false but the laptop is on** — the hourly cron line
  is missing; run `bin/install-cron.sh`.
