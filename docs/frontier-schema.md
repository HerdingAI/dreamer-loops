# Frontier display contract

This document is the contract for the two optional consumers of the knowledge
frontier. Both are off by default.

1. **A display** reads the map from Postgres (the first half of this page).
2. **The lesson consumer ("the show")** is whatever app the owner points
   `frontier.show_url` at; Dreamer sends it lessons over HTTP (the last section).

## The Postgres display

Dreamer publishes its knowledge map to Postgres for any display that wants it.
It runs only when `DREAMER_PUBLISH_FRONTIER=1` is set in
`~/.config/dreamer/postgres.env`.

- **Host and database:** the ones named in `postgres.env` (database `dreamer`)
- **Schema:** `frontier`
- **Writer:** `scripts/frontier_publish.py`, Dreamer only. It runs after each
  frontier job and every hour, and it replaces every table in one transaction
  (truncate + insert). The schema is never dropped while live.
- **Readers:** sessions with `default_transaction_read_only=on`.

The schema carries content: goal titles, item titles and summaries, and lesson
text. It does **not** carry evidence quotes, passages, web excerpts, the egress
record, internal causes or reasons, or any count. A reader that needs a number
derives nothing. A display shows lists, never counts.

## Tables

`frontier.snapshot`: exactly one row.

| Column | Type | Meaning |
|---|---|---|
| `published_at` | timestamptz | When this copy was written (Postgres clock) |
| `content_hash` | text | Hash of the canonical content; unchanged content moves only `published_at` |

`frontier.goal`: every goal, all statuses.

| Column | Type | Meaning |
|---|---|---|
| `id` | text PK | `G0003` |
| `title` | text | |
| `status` | text | `active`, `paused`, `achieved`, `abandoned` |

`frontier.item`: one row per map item.

| Column | Type | Meaning |
|---|---|---|
| `id` | text PK | `K2001` |
| `title`, `summary` | text | |
| `quadrant` | text | `known_known`, `known_unknown`, `unknown_known`, `unknown_unknown` |
| `last_evidence_on` | date NULL | Newest evidence date. The page orders by it, newest first |
| `lesson_id` | text NULL | The item's newest lesson whose status is not `dropped` or `rejected`. NULL means no lesson in effect |
| `lesson_status` | text NULL | That lesson's status: `ready`, `held`, `released`, `aired`, `got_it`, `dig_deeper`, `failed` |

`frontier.item_goal`: the item–goal relations (an item may serve several goals).

| Column | Type | Meaning |
|---|---|---|
| `item_id`, `goal_id` | text | PK together |
| `relation` | text | `connected`, `unconnected` |

`frontier.evidence`: what shows the owner met an item, by chat, never by quote.

| Column | Type | Meaning |
|---|---|---|
| `item_id` | text | |
| `position` | int | Order on the item page (newest first) |
| `kind` | text | `explained`, `decided`, `applied`, `asked`, `reconstructed`, `exposure` |
| `chat_title` | text | The transcript's title (its frontmatter `title`, else the file name without date or extension) |
| `on_date` | date | The transcript's date |

`frontier.move`: quadrant changes, newest last.

| Column | Type | Meaning |
|---|---|---|
| `item_id` | text | |
| `at` | timestamptz | |
| `from_quadrant`, `to_quadrant` | text | `from_quadrant` is empty for a new item |

`frontier.lesson`: one row per lesson that is not `dropped` or `rejected`.

| Column | Type | Meaning |
|---|---|---|
| `id` | text PK | `K2001-1` |
| `item_id`, `goal_id` | text | |
| `title` | text | The lesson's headline, the first line of its `## Lesson` section (what the show presents it as); the item's title when it has none |
| `kind` | text | `new`, `follow_up` |
| `status` | text | As `item.lesson_status` |
| `body` | text | Markdown of the `## Lesson` section only. Citation markers `[n]` refer to `sources`. Render as text |
| `sources` | jsonb | `[{"n": 1, "title": "...", "url": "https://..."}]`, or `"ref"` in place of `url` for a library source. No passages |
| `released_at` | timestamptz NULL | Sent to the show |
| `aired_on` | date NULL | First aired |
| `got_it_at`, `dig_deeper_at` | timestamptz NULL | The tap in effect. An undone tap is NULL at the next publish |

## Views

`frontier.v_snapshot`: `published_at`, `snapshot_age_seconds` (Postgres clock).

`frontier.v_movement`: what moved on the frontier, one row per event, for a display.

```sql
SELECT kind, lesson_id, title, at FROM frontier.v_movement
 WHERE at > now() - make_interval(days => $1)
 ORDER BY at DESC LIMIT $2;
```

`kind` is `released`, `aired`, `got_it` or `dig_deeper`. A display might show
`released` ("New lesson"), `got_it` and `dig_deeper`. `at` is `released_at`,
`aired_on` at midnight UTC, `got_it_at` or `dig_deeper_at`.

`frontier.v_goal_items`: the map page, one row per item–goal pair.

```sql
SELECT goal_id, goal_title, goal_status, item_id, item_title, summary, quadrant,
       relation, last_evidence_on, lesson_id, lesson_status
  FROM frontier.v_goal_items
 ORDER BY goal_id, quadrant, last_evidence_on DESC NULLS LAST, item_id;
```

## Reader rules

- A missing schema or relation (SQLSTATE `42P01`) means "not published yet":
  show the empty state, not an error.
- Lesson ids match `^K\d{4,}-\d+$`; reject anything else before querying.
- Text is plain text. Never render a field as HTML.

## The lesson consumer (the show)

`scripts/frontier_sync.py` talks to the show. Dreamer starts every exchange; the
show never calls Dreamer. Nothing is sent while `frontier.show_url` is empty.

- **Transport.** The show is expected to serve **HTTPS**. Plain `http://` is
  accepted only to a loopback host (`127.0.0.0/8`, `::1`, `localhost`); any
  other `http://` URL is refused before a request is made, unless the owner sets
  `frontier.allow_insecure_http: true`.
- **Authentication.** Every request carries the shared secret in an
  `X-Dreamer-Token` header. The secret is the `DREAMER_LESSON_TOKEN` entry of the
  env file named by `frontier.show_env_path` (override with `DREAMER_SHOW_ENV`).
  It is never logged or printed. Answer `401` for a wrong token and `503` when the
  show has no token configured.
- **Vocabulary.** A lesson is *aired* when the show has presented it to the owner; `episode_status` is the show's own status for that presentation (`skipped` or `bad` mean it will not be presented).
- **Bodies are JSON.** A reply that is not a JSON object is treated as empty.

| Request | Meaning |
|---|---|
| `GET /lessons/status?after=<n>` | Reply `{"lessons": [{"id", "aired", "assigned_date", "episode_status"}], "taps": [{"id", "lesson_id", "kind", "at", "resolved"}]}`. `episode_status` of `skipped` or `bad` fails the lesson; taps with an id above `after` are returned, `kind` is `got_it` or `dig_deeper`, `at` is UTC |
| `POST /lessons` | Body `{"id", "kind", "title", "body", "sources", "released_at"}`. Answer `2xx` to store it. Answer `409` with `{"detail": {"stored_sha256": "..."}}` when a different body is stored under that id (the lesson text is frozen once released) |
| `POST /known` | Body `{"titles": ["..."]}`: the titles the owner already knows, replacing the previous list |

Any other `4xx` rejects that lesson for good. `5xx` and network errors are
retried on the next run.
