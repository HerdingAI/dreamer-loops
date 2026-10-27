# Board display contract

Dreamer can publish its current problem → solution pairs to Postgres for a
display. It is **off by default**: it runs only when
`DREAMER_PUBLISH_BOARD=1` is set in `~/.config/dreamer/postgres.env`.

- **Database:** a Postgres database you control, named by `PGDATABASE` in
  `~/.config/dreamer/postgres.env` (override the path with `DREAMER_PG_ENV`)
- **Schema:** `board`

Unlike `health`, this schema carries content: loop titles, conclusion titles
and conclusion text. Enable it only for a display you are content to show
them on.

## Two queries

The list (no body text, small):

```sql
SELECT id, loop_id, problem, answer, concluded_on, confidence, raised, tags,
       ev_accepted, ev_provisional, ev_contested, ev_unsupported,
       published_at, total, snapshot_age_seconds
  FROM board.v_pairs
 ORDER BY concluded_on DESC NULLS LAST, id DESC;
```

One pair's text:

```sql
SELECT restated, synthesis, open_questions FROM board.pair WHERE id = $1;
```

| Column | Meaning |
|---|---|
| `id` | Stable id of the conclusion; use it to open a pair |
| `problem` | The recurring problem (the loop's title) |
| `answer` | The solution found (the conclusion's title) |
| `raised` | How many distinct conversations raised the problem |
| `confidence` | `low`, `medium` or `high` |
| `ev_*` | Evidence counts by grade; all `NULL` when the page has no ledger |
| `restated`, `synthesis`, `open_questions` | Markdown text. Render as text, never as HTML |
| `published_at`, `snapshot_age_seconds` | When the set was last published, on Postgres's clock |
| `total` | Number of pairs in the set |

## What is and is not published

- Only **current** conclusions: a superseded conclusion is not listed.
- Not the conclusions of **resolved** loops. Marking a loop resolved (through
  the MCP's `set_status`) is how the owner takes a solution off the board;
  reopening it puts the solution back. Archived loops stay listed.
- A status change made through the MCP publishes straight away
  (`board.publish_on_owner_write`), so a display sees it within its own poll.
- Only these sections of a conclusion: the restated problem, the synthesis,
  the open sub-questions, and the evidence counts.
- Never published: fetched web excerpts, the egress record, earlier
  conclusions, library notes, and any section added later until it is
  allow-listed in `scripts/board_publish.py`.
- A page is skipped, and counted in `logs/board-publish.log`, when it has no
  synthesis or when its headings are repeated or out of the order Dreamer
  writes them in. That is how a heading planted by embedded web text is kept
  from being published as the answer.
- No tag filter is applied. `tags` is carried so a display can filter.

## Reading it safely

- Use autocommit or close the transaction after each read.
- Poll no faster than once a minute.
- The set is replaced in one transaction; a reader never sees a partial set.
  A pair opened by `id` can disappear between a list read and the click when
  a newer conclusion superseded it.
- Is the laptop on? Ask `health.v_status` (`laptop_reachable`), not this
  schema.
- Do not add columns to these tables; build your own beside them.

## Operating it

| Question | Command |
|---|---|
| Turn it on | add `DREAMER_PUBLISH_BOARD=1` to `postgres.env`, then `scripts/board_publish.py --init` |
| What would be published? | `scripts/board_publish.py --dry-run` |
| What does the display see? | `scripts/board_publish.py --show` |
| Did the last publish work? | `tail logs/board-publish.log`, or the `publish.board` axis |
| Stop publishing | remove the flag; `publish.board` then reads `disabled`. **Rows already published stay** and a display keeps showing them |
| Remove what was published | remove the flag, then `DROP SCHEMA board CASCADE` |

Troubleshooting:

- **`board: disabled`** — the flag is unset or is not exactly `1`.
- **`board: failed (UndefinedTable)`** — the schema was never created; run
  `--init`. A routine publish runs no DDL.
- **`board: failed (OperationalError)`** — host unreachable, wrong
  credentials, or a timeout. The next publish sends the complete set.
- **`board: failed (UnicodeEncodeError)`** or garbled characters on the
  display — the database is not UTF8 (`SELECT pg_encoding_to_char(encoding)
  FROM pg_database WHERE datname = current_database()`). Recreate it as UTF8;
  `--create-database` now does so for a new one.
- **Fewer pairs than conclusion pages** — superseded pages are excluded; a
  page with no loop page or no title is skipped and logged by file name in
  `logs/board-publish.log`.
