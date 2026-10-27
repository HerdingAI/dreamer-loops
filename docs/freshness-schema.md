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
