# Configuration

Every key in `config.example.yaml`, what it does, and — the part that matters —
how to pick a value from your own data instead of trusting the default.

`./bin/setup.sh` copies `config.example.yaml` to `config.yaml`, which is
gitignored so your real paths and tuning never land in a commit. The constants
here are the ones [CLAUDE.md](../CLAUDE.md) names; the constitution is forbidden
from hardcoding any of them, which is what keeps the rules readable and the
numbers tunable.

Config is loaded once per process (`dreamer_common.CFG`). Jobs are separate
processes, so an edit takes effect on the next job — no reload needed.

---

## `paths`

```yaml
paths:
  root:            .
  vault:           vault
  inbox:           vault/inbox
  resurfacings:    vault/inbox/resurfacings
  sources:         vault/sources/transcripts
  loops:           vault/loops
  conclusions:     vault/conclusions
  concepts:        vault/concepts
  archive:         vault/archive
  digests:         vault/digests
  meta:            vault/.vault-meta
  logs:            logs
```

Resolution is in `dreamer_common.p()`: **relative paths resolve against the repo
root**, so a fresh clone works wherever you put it. **Absolute paths are honoured
unchanged**, and `~` is expanded.

### Putting the vault in a separate private repo

This is the recommended setup and the reason absolute paths are supported. Your
vault is a reasoning record of your own conversations; it should not share
history with code you might one day make public.

```bash
mkdir -p ~/private/dreamer-vault && git -C ~/private/dreamer-vault init
```

```yaml
paths:
  vault:        /home/you/private/dreamer-vault
  inbox:        /home/you/private/dreamer-vault/inbox
  resurfacings: /home/you/private/dreamer-vault/inbox/resurfacings
  sources:      /home/you/private/dreamer-vault/sources/transcripts
  loops:        /home/you/private/dreamer-vault/loops
  conclusions:  /home/you/private/dreamer-vault/conclusions
  concepts:     /home/you/private/dreamer-vault/concepts
  archive:      /home/you/private/dreamer-vault/archive
  digests:      /home/you/private/dreamer-vault/digests
  meta:         /home/you/private/dreamer-vault/.vault-meta
```

Change all of them together — they are independent keys, and leaving one
pointing inside the code repo splits your vault across two places.

One consequence to know about: `bin/_common.sh :: commit` runs
`git add -- vault logs` **inside the code repo**. With the vault moved out, job
commits become no-ops and versioning the vault is on you (a `git -C
~/private/dreamer-vault add -A && commit` in a cron line, or your own wrapper).
The same is true of a stock clone, where `.gitignore` excludes `vault/**` and
`logs/` — nothing is lost either way, but do not expect automatic history until
you have set it up.

`concepts` is reserved: the directory is created and linted around, but no
current script writes to it.

---

## `corpora`

```yaml
corpora:
  claude_export:   /path/to/claude-export
  wisdom_sources:
    - /path/to/your/library
  wisdom_build:    wisdom_md
```

Both corpora are optional, and each degrades a specific thing.

### `claude_export`

The default target for `scripts/convert_claude_export.py`. It accepts a `.zip`,
an unpacked export directory, or a bare `conversations.json`. The converter is
written against the real claude.ai export schema, verified against an
887-conversation dump:

```json
[ { "uuid": "...", "name": "...", "created_at": "...",
    "chat_messages": [ { "sender": "human", "text": "...", "content": [ ... ] } ] } ]
```

It reads `content[]` blocks as well as `text`, because in a real archive some
messages carry an empty `text` and a populated `content[]`; a naive `.text`
reader silently drops them. Tool-use and thinking blocks are summarised rather
than dumped, since tool noise is not reasoning you did and would poison
extraction.

**Without it**, nothing breaks — you ingest transcripts yourself. Drop Markdown
files into `vault/sources/transcripts/YYYY/MM/` (or anything under
`paths.sources`; `make_batch.py` globs recursively) and the pipeline picks them
up. The practical route for other providers is to write a converter that emits
the same shape and reuses `dreamer_common.redact()`.

In practice you will mostly use `./bin/ingest.sh`, which takes drops from
`to_ingest/` regardless of this setting.

### `wisdom_sources`

Directories of books, papers and lecture transcripts — your reference library.
`scripts/build_wisdom_corpus.py` walks them and converts `.txt` / `.md`,
`.epub` / `.mobi` / `.azw3` / `.doc(x)` / `.rtf` / `.fb2` (via `ebook-convert`)
and `.pdf` (via `pdftotext -layout`) into Markdown under `wisdom_build`,
preserving the source directory structure — folder names like "Wisdom Books" or
"The Great Courses" are real thematic signal that feeds qmd's context tree.
Source files are read-only; the builder never touches them.

**Without it**, the `wisdom` route has nothing to read. That is a *supported*
state, not a broken one: rule 5a says a wisdom route returning no usable hits
escalates to `web` and finishes as `mixed`, with the escalation recorded in the
conclusion. You get web-researched conclusions instead of library-grounded ones.

Choosing what to put here matters more than the size. A library of anthropology,
behavioural science, economics and negotiation has essentially nothing to say
about software architecture or current tooling — technical loops routed `wisdom`
will legitimately come back empty and escalate. That is the system working, but
if *most* of your loops are technical, expect the wisdom route to be mostly
decorative.

### `wisdom_build`

Where extracted plain text is written for qmd to index. Gitignored. Relative to
the repo root. Only change it if the default disk is too small — the converted
corpus is roughly the size of the source text.

---

## `decay`

```yaml
decay:
  go_live_date:    null
  decay_weeks:     8
  terminal_multiplier: 2
```

### `go_live_date` — start at `null`, and mean it

While this is `null`, `Loop.decay_deadline()` returns `None` for every loop and
**decay is completely inert**. That is the correct value for a first run and for
the whole of any backfill.

When it is set, the decay clock is anchored at:

```
max(last_seen, go_live_date) + window
```

never `last_seen` alone. The floor is the whole point. A backfill produces loops
whose most recent occurrence is months or years old; measured against
`last_seen`, every one of them would be past its deadline on the first
`decay-archive` run and the entire backfill would archive itself overnight.
The floor gives every backfilled loop a full window *from go-live* to prove it
is still live.

**How to choose:** set it to the date you finished your backfill and were happy
with what came out — after `scripts/calibrate.py sample` and the ≥70% "genuinely
open as stated" read-through, not before. Format `YYYY-MM-DD`.

### `decay_weeks`

Default `8`. How long an `open` loop may go untouched before archiving.
Archiving is not deletion: the page moves to `vault/archive/`, it is still
searchable with `include_archived`, and any new occurrence reopens it
automatically (`add_occurrence()` moves it back).

**How to choose:** ask how long a topic can be quiet in *your* life and still be
live. Someone exporting weekly with a fast-moving project should go shorter (4-6);
someone whose threads span quarters should go longer (12+). The
digest's "Archiving soon" section lists anything within 14 days of its deadline,
so you get a warning before anything goes — if you find yourself reviving loops
from that list every week, the window is too short.

### `terminal_multiplier`

Default `2`. `paused` and `decision-only` loops decay at
`decay_weeks × terminal_multiplier`. They have earned a conclusion or a framing;
that work should stay reachable longer than an unresearched thread. `researching`
never decays at all, and `archived` is already terminal.

---

## `matching`

```yaml
matching:
  recurrence_min:  2
  stage_a_top_k:   5
  recency_half_life_days: 60
  merge_proposal_expiry_weeks: 4
  max_judged_per_run: 40
  precision_floor: 0.70
  precision_window: 30
```

### `recurrence_min` — measure this, do not guess it

How many distinct conversations a loop needs before it is eligible for research.
It gates selection only; a loop is *tracked* from its first occurrence.

**The default of 2 is a placeholder.** The threshold's selectivity is entirely
scale-dependent: over a multi-year archive, 2 admits nearly everything and the
real filter silently becomes "top N by recency-weighted rank", which is a
different system from the one the config claims to describe.

Read it off your own corpus:

```bash
python3 scripts/calibrate.py histogram   # -> vault/digests/recurrence-histogram.md
```

That file gives you the full recurrence distribution, the cumulative count at
each threshold, and eligible-loop counts for `recurrence_min` of 2, 3 and 4. The
question it answers is: *at what threshold does the threshold actually filter?*

Rules of thumb the histogram applies for you:

| Eligible share at your threshold | Verdict |
|---|---|
| > 60% | Permissive. The threshold is not filtering; ranking is. Raise it, or accept ranking as the real filter and know that you have. |
| 5-60% | Workable — excludes the long tail without starving the weekly run. |
| < 5% | Restrictive. Expect consecutive quiet weeks. Lower it, or feed in more history. |

Failure modes at each end:

- **Too low**: the weekly run drowns in one-offs. You spend research budget on
  questions you raised once and never returned to, and the digest fills with
  conclusions you do not care about. The signal Dreamer exists to find —
  *persistence* — stops being what selects.
- **Too high**: quiet week after quiet week. `weekly-dream.sh` exits early with
  a "no loop reached the recurrence threshold" digest, which is honest but
  useless. Check the histogram before assuming extraction is broken.

Also re-run the histogram after a large backfill. A threshold calibrated on a
100-transcript pilot slice will not hold at 900.

### `stage_a_top_k`

Default `5`. How many catalog candidates Stage A considers before opening any
full loop page (rule 7 / rule 11). This is read by the extraction prompt, not by
Python.

Higher means better matching recall and more tokens per candidate. Raise it if
your digest's matching sample shows false *splits* (the model created a loop
that clearly duplicates one it never considered); leave it alone if the
`considered` lists in `matching-decisions.json` already contain the right loop
and the judge simply chose wrong — that is a Stage B problem, not Stage A.

### `recency_half_life_days`

Default `60`. Selection ranks by
`recency_weighted_score = Σ 0.5 ** (age_days / half_life)` over occurrence dates,
not by raw `recurrence_count`. A topic raised three times last month outranks one
raised three times two years ago, because a new occurrence is what defines
relevance.

Shorten it (30) to make the weekly run chase whatever is hot right now. Lengthen
it (120+) to let long-running threads with a steady drip of occurrences compete
with recent bursts. If you are backfilling years of history and the weekly run
keeps picking recent trivia over the deep recurring threads, this is the knob —
not `recurrence_min`.

Loops with no parseable occurrence dates fall back to
`distinct_conversations() × 0.01`, so they never outrank a loop with real
dated evidence.

### `merge_proposal_expiry_weeks`

Default `4`. A merge proposal you have not confirmed is re-proposed once after
this long, and on the second lapse recorded as `expired` — never `rejected`,
because expiry means you did not look, not that you decided. The expiry is
announced in the digest and buys a cooldown of the same length in the judgment
cache, so `detect()` stops re-paying for it every run.

**How to choose:** match your digest-reading rhythm. If you read the digest
fortnightly, 4 weeks gives you two chances to see a proposal. Shorter than your
reading cadence and proposals expire before you ever see them twice.

### `max_judged_per_run`

Default `40`. Paid Stage-B judge calls per merge refresh.

This is a **drain rate**, not a work volume, because of the judgment cache. Each
candidate pair is judged once; the verdict is stored in
`vault/.vault-meta/merge-judgments.json` with a fingerprint of both titles and
both occurrence lists. A cached `distinct` is reused — and invalidated the
moment either title changes **or either loop gains a new occurrence**. That
second invalidation is deliberate: rule 7 justifies over-splitting by promising
a false split self-heals as both loops accrue evidence, and the pairs most
deserving a second look are precisely the ones gaining occurrences under an
unchanged title.

So the steady-state cost is "new and newly-changed pairs", not "all pairs".
Candidates over the cap are logged as `remain queued`, never silently dropped.

**How to choose:** run `weekly-dream.sh` once and read the `merge candidates:`
line in `logs/weekly-dream.log`. If `remain queued` is persistently non-zero
across several runs, the backlog is not draining — raise the cap until it does,
then leave it. After the initial backfill the number needed drops sharply.

### `precision_floor` and `precision_window`

Defaults `0.70` and `30`. The digest samples ten unmarked matching decisions per
week as checkboxes; `digest.ingest_marks()` reads your ✓/✗ back on the next run.
`rolling_precision()` computes correctness over the last `precision_window`
marked decisions and flags anything below `precision_floor` as
"below floor — tuning flag raised".

Unmarked boxes are *no signal*, by design — you are never forced to grade
everything. Until you have marked anything, the digest says so explicitly rather
than reporting a silent 100%.

**How to choose:** leave them. `precision_window` should be big enough to be
stable and small enough to react (30 marks ≈ 3 weeks of light marking).
`precision_floor` is the point below which you would want to go rewrite the
extraction prompt.

---

## `extraction`

```yaml
extraction:
  batch_size:      15
  slice_size:      100
```

### `batch_size`

Default `15`. Transcripts per LLM call, used by `make_batch.py` as the default
`--limit`. Smaller means more resumable and more per-call overhead; larger means
fewer calls but a bigger loss when one defers or fails.

The unit of loss is one batch. A usage-limit exit does **not** mark the batch
extracted, so it retries whole next run. An apply failure quarantines the batch
by name and continues.

**How to choose:** raise it if your batches complete comfortably within
`budget.max_turns_backfill` and the logs show no truncated JSON; lower it if you
see apply failures from malformed or unclosed JSON, which is the signature of a
reply that got too long. `BATCH_SIZE=5 ./bin/backfill.sh 1` overrides it for one
run without touching config.

### `slice_size`

Default `100`. The size of the pilot slice you run before committing to a full
backfill: extract ~100 transcripts, then run `scripts/calibrate.py all`, read
the histogram, review the 25-loop sample against the ≥70% "genuinely open as
stated" gate, and set `recurrence_min` before spending the rest of your budget.
This one is an operator-facing number — no script reads it.

---

## `budget`

```yaml
budget:
  max_turns_nightly:  60
  max_turns_weekly:   150
  max_turns_backfill: 80
  model: sonnet
  cost_ceiling_per_run: null
```

### `max_turns_*`

Passed straight to `claude -p --max-turns`. Rule 9: if a job approaches the cap
it should finish the current unit cleanly, write what it has, and stop.

`max_turns_weekly` is per **loop**, not per run — `weekly-dream.sh` makes one
model call per selected loop so a limit exit costs one loop, not the week. It is
the largest because a single research call may read the catalog, several loop
pages, run qmd queries across collections, fetch web pages and then compose a
cited page.

**How to choose:** watch for truncated output. A job that consistently produces
usable results is not turn-starved; raise a limit only when you see work cut
short mid-page in `logs/`. Raising limits raises the ceiling on a bad run's
cost, so do it deliberately.

### `model`

Default `sonnet`, passed as `--model`. Jobs are high-volume and structured, so a
cheaper model per call means more calls before any usage window closes.

Set it explicitly and leave it set. Before this key existed the jobs passed no
`--model` and silently inherited whatever the CLI default was — meaning the
model doing your unattended extraction could change under you without a single
line changing in this repo.

### `cost_ceiling_per_run`

Default `null` = unlimited. **Cost is recorded and reported every run
regardless**; only the gate is off.

Two things to understand before you set a number:

1. **Under a subscription, the reported figure is not money.** `total_cost_usd`
   from the CLI is API-equivalent notional pricing. Your subscription is already
   paid, and embedding/reranking run locally for free. Gating on that figure
   means an expected, finite backfill cost silently disables the weekly dream —
   observed live, with batches costing a notional $6-26 against a $5 ceiling,
   which would have muted research for the entire backfill window.
2. **The real limit is the rolling usage window**, and the harness already
   handles it. `run_claude` treats any non-zero exit as a clean, resumable
   deferral: it logs it honestly, records it in `run-state.json`, and the caller
   stops with work intact for the next run. Nothing is lost and nothing reports
   false success.

There is also no mid-run abort — cost is reported *after* the run. So the
ceiling, when set, makes the **next** run skip research. The flag latches
(`skip_research_next_run = old or breach`), because weekly-dream calls the model
once per loop and a plain assignment let the last cheap loop clear a breach the
expensive ones set. It is cleared only after a run has actually honoured it and
said so in the digest.

**How to choose:** leave it `null` on a subscription. Set a float only if you
move the jobs to a paid API key, where the number becomes real money — and set
it well above your observed per-run cost, which `run-state.json → costs[]` gives
you after a few runs.

---

## `freshness`

```yaml
freshness:
  stale_inbox_days: 10
```

Default `10`. If no new transcript has been ingested in this long, the digest
opens with a freshness warning telling you the export may be overdue. Measured
from the newest `date` in `vault/.vault-meta/ingested.json`, not from file
mtimes. If nothing has ever been ingested, the banner says that instead.

This exists because an empty inbox is not self-evidently fine: a quiet digest
from a system that simply stopped receiving input looks identical to a quiet
digest from a quiet week.

**How to choose:** set it slightly longer than your export interval. Weekly
exports → 10 is right. Monthly exports → 35, or you will get a warning every
digest and learn to ignore it.

---

## `research`

```yaml
research:
  weekly_loop_count: 3
  max_fetches_per_loop: 45
  min_days_between_reresearch: 21
  web_conclusion_stale_days: 90
```

### `weekly_loop_count`

Default `3`. How many loops `vault.py select` returns, and therefore how many
model calls the weekly dream makes.

**How to choose:** by how much you will actually read. The digest's design
target is a ten-minute decision-first read; three cited conclusions is already a
lot of reading. Raise it only if your eligible pool is large and your digests
consistently leave good loops unresearched — check the "Growing loops" table for
loops sitting near the top week after week without ever being picked.

### `max_fetches_per_loop`

Default `45`. Hard cap on outbound fetches for one loop's research (rule 12),
injected into the dream prompt. The web-egress contract also requires that
outbound queries be generic research questions composed from the loop's
canonical statement — never verbatim transcript text, personal identifiers or
third-party names — and that every query and URL be logged. They appear in the
conclusion's "Egress record" and in the digest's "Web queries sent".

**How to choose:** lower it if you want a tighter egress surface, or if the
digest shows conclusions built on thirty shallow sources rather than five good
ones. `decision-only` routes must produce zero fetches regardless — that is
asserted, not budgeted.

### `min_days_between_reresearch`

Default `21`. A **hard cooldown**, enforced in `vault.reresearch_gate()`
regardless of what the selection judge decides. Within this many days of a
conclusion's date, the loop is not re-research eligible; it is served instead,
and the serve is recorded in the digest under "Conclusions served (no
re-research)".

This backstop exists because without it one loop generated four superseding
conclusions in two days, each citing the previous one under "What you previously
concluded", every claim graded accepted — an entire edifice resting on nothing.

**How to choose:** at least as long as your export cadence, so a single export
cannot trigger repeated re-research of the same loop. Shortening it below ~14
days is asking for the failure above.

### `web_conclusion_stale_days`

Default `90`. Only externally-factual conclusions rot on age alone. When nothing
new has been said since the conclusion was written, a `web`- or `mixed`-routed
conclusion becomes re-research eligible once it is this old; `wisdom`,
`past-reasoning` and `decision-only` conclusions do not, because principles do
not expire on a timer.

**How to choose:** by how fast your web-routed topics actually move. Pricing,
tooling and API surfaces: 60-90 days. Slower-moving factual questions: 180. The
cooldown above still applies underneath it.

---

## `mcp`

```yaml
mcp:
  transport: stdio
  note_max_chars: 500
  max_resurfacings_per_run: 50
```

`transport: stdio` documents what the server is: `scripts/dreamer_mcp.py` speaks
newline-delimited JSON-RPC 2.0 on stdin/stdout and has no TCP listener, so there
is no port to expose and no token to manage. The value is declarative — the
server has no other mode.

`note_max_chars` (default `500`) caps the note on `log_resurfacing`; longer
notes are rejected with an error rather than truncated. It is small on purpose.
The note is untrusted client input (rule 10) that gets quoted into the vault, and
a resurfacing is a *relevance* signal — "this came up again, here is the new
angle" — never evidence.

`max_resurfacings_per_run` (default `50`) caps the pending queue in
`vault/inbox/resurfacings/`. Once full, `log_resurfacing` returns
`"resurfacing queue is full; run nightly-extract"` rather than accumulating
unbounded work for the nightly job. Raise it only if you genuinely log dozens of
resurfacings between nightly runs.

---

## `pins`

```yaml
pins:
  qmd_version:     "2.5.3"
  claude_obsidian_commit: "1c1bc49c03a685ee8f5d09c99efe52b42d6673f5"
  claude_obsidian_scope: "skills/autoresearch only (vendored, MIT)"
```

Provenance, not behaviour — no script reads these. `qmd_version` records the
retrieval engine this was built and measured against (the relevance floors and
timeouts in `dreamer_mcp.py` were tuned against it). The `claude_obsidian_*`
pins record the upstream commit `skills/autoresearch/` was vendored from; see
its `ATTRIBUTION.md`.

Update them when you upgrade or re-vendor, so a future reader can tell what the
measurements were taken against.

---

## Tuning by symptom

| Symptom | Most likely key | Direction |
|---|---|---|
| Weekly run has nothing to research, week after week | `matching.recurrence_min` | Lower it — and run `calibrate.py histogram` first |
| Drowning in one-off loops nobody cares about | `matching.recurrence_min` | Raise it |
| Weekly run picks recent trivia over deep recurring threads | `matching.recency_half_life_days` | Raise it |
| Long-dormant threads never get picked despite high counts | `matching.recency_half_life_days` | Raise it |
| Merge backlog never drains (`remain queued > 0` every run) | `matching.max_judged_per_run` | Raise it |
| Obvious duplicate loops the merge judge never sees | `matching.stage_a_top_k`, then the merge candidate floor | Raise `stage_a_top_k` |
| Merge proposals expire before you ever review them | `matching.merge_proposal_expiry_weeks` | Raise to match your reading cadence |
| Everything archived right after the backfill | `decay.go_live_date` | It was set too early — the floor only works if go-live is *after* the backfill |
| Loops you still care about keep archiving | `decay.decay_weeks` | Raise it; watch "Archiving soon" in the digest |
| Digest is more reading than you have time for | `research.weekly_loop_count` | Lower it |
| The same loop gets re-researched repeatedly | `research.min_days_between_reresearch` | Raise it |
| Web-routed conclusions feel out of date | `research.web_conclusion_stale_days` | Lower it |
| Backfill batches fail on malformed JSON | `extraction.batch_size` | Lower it |
| Research silently stopped running, no error | `budget.cost_ceiling_per_run` | Check `run-state.json → skip_research_next_run`; on a subscription, set the ceiling back to `null` |
| Jobs cut off mid-page | `budget.max_turns_*` | Raise the one for that job |
| Freshness warning on every digest | `freshness.stale_inbox_days` | Raise it to match your export interval |
| Conclusions cite no books at all | not a config problem | `qmd` is not on `PATH` under cron, or `corpora.wisdom_sources` is unbuilt — check `logs/` for the `degraded` event |

For anything that is not a knob, see
[architecture.md § Where to look when something breaks](architecture.md#8-where-to-look-when-something-breaks).
