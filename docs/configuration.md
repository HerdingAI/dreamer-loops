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
  goals:           vault/goals
  frontier:        vault/frontier
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
  claude_code_sessions: ~/.claude/projects
  claude_desktop_sessions: ~/.config/Claude/local-agent-mode-sessions
  wisdom_sources:
    - /path/to/your/library
  wisdom_build:    wisdom_md
```

All corpora are optional, and each degrades a specific thing.

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

### `claude_code_sessions`

Default `~/.claude/projects`. Where `bin/ingest-cc.sh` sweeps for Claude Code
sessions — one `.jsonl` file per conversation at `<project>/<session-id>.jsonl`.
Files nested deeper than that are spawned subagent transcripts and are excluded
by shape. What qualifies, and how sessions are summarised into transcripts, is
governed by the [`cc_ingest`](#cc_ingest) block below.

**Without it** (or without ever running `ingest-cc.sh`), nothing breaks — you
simply track loops from claude.ai conversations only. Point it elsewhere only
if your Claude Code home is non-standard.

### `claude_desktop_sessions`

Default `~/.config/Claude/local-agent-mode-sessions`. Where the Claude desktop
app keeps its local agent sessions, one `audit.jsonl` per
`<account>/<org>/local_<id>/` directory.
`scripts/convert_desktop_sessions.py` reads them on the same 18:30 job as Claude
Code sessions, with the same triage floors and the same mandatory credential
redaction (audit logs hold plaintext secrets). Behaviour is governed by
[`desktop_ingest`](#desktop_ingest). **Without it** nothing breaks: point it at
a directory that does not exist, or leave the app uninstalled, and the sweep
finds nothing.

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

## `tagging`

```yaml
tagging:
  backfill_per_run: 40
```

### `backfill_per_run`

Default `40`. Batch size for `bin/tag-backfill.sh`, the **one-time** drain
that tags loops created before your vocabulary was frozen (rule 15's
initialization carve-out — it is the only tagging path allowed to touch
`paused` and `decision-only` pages). Ongoing tagging needs no knob: once
`vault/.vault-meta/tag-vocabulary.json` exists, extraction emits
vocabulary-validated tags as part of its normal pass, and `apply_tags.py`
rejects anything outside the vocabulary (rule 4).

**How to choose:** leave it. The drain is resumable, so the batch size only
sets how much one run bites off. The key — and the script — are safe to
delete once the drain reports complete.

---

## `cc_ingest`

```yaml
cc_ingest:
  min_human_turns:      3
  min_human_chars:      1500
  min_quiet_hours:      6
  max_code_lines:       8
  max_summarizer_chars: 240000
  max_sessions_per_run: 10
  entrypoints:          [cli]
  exclude_projects:     []
```

Governs `bin/ingest-cc.sh` (see
[architecture.md §10](architecture.md#10-claude-code-session-ingestion)).
Defaults also live in `scripts/convert_cc_sessions.py`, so the job works
before this block exists; anything set here wins.

### `min_human_turns` and `min_human_chars`

Defaults `3` and `1500`. Floors on what counts as a conversation rather than
a one-shot command. Below either floor the session is refused — with the
reason recorded in the ledger, so an over-strict filter and a quiet week do
not look the same.

**How to choose:** raise them if your transcripts fill with trivial
"run this, thanks" sessions; lower `min_human_chars` if you tend to hold real
design conversations in short turns.

### `min_quiet_hours`

Default `6`. A session must have been idle this long before it is ingested —
never capture half a conversation that is still being written, because the
ledger would mark it done and the rest would never be revisited.

**How to choose:** longer than your longest realistic mid-session break.

### `max_code_lines`

Default `8`. Fenced blocks longer than this collapse to a line count before
the summariser sees them. Short blocks are usually the thing being discussed;
long ones are pastes that would drown the conversation.

### `max_summarizer_chars`

Default `240000`. Summariser input budget per session. Owner turns are kept
whole and assistant context is spent first; if owner turns alone overflow,
both ends are kept and the elision is stated in the rendered prompt.

### `max_sessions_per_run`

Default `10`. Sessions summarised per run — one paid model call each, so this
bounds a run's spend and lets a backlog drain over several runs.

**How to choose:** by how many qualifying sessions a day you actually
produce. The 18:30 job runs daily, so a cap above your daily output means the
backlog never grows.

### `entrypoints`

Default `[cli]`, and this is the discriminator that matters: `sdk-cli`
sessions are headless cron runs and spawned subagents — **including Dreamer's
own nightly and weekly jobs**. Ingesting those would feed Dreamer its own
output as though it were yours. Do not widen this casually.

### `exclude_projects`

Default `[]` — an escape hatch, empty by design, because the entrypoint
filter already excludes Dreamer's own automation. List project directory
names here only if there is a project whose sessions you never want tracked.

---

## `goals`

```yaml
goals:
  max_active:                4
  silence_ping_days:         10
  movement_stale_days:       21
  drift_recurrence_min:      3
  max_open_questions:        2
  back_off_days:             14
  silence_escalation_weeks:  3
  assess_batch_size:         null
```

Governs the goal assessor (`bin/nightly-assess.sh`, see
[architecture.md §11](architecture.md#11-goals-and-the-assessor)). Goals are
authored by you; these keys decide how patient Dreamer is about them.

- `max_active` is enforced deterministically when a goal is committed or
  resumed. It exists because a long list of live commitments is a list nobody
  tracks. Lower it if you keep over-committing.
- `silence_ping_days` is how long an active goal may go without a mention
  before Dreamer asks about it, and `movement_stale_days` is how long it may go
  without a step, evidence or a plan change before it counts as stale. Mention
  and movement are separate clocks on purpose: talking about a goal is not
  moving it. **How to choose:** set silence to the longest gap you would
  normally leave a goal alone, and stale to a few multiples of how often its
  steps usually move.
- `drift_recurrence_min` is the recency-weighted score at which an open loop
  that serves no active goal is flagged with "what is this for?". Raise it if
  you get asked about loops you do not care about.
- `max_open_questions` is back-pressure on the queue, not a cap on what is
  kept: questions beyond it wait, and nothing ages out. `back_off_days` is how
  long a question you deferred is left alone, and `silence_escalation_weeks` is
  how long silence continues before its question escalates.
- `assess_batch_size` caps the transcripts the assessor reads per run, oldest
  unassessed first; the rest drain on later nights. `null` (the default) means
  no cap: the whole backlog goes into one run. Every selected transcript is
  inlined in one model call, so set a number before a large backfill.

The owner-write keys that go with this feature live under [`mcp`](#mcp).

---

## `desktop_ingest`

```yaml
desktop_ingest:
  ledger:               desktop-ingested.json
  project_label:        desktop
  max_sessions_per_run: 10
```

Claude desktop agent sessions use the same triage floors and quiet window as
[`cc_ingest`](#cc_ingest); set one of those keys here only to diverge.
`ledger` is separate so the Claude Code freshness axis keeps meaning Claude
Code. `project_label` is shown as `project:` on the page, since the app has no
project dimension. `max_sessions_per_run` bounds a run's summariser spend.

---

## `solving`

```yaml
solving:
  enabled: true
  min_options: 2
```

The solution spine (rule 5b). `enabled: false` removes the spine request from
the research prompt and restores the previous page layout, so it is the switch
to reach for if you want conclusions without the staged structure. `min_options`
is how many real options a conclusion must list besides the option named
"Leave it" that every full Options stage includes; a page with fewer is
flagged and may lose confidence. Two is the smallest number that is
still a choice.

---

## `budget`

```yaml
budget:
  max_turns_nightly:  60
  max_turns_weekly:   150
  max_turns_backfill: 80
  max_turns_cc_ingest: 10
  max_turns_thread_fold: 8
  max_turns_assess: 30
  max_turns_assess_fold: 8
  max_turns_frontier_research: 8
  max_turns_frontier_extract: 4
  max_turns_frontier_match: 4
  max_turns_frontier_premortem: 4
  max_turns_frontier_check: 4
  max_turns_frontier_consequence: 4
  max_turns_frontier_queries: 4
  max_turns_frontier_claims: 4
  max_turns_frontier_compose: 4
  max_turns_frontier_lesson_check: 4
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

### `max_turns_cc_ingest` and `max_turns_thread_fold`

Defaults `10` and `8`, and deliberately small. The session summariser reads
one inlined session and returns JSON; the thread fold reads one inlined
transcript plus the current thread and returns JSON. Both run without tools —
the fold in fully restricted, output-only mode — so neither should be taking
turns. A cap this low is a tripwire: hitting it means the prompt is
misbehaving, not that the job needs more room.

### The goal and frontier budgets

`max_turns_assess` (default `30`) is for the assessor's attachment pass, which
may read the goal catalog and transcripts. `max_turns_assess_fold` (default `8`)
is for the per-goal assessment fold, which runs restricted with no tools, like
the thread fold.

`max_turns_frontier_research` (default `8`) is for the gated research call:
WebSearch only, and each approved query costs about one turn, so this is also a
soft cap on queries per lesson. The nine other `max_turns_frontier_*` keys
(`extract`, `match`, `premortem`, `check`, `consequence`, `queries`, `claims`,
`compose`, `lesson_check`) are all `4`. They are sealed calls that read an
inlined prompt and answer with one JSON object, and `run_claude` clamps
restricted turns at 4, so raising one above 4 changes nothing.

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
  cc_sessions_quiet_days: 3
  claude_ai_sync_max_age_hours: 36
  claude_ai_login_warn_days: 3
  job_max_age_hours: 28
  weekly_job_max_age_days: 8
  extract_lag_days: 2
  assess_lag_days: 2
  frontier_map_lag_days: 3
  frontier_premortem_slack_days: 3
  publish_max_age_hours: 3
  publish_timeout_seconds: 5
  publish_statement_timeout_seconds: 10
  publish_deadline_seconds: 30
  reachable_max_age_hours: 2
  run_history_keep: 200
```

Windows for the freshness verdicts
([architecture.md §12](architecture.md#12-freshness-and-heartbeats)). Like the
health thresholds, they change when an alarm fires, never what a job does. A
verdict is `degraded` at worst and blocks nothing.

### `stale_inbox_days`

Default `10`. If no new transcript has been ingested in this long, the digest
opens with a freshness warning telling you the export may be overdue. Measured
from the newest `date` in `vault/.vault-meta/ingested.json`, not from file
mtimes. If nothing has ever been ingested, the banner says that instead. It is
also the window for the claude.ai export source axis.

This exists because an empty inbox is not self-evidently fine: a quiet digest
from a system that simply stopped receiving input looks identical to a quiet
digest from a quiet week.

**How to choose:** set it slightly longer than your export interval. Weekly
exports → 10 is right. Monthly exports → 35, or you will get a warning every
digest and learn to ignore it.

### The other windows

| Key | Default | Meaning |
|---|---|---|
| `cc_sessions_quiet_days` | `3` | No Claude Code session ingested for this long marks that source stale |
| `claude_ai_sync_max_age_hours` | `36` | How old the last ok claude.ai sync may be: one missed day plus slack |
| `claude_ai_login_warn_days` | `3` | The sync axis goes stale this many days before the claude.ai login expires, so you can renew it while the sync still works |
| `job_max_age_hours` | `28` | A nightly job's last good run may be this old: one missed night goes stale |
| `weekly_job_max_age_days` | `8` | Same for weekly jobs (decay) |
| `extract_lag_days`, `assess_lag_days` | `2`, `2` | A transcript waiting this long for extraction, or for the assessor while a goal is active, is stale |
| `frontier_map_lag_days` | `3` | A transcript waiting this long to be mapped is stale. The map takes one batch a night, so a long backfill reads stale by design |
| `frontier_premortem_slack_days` | `3` | An active goal is stale this long past `frontier.premortem_every_days` without a pre-mortem |
| `run_history_keep` | `200` | Runs kept in `heartbeats.json` |

**How to choose:** the job windows should be your schedule's longest quiet gap
plus slack, the same reasoning as `health.checked_max_age_hours`. If you only
run extraction weekly, raise `job_max_age_hours` or every night reads stale.

### The publish keys

`publish_max_age_hours` (default `3`) is how old the last successful Postgres
publish may be before it reads stale (opt-in; with no publisher configured the
axis reads `info`). The three timeouts bound a stuck database:
`publish_timeout_seconds` (connect, `5`), `publish_statement_timeout_seconds`
(per statement and lock wait, `10`) and `publish_deadline_seconds` (the whole
publish, after which the process ends, `30`). `reachable_max_age_hours`
(default `2`) is how old the newest snapshot may be before the display's
reachability flag turns false, meaning your machine is off or asleep.
Recreate the view after changing it (`scripts/freshness_publish.py --init`).

---

## `board`

```yaml
board:
  publish_deadline_seconds: 30
  section_max_chars: 20000
  publish_on_owner_write: true
```

The board publisher ([board-schema.md](board-schema.md)). Whether it runs at
all is `DREAMER_PUBLISH_BOARD=1` in `postgres.env`, never this file, so a config
edit cannot turn publishing on by accident. `publish_deadline_seconds` is the
hard cap on one publish. `section_max_chars` truncates a longer conclusion
section and counts it. `publish_on_owner_write` makes the MCP server publish
right after a status change instead of waiting for the hourly run.

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

## `thread`

```yaml
thread:
  fold_per_run: 20
  fold_max_attempts: 5
  fold_queue_max: 80
  fold_queue_max_age_days: 7
  backfill_per_run: 60
```

The living-thread fold queue (rule 15; see
[architecture.md §9](architecture.md#9-the-living-thread)).

### `fold_per_run`

Default `20`. Fold passes per extraction run — one restricted model call each,
so this bounds a run's fold spend. Entries over the cap stay queued and drain
next run.

**How to choose:** above your typical nightly occurrence count, so the queue
drains to zero most nights. If the fold-queue health assertion starts firing,
raise this before touching the queue limits.

### `fold_max_attempts`

Default `5`. A fold that keeps failing is a defect, not work to re-buy every
night: after this many attempts the entry moves to
`vault/.vault-meta/fold-quarantine.json` with one `degraded` event.

### `fold_queue_max` and `fold_queue_max_age_days`

Defaults `80` and `7`. Thresholds for the fold-queue health assertion
(`degraded`): depth above the max means folds are enqueued faster than the
drain retires them; any entry older than the age limit means the drain has
stopped retiring it. Entries without an `enqueued_at` stamp count as fresh.

**How to choose:** leave them. They are alarms, not behaviour — the fix for a
firing alarm is `fold_per_run` or the quarantine list, not a bigger alarm.

### `backfill_per_run`

Default `60`. Drain cap for `bin/thread-backfill.sh`, the one-time drain that
builds initial threads for a pre-existing vault — counted in **fold passes**,
not loops. The key and the script are safe to delete once the backfill
reports drain complete.

---

## `health`

```yaml
health:
  checked_max_age_hours: 16
  detail_max_ids: 8
  embed_pending_max: 50
  index_stale_hours: 30
  untagged_window_days: 14
  cost_max_per_run: 50.00
  cost_window_days: 7
  gates: []
```

Thresholds for the health spine (see
[architecture.md §8](architecture.md#8-the-health-spine)). Every one is an
assertion input, never behaviour: changing a value changes when an alarm
fires, not what any job does.

### `checked_max_age_hours`

Default `16`. How stale the health record may get before the hourly watchdog
raises a `degraded` event. Set it to the longest quiet gap in your cron
schedule plus slack — in the stock schedule that gap runs from the 05:00
night cycle to the 18:30 ingest, so 16 covers it without false alarms.

### `detail_max_ids`

Default `8`. A frontier assertion's one-line detail names at most this many ids
and counts the rest, so a large backlog does not bury the digest event.

### `embed_pending_max`

Default `50`. Documents awaiting embedding before retrieval counts as
degraded — indexed-but-not-embedded is exactly the silent state the spine
exists to catch, because BM25 keeps returning rows while semantic search
quietly reads an older corpus.

### `index_stale_hours`

Default `30`. How old the search index may be before the assertion fires:
nightly reindex cadence plus slack. If you disable the nightly jobs, raise
this or the assertion fires permanently.

### `untagged_window_days`

Default `14`. The window for the vocabulary-applied check: once a tag
vocabulary is frozen, recently touched loops should be carrying tags. A
frozen vocabulary with a growing untagged population is the
"state changed, gating component never told" failure in its original form.

### `cost_max_per_run` and `cost_window_days`

Defaults `50.00` and `7`. A recorded run cost above the max, within the
window, fails the cost assertion. Set the max above any legitimate single
run; the window keeps an old expensive backfill from tripping the check
forever.

### `gates`

Default `[]`. Standing owner reminders, each
`{flag: <name>, text: "<what is still open>"}`, surfaced as `info` assertions
on every run. Close one by writing `vault/.vault-meta/gate-state.json` with
`{"<name>": true}`. Use a gate for anything you must not forget but a job
cannot check — an acceptance review you owe yourself, a migration you left
half-planned.

---

## `qmd`

```yaml
qmd:
  collections: [vault, transcripts, conclusions, wisdom, frontier]
```

### `collections`

The qmd collections `reindex` in `bin/_common.sh` refreshes every run — and
the same list the healthcheck's `collections-covered` and `index-fresh`
assertions hold that refresh to, so the config, the refresh and the assertion
cannot drift apart separately. A missing or empty list is loud: reindex logs
an error and records a `degraded` event rather than silently skipping.

`frontier` is the collection the knowledge frontier searches
(`vault/frontier`). Register it once in `.qmd/index.yml` (the example file shows
how) and build it with `qmd update && qmd embed`. Until it is registered, the
frontier's map and pre-mortem steps defer rather than treat missing retrieval as
"nothing found".

**How to choose:** leave it matching your qmd setup. Removing `wisdom` does
not disable the wisdom route — leave the list alone and let an unbuilt corpus
degrade the route honestly instead (rule 5a).

---

## `mcp`

```yaml
mcp:
  transport: stdio
  note_max_chars: 500
  max_resurfacings_per_run: 50
  owner_write_quote_min_chars: 8
  owner_write_verify_lag_days: 3
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

`owner_write_quote_min_chars` (default `8`) is the shortest `owner_quote` an
owner-write tool accepts: `commit_goal`, `revise_plan`, `complete_step`,
`set_status`, `assert_evidence`, `answer_question`, `tap_lesson` and `undo_tap`.
It is a floor against empty or one-word "quotes", not a check of truth; the
check of truth is the reconciliation below.

`owner_write_verify_lag_days` (default `3`) is how long a write may wait for its
quote to appear in an ingested Human turn. Ingestion is not instant (a Claude
Code session needs its quiet window, a claude.ai chat needs the next sync), so
the lag must exceed your slowest source. Past it the write is quarantined and
raised as a question, never reverted.

---

## `locks`

```yaml
locks:
  cron_wait_seconds: 120
```

How long a cron job that finds `wiki.lock` held waits before skipping its run.
A frontier apply holds the lock for seconds, so a job that starts inside one
should wait rather than lose its night. A missing key is fatal on purpose: a
silent fall-back to "do not wait" would bring the lost runs back. Raise it only
if your jobs routinely start while a long apply runs.

---

## `claude_ai_sync`

```yaml
claude_ai_sync:
  python:                       /usr/bin/python3
  host_name:                    com.dreamer.claude_ai_sync
  extension_id:                 ""
  due_hours:                    20
  retry_after_failure_hours:    3
  quiet_hours:                  6
  max_per_run:                  40
  spacing_seconds:              3
  page_size:                    50
  max_pages:                    200
  max_retries:                  4
  backoff_base_seconds:         5
  backoff_max_seconds:          120
  lock_wait_seconds:            2700
```

The opt-in claude.ai daily sync ([claude-ai-sync.md](claude-ai-sync.md)). Read
by the native host, the extension (through the host's status reply), and the
cron and install scripts. **`extension_id` is the switch.** Empty means the sync
is not set up and its freshness axes read `info` / `disabled`. Set it to the ID
Chrome shows for the unpacked extension on `chrome://extensions`, then run
`bin/install-sync-extension.sh`. Check the terms that apply to your claude.ai
account first.

| Key | Meaning |
|---|---|
| `python`, `host_name` | The interpreter the host runs under (Chrome starts it with no shell environment) and the native-messaging host name. The extension connects to the name in `extension/dreamer-sync/background.js`, so the installer refuses any other value |
| `due_hours` | A run is due when the last ok run is older than this |
| `retry_after_failure_hours` | After a failed attempt, the hourly tick waits this long before retrying |
| `quiet_hours` | Chats updated more recently than this are skipped, since they may be in progress; a later run takes them |
| `max_per_run`, `spacing_seconds` | Chats fetched per run (the rest wait as `pending`) and the minimum gap between chat requests. Together they set how politely the sync behaves; lower the first and raise the second if you see rate limits |
| `page_size`, `max_pages` | List paging: page size, and a safety bound on how many pages one run reads |
| `max_retries`, `backoff_base_seconds`, `backoff_max_seconds` | Retry policy for 429 and 5xx, per request |
| `lock_wait_seconds` | How long `sync-claude-ai.sh` waits for `wiki.lock` (an extraction in progress) before leaving the drops for the next run |

---

## `frontier`

The knowledge frontier has about eighty keys, almost all of them tuned
constants you should leave alone. This section groups them by purpose and
explains the ones that decide behaviour. Each key's comment in
`config.example.yaml` is the authoritative description, and every default below
is that file's. See
[architecture.md §16](architecture.md#16-the-knowledge-frontier) for how the
pieces fit.

### Map and retrieval

| Key | Default | Meaning |
|---|---|---|
| `map_batch_chars` | `80000` | Owner text per extraction batch, so one night maps one batch. Raise it to drain a backlog faster; each batch is one model call per step |
| `prompt_max_chars` | `120000` | Hard cap on any frontier prompt; retrieved neighbours are dropped lowest-ranked first |
| `match_top_k` | `5` | Neighbours retrieved per concept. Recall-tuned: the judge, not retrieval, decides |
| `match_chunk_concepts` | `40` | Concepts judged per match call |
| `max_items_per_transcript` | `8` | Learnable concepts kept per transcript, so a long chat does not flood the map |
| `parse_retries`, `max_parse_attempts` | `1`, `3` | A malformed reply is retried, then the chat is deferred; after this many deferrals it is skipped with a degraded event |
| `seed_loop_statuses` | `[open, researching, paused, decision-only]` | Unresolved loops seed a known-unknown item; archived and resolved ones do not |
| `owner_turn_max_chars`, `passage_window_paragraphs` | `3000`, `1` | A longer owner turn keeps its head and tail and marks the cut, since long turns are mostly pasted material; the paragraphs kept either side of a retrieval hit |
| `qmd_timeout_seconds`, `qmd_default_limit` | `20`, `10` | Timeout per qmd call, including the first call's model load. Past it the result is "degraded", never "empty". Raise the timeout on a slow machine |
| `map_ledger_file`, `lock_file`, `vault_lock_wait_seconds`, `cause_min_chars` | see file | Ledger and lock names under `paths.meta`; how long a frontier command waits for `wiki.lock`; the minimum length of a recorded undo cause |

### Gaps: the pre-mortem and the never-discussed check

| Key | Default | Meaning |
|---|---|---|
| `premortem_every_days` | `7` | A goal is due when its last pre-mortem is older than this, or its plan changed |
| `premortem_map_items`, `premortem_max_causes`, `max_candidates_per_cause` | `40`, `6`, `3` | Map items shown to the pre-mortem, failure causes accepted from one reply, and pieces of knowledge per cause |
| `check_top_k`, `check_title_lex_k` | `8`, `5` | Hits per collection per candidate by meaning, then by the title's words |
| `check_collections` | `[frontier, transcripts, vault, conclusions]` | Where to look for evidence you already met a candidate |
| `check_phrase_split_words`, `check_phrase_min_words`, `check_phrase_max_chats` | see file | Also search the title's own phrases in your turns. A keyword search ANDs a whole title, so a chat where you said one half of a compound title in passing would otherwise never be shown |
| `check_passage_chars`, `check_owner_chars`, `check_owner_paragraphs`, `check_owner_paragraph_chars` | `1500`, `1500`, `3`, `600` | How much text the judge sees. Your own paragraphs get a separate budget so long assistant replies never crowd your words out |
| `check_owner_min_score`, `check_owner_title_weight`, `check_owner_stem_chars`, `check_owner_min_word_chars`, `check_owner_stopwords` | see file | The word-overlap rule that picks your paragraphs for a candidate. Leave alone unless the check keeps missing obvious matches |
| `premortems_dir` | `premortems` | Records live under `paths.frontier/<this>/<goal>/<date>.md` |

### Lessons

| Key | Default | Meaning |
|---|---|---|
| `research_per_night` | `3` | Lessons researched per night, follow-ups first. This is the research budget; the release ceiling is separate |
| `max_queries_per_lesson`, `max_fetches_per_lesson` | `3`, `6` | Generic questions written per lesson, and web pages code will fetch. Refused pages count against the cap, and the two-domain rule needs two that load |
| `lesson_wisdom_hits`, `lesson_wisdom_window_lines`, `lesson_wisdom_window_chars`, `lesson_wisdom_anchor_chars` | `3`, `60`, `6000`, `40` | Library passages per lesson and how much text is read around each hit |
| `lesson_owner_sources`, `lesson_source_chars`, `lesson_max_claims` | `3`, `12000`, `12` | Earlier owner turns a lesson may cite, characters of each source shown to the claims call, and claims accepted per reply |
| `lesson_max_revisions` | `2` | Rewrites a rejected lesson gets, each judged afresh. Security rejections are never rewritten |
| `passage_min_run_tokens`, `passage_max_chars` | `8`, `400` | A quoted passage counts when this many normalised tokens appear as one run in its source; the kept span is cut to this length. Lower the first and a paraphrase could pass |
| `source_title_max_chars`, `shared_host_suffixes`, `instruction_patterns` | see file | Source labels, hosts where each subdomain is a different owner, and the patterns that mark text as an instruction to a model (rule 10) |
| `fetch_timeout_seconds`, `fetch_max_bytes`, `fetch_chunk_bytes`, `fetch_max_redirects`, `fetch_user_agent` | `15`, `1500000`, `16384`, `3`, see file | Limits on the code fetcher. Every redirect hop is re-checked |
| `research_ledger_file`, `pending_dir`, `research_approved_file` | see file | Run state, the holding directory for a lesson that found `wiki.lock` busy, and the approved-queries file for the gated call |

### Ceiling and release

| Key | Default | Meaning |
|---|---|---|
| `weekly_ceiling` | `3` | At most this many new lessons are released in any trailing window. Follow-ups are exempt, and a failed lesson frees its slot |
| `ceiling_window_days` | `7` | The trailing window |
| `air_deadline_days` | `7` | A released lesson not aired within this long becomes failed |
| `max_follow_ups_per_item` | `2` | Follow-up lessons one item may hold |
| `known_push_max` | `200` | Known-known titles sent to the consumer, newest evidence first |
| `mcp_max_items` | `20` | Items returned per `what_do_i_know` or `get_frontier` call |

**How to choose:** the ceiling is the dial that matters. It is a promise about
how much the system may ask of your attention, so set it to what you will
actually read and let a week with fewer be fine. `research_per_night` only
decides how fast the queue of candidates is worked through.

### The lesson consumer (the show)

| Key | Default | Meaning |
|---|---|---|
| `show_url` | `""` | Where lessons go. **Empty means off:** no token is read and no request is made |
| `allow_insecure_http` | `false` | The URL must be https, and loopback http is also accepted. Any other http needs this |
| `show_timeout_seconds` | `10` | Per request |
| `show_env_path` | `~/.config/dreamer/show.env` | Holds `DREAMER_LESSON_TOKEN` as `KEY=VALUE`. The env var `DREAMER_SHOW_ENV` overrides the path |
| `sync_state_file` | `frontier-sync.json` | Cursor, conflicts and known-list hash, under `paths.meta` |

The HTTP contract the consumer implements is in
[frontier-schema.md](frontier-schema.md). Dreamer starts every exchange.

### Egress

`frontier.egress` holds the web gate for the one research call
([architecture.md §17](architecture.md#17-model-call-modes-and-the-egress-gate)).

| Key | Default | Meaning |
|---|---|---|
| `dictionary_path` | `/usr/share/dict/words` | Word list for the name checks (capitalised tokens, and lowercase words the list only knows capitalised). A missing or empty file blocks every query (fail closed); on Debian or Ubuntu it comes from the `wamerican` package |
| `blocked_names_path` | `~/.config/dreamer/egress-blocked.txt` | Your names and third-party names, one per line, matched case-insensitively. A missing, unreadable or empty file blocks every query (fail closed), so create it before the first run |
| `allowed_proper_nouns` | `[]` | Capitalised terms that name no person or private entity. Add a term only when it is a product, place or field name |
| `phrase_run_words` | `4` | A run of this many words copied from the goal page or the item's evidence blocks the query |
| `year_min`, `year_max` | `1900`, `2100` | A 4-digit run counts as a year (allowed) only inside this range |
| `log_path` | `logs/frontier-egress.jsonl` | Every query, allowed or denied, one JSON line each |
| `dns_timeout_seconds` | `5` | A DNS lookup for a fetch gives up after this long (fail closed) |

**How to choose:** the two list files are the privacy setting. Put every name you
would not want in a search engine into `blocked_names_path`, and expect a few
harmless capitalised words to be dropped until you add them to
`allowed_proper_nouns`. Dropped is the safe direction: a failing query is never
rewritten.

### Publish

| Key | Default | Meaning |
|---|---|---|
| `publish_lock_wait_seconds` | `5` | How long `frontier_publish.py` waits for `wiki.lock` before skipping this hour silently |
| `publish_deadline_seconds` | `30` | Hard cap on one publish; the process ends |

Whether it runs at all is `DREAMER_PUBLISH_FRONTIER=1` in `postgres.env`.

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
| Fold-queue health assertion keeps firing | `thread.fold_per_run` | Raise it — the alarm thresholds (`fold_queue_max*`) are the last thing to touch |
| The same fold fails night after night | `thread.fold_max_attempts` | Check `fold-quarantine.json` first — a repeat failure is a defect, not a budget problem |
| Claude Code sessions you expected never appear | `cc_ingest.min_human_turns` / `min_human_chars` | Check the refusal reasons in `cc-ingested.json`, then lower the floor that refused them |
| Health record goes stale every day at the same time | `health.checked_max_age_hours` | Raise it past your schedule's longest quiet gap |
| Asked about goals you do not care to track, or never asked about ones you do | `goals.silence_ping_days`, `goals.movement_stale_days` | Raise to ask less often, lower to ask sooner |
| Asked "what is this for?" about loops that do not matter | `goals.drift_recurrence_min` | Raise it |
| An owner write was quarantined though you did say it | `mcp.owner_write_verify_lag_days` | Raise it past your slowest ingestion source |
| A job logs "vault is still locked" and skips | `locks.cron_wait_seconds` | Raise it past the longest apply |
| Freshness axes stale every morning | `freshness.job_max_age_hours` | Raise it to match your real schedule |
| Too many lessons, or not enough | `frontier.weekly_ceiling` | Set it to what you will read |
| Lessons are dropped for no usable source | `frontier.max_fetches_per_lesson`, `frontier.passage_min_run_tokens` | Raise fetches first; lower the run length only knowing a paraphrase could then pass |
| Every research query is blocked | `frontier.egress.blocked_names_path`, `dictionary_path` | A missing or empty list blocks everything by design; create or fix the file |
| Frontier map or pre-mortem keeps deferring | `frontier.qmd_timeout_seconds` | Raise it, and confirm the `frontier` collection is registered |
| Conclusions cite no books at all | not a config problem | `qmd` is not on `PATH` under cron, or `corpora.wisdom_sources` is unbuilt — check `logs/` for the `degraded` event |

For anything that is not a knob, see
[architecture.md § Where to look when something breaks](architecture.md#18-where-to-look-when-something-breaks).
