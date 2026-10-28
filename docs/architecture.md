# Architecture

For someone who wants to understand or change how Dreamer works. It assumes you
have read the [README](../README.md) and skimmed [CLAUDE.md](../CLAUDE.md).
Design notes referenced from the code live here.

The single organising idea: **the LLM decides *what*, Python decides *where the
bytes go*.** The model judges what counts as an unresolved thread, which
existing loop a new occurrence belongs to, and what a conclusion says. Every
state transition, file write, id assignment, decay calculation and citation
check is deterministic Python. That split is what makes the end-to-end test in
`tests/simulated_week.sh` meaningful — it substitutes only the model call
(`tests/fake_claude.py`) and still exercises the real state machine, decay
clock, digest, lint and git commits.

---

## 1. Data flow, end to end

```
claude.ai export (zip / dir / conversations.json)
      │  scripts/convert_claude_export.py      ← redaction happens HERE, pre-write
      ▼
vault/sources/transcripts/YYYY/MM/DATE--slug.md         (immutable provenance)
      │  scripts/make_batch.py                 ← checkpointed by extracted.json
      ▼
prompt (skills/extract/PROMPT.md + batch)
      │  bin/_common.sh :: run_claude           ← the LLM half
      ▼
extraction JSON  { candidates: [...], skipped: [...] }
      │  scripts/apply_extraction.py            ← the deterministic half
      ▼
vault/loops/L####.md     ── regenerated ──▶  vault/loops/_catalog.md
      │  scripts/vault.py select              ← recurrence_min + recency rank + rule 14 gate
      ▼
selected loop
      │  scripts/make_dream_prompt.py (skills/dream/PROMPT.md)
      │  bin/_common.sh :: run_claude           ← routing + research happen inside this call
      ▼
dream JSON  { action | route, sections, claims+citations, web_queries, ... }
      │  scripts/apply_conclusion.py           ← citation/grade/quarantine enforcement
      ▼
vault/conclusions/YYYY-MM-DD--slug.md   +   loop → paused (or decision-only)
      │  scripts/grade_conclusions.py (structural rubric)
      │  scripts/merge_proposals.py refresh
      │  scripts/digest.py build
      ▼
vault/digests/YYYY-WW.md   +   vault/dashboard.html
```

Step by step:

| Step | Script | What it actually does |
|---|---|---|
| Ingest | `scripts/convert_claude_export.py` | Reads the real export schema (`chat_messages[]`, with `content[]` blocks — 104 messages in a real 887-conversation dump had empty `text` but populated `content`). Writes one Markdown transcript per conversation. Runs `dreamer_common.redact()` **before the first disk write**, because `vault/sources/` is immutable and committed. Maintains a dedupe ledger so re-ingesting an overlapping export is a no-op. |
| Batch | `scripts/make_batch.py` | Picks the next N un-extracted transcripts **oldest first**, so recurrence accretes in the order it happened and Stage A matches against the loop set as it existed then. Checkpoint is `vault/.vault-meta/extracted.json`; `--mark-batch` is called only *after* a successful apply, so a failed apply is retryable. |
| Extract + match | `skills/extract/PROMPT.md` via `run_claude` | Applies the meta-idea filter (rule 6) and the two-stage matching procedure (rule 7). Emits JSON only. |
| Apply | `scripts/apply_extraction.py` | Creates or updates loops, records every Stage-B decision to `matching-decisions.json`, drains `vault/inbox/resurfacings/`. One bad candidate is rejected and logged; the rest of the night still lands. |
| Select | `scripts/vault.py select` | `status == open` **and** `recurrence_count >= matching.recurrence_min`, then `reresearch_gate()` (rule 14), then sort by `recency_weighted_score()`. Loops filtered out by the gate are staged as `served` for the digest. |
| Route + research | `skills/dream/PROMPT.md` via `run_claude`, one call **per loop** | Routing (rule 5), empty-corpus escalation (rule 5a) and the serve/re-research judgment (rule 14) all happen inside this single call. One call per loop means a usage-limit exit costs one loop, not the week. |
| Apply conclusion | `scripts/apply_conclusion.py` | Drops uncited claims, caps and quarantines derived citations, quarantines web text under the untrusted heading, writes the page, links supersession both ways, transitions the loop. |
| Grade | `scripts/grade_conclusions.py` | Structural rubric only. Score < 0.7 raises a digest warning. |
| Report | `scripts/digest.py`, `scripts/dashboard.py` | Digest = "what happened this week, what needs you". Dashboard = "what shape is the vault in right now". |

Four side paths hang off this spine, each described in its own section below:
goals (§11), freshness and heartbeats (§12), the opt-in publishers (§13) and the
knowledge frontier (§16). The solution spine (§14) shapes the conclusion step,
and §15 covers the extra ingestion sources.

### Where routing actually lives

There is no `route.py`. Routing is a decision the dream prompt makes and reports
in its JSON; `apply_conclusion.py` validates it against
`VALID_ROUTES = {wisdom, web, past-reasoning, decision-only, mixed}`, defaults
an invalid value to `decision-only`, and records a problem if a `decision-only`
route reported any `web_queries` or `fetched_urls` — the rule 5 contract
assertion.

---

## 2. Loop schema and state machine

Every loop is `vault/loops/L####.md`. Frontmatter is written by
`vault.Loop.frontmatter()`:

| Field | Type | Notes |
|---|---|---|
| `type` | `loop` | `loop-redirect` marks a merge stub; `load_loops()` skips those. |
| `id` | `L0042` | Assigned by `vault.next_loop_id()`, monotonic across `loops/` **and** `archive/` so an id is never reused. Never invent one. |
| `status` | enum | `open \| researching \| paused \| decision-only \| archived`. `Loop.save()` refuses anything else. |
| `title` | string | Canonical restatement of the thread. This is what the catalog and the merge judge see. |
| `created` | date | When Dreamer first wrote the page. |
| `first_seen` | date | Earliest occurrence — may be historical, from a backfill. |
| `last_seen` | date | Most recent occurrence. Feeds the decay clock. |
| `recurrence_count` | int | **Defined as** `len(set(occurrences))`, not a free-running counter. Lint fails if the two disagree. |
| `occurrences` | list of wikilinks | Kept sorted chronologically (`_occurrence_sort_key`); undated ones sort last, never dropped. |
| `route` | enum | Set when the loop is researched. |
| `conclusion` | wikilink | Set only once a conclusion page exists. Its filename date drives the rule 14 cooldown. |
| `tags` | list | Controlled vocabulary only (rule 4); empty until `vault/.vault-meta/tag-vocabulary.json` exists. |

### Transitions

```
                 new occurrence, no match
                          │
                          ▼
   ┌──── reopening ───▶ open ────── weekly selection ─────▶ researching
   │                   │  │                                     │
   │                   │  └── router: decision-only ──┐    conclusion written
   │                   │                              │          │
   │              decay (DECAY_WEEKS)                 ▼          ▼
   │                   │                       decision-only   paused
   │                   ▼                              │          │
   └───────────────  archived ◀── decay (× terminal_multiplier) ─┘
```

| From | To | Trigger | Enforced in |
|---|---|---|---|
| *(new)* | `open` | Extraction finds a thread matching no existing loop | `vault.create_loop()` |
| `open` | `open` | New transcript matches: append occurrence, bump `last_seen` | `vault.add_occurrence()` |
| `paused` / `decision-only` / `archived` | `open` | Topic resurfaces (reopening rule) | `vault.add_occurrence()` — also moves the page back out of `archive/` |
| `open` | `researching` | Weekly dream selects it | `bin/weekly-dream.sh`, before the model call |
| `researching` | `paused` | Conclusion written and linked | `apply_conclusion.apply()` |
| `open` / `researching` | `decision-only` | Router decided only an owner decision resolves it | `apply_conclusion.apply()` |
| `open` | `archived` | Decay after `DECAY_WEEKS` | `vault.run_decay()` |
| `paused` / `decision-only` | `archived` | Decay after `DECAY_WEEKS × terminal_multiplier` | `vault.run_decay()` |
| `researching` | `open` | **Recovery**, not a spec transition: a usage-limit exit or an unparseable reply left the loop in flight | `vault.recover_stranded()` and the `_extract_json is None` path in `apply_conclusion.main()` |

That last row matters. `researching` never decays (rule 3) and selection reads
only `open`, so a stranded loop is invisible forever. Two independent
recoveries exist: `weekly-dream.sh` calls `vault.py recover` at the top of
every run, and `apply_conclusion.py` resets the loop itself when the model
answers in prose instead of JSON.

### Decay

`Loop.decay_deadline()` is the whole rule:

```python
anchor = max(last_seen or first_seen or go_live, go_live_date)
weeks  = decay_weeks * (terminal_multiplier if status in {paused, decision-only} else 1)
deadline = anchor + weeks
```

`go_live_date is None` → returns `None` → decay is inert. `archived` and
`researching` also return `None`.

---

## 3. Vault layout

```
vault/
├── sources/
│   ├── transcripts/YYYY/MM/YYYY-MM-DD--slug.md   immutable; never modified (rule 8)
│   └── resurfacings/                             applied MCP notes, kept as provenance
├── inbox/
│   ├── *.zip, *.json                             raw exports awaiting conversion
│   └── resurfacings/                             QUEUE: the MCP server's only write target
├── loops/
│   ├── L0001.md …                                one page per tracked thread
│   └── _catalog.md                               regenerated; read FIRST (rule 11)
├── conclusions/YYYY-MM-DD--slug.md               cited, graded research output
├── concepts/                                     reserved for distilled concept pages
├── archive/L####.md                              decayed loops and merge stubs
├── digests/
│   ├── YYYY-WW.md                                the weekly read
│   ├── pending.json                              cross-job staging (see below)
│   └── recurrence-histogram.md, calibration-sample.md
├── .vault-meta/                                  machine state, not for reading
│   ├── wiki.lock                                 flock target
│   ├── extracted.json          transcripts already through extraction
│   ├── ingested.json           conversion dedupe ledger + dates (drives freshness)
│   ├── run-state.json          events, deferrals, costs, skip_research_next_run
│   ├── matching-decisions.json Stage-B decisions awaiting digest sampling
│   ├── matching-feedback.json  the owner's ✓/✗ marks, read back from digests
│   ├── merge-proposals.json    live proposals
│   ├── merge-judgments.json    the judgment cache
│   ├── golden-set.json         owner-labelled matching pairs
│   ├── quarantine.json         batches whose apply failed
│   ├── tag-vocabulary.json     frozen vocabulary; absent = no tags at all
│   └── mcp-access.log          which MCP tool was called when
└── dashboard.html                                gitignored, regenerated per commit
```

`vault/**` is gitignored in this repo. If you want your vault versioned —
recommended — point `paths.vault` at a directory in a **separate private repo**
(see [configuration.md](configuration.md#paths)).

### pending.json

Three jobs contribute to one digest but only Job 2 writes it, and `decay-archive`
runs *before* `weekly-dream`. So every job stages items with
`digest.stage(kind, item)` into `vault/digests/pending.json`; `digest.build()`
consumes and deletes it. Kinds in use: `conclusions`, `served`,
`derived_citations`, `quality`, `archived`, `web_queries`, `proposed_tags`,
`merge-expired`, `events`.

---

## 4. Script inventory

| Script | One line |
|---|---|
| `dreamer_common.py` | Config loading, path resolution, `atomic_write`, secret redaction, frontmatter read/write, `today()`. Every vault-touching module imports it. |
| `vault.py` | The `Loop` model, state machine, catalog, decay, ranking, selection, `reresearch_gate`, merge arithmetic, lint. CLI: `catalog \| lint \| decay \| select \| recover`. |
| `convert_claude_export.py` | Export → one redacted Markdown transcript per conversation, with a dedupe ledger. |
| `make_batch.py` | Next un-extracted batch + rendered extraction prompt; owns the extraction checkpoint. `--count-remaining`, `--mark-batch`. |
| `apply_extraction.py` | Applies extraction JSON: create/match loops, intra-batch duplicate safety net, record decisions, drain resurfacings. |
| `make_dream_prompt.py` | Renders the per-loop research prompt from `skills/dream/PROMPT.md` + loop state + any existing conclusion (injected as a hypothesis, rule 14). |
| `apply_conclusion.py` | Applies dream JSON: enforce citations, cap/quarantine derived claims, quarantine web text, write the conclusion, link supersession, transition the loop. Handles `action: "serve"`. |
| `grade_conclusions.py` | 12-check structural rubric over conclusion pages; reports re-run variance and generation-over-generation regressions. |
| `merge_proposals.py` | Detects near-duplicate loops (lexical band + qmd embedding neighbours), judges with the LLM, caches verdicts, expires proposals, applies confirmed merges. CLI: `refresh \| list \| confirm`. |
| `golden_set.py` | Replays owner-labelled loop pairs through the Stage-B judge; also exports `judge_llm()`, which `merge_proposals` reuses. `validate \| run --judge llm\|lexical`. |
| `digest.py` | Builds the weekly digest, reads ✓/✗ marks back, computes rolling matching precision, owns `stage()`/`pending.json`. |
| `dashboard.py` | Read-only HTML projection of live vault state. `--json`, `--serve [PORT]`. No locks, no writes outside its own output. |
| `calibrate.py` | Recurrence histogram, 25-loop review sample, golden-set scaffold. Run this before trusting `recurrence_min`. |
| `probe_recall.py` | Retrieval recall gate using the owner's *raw* transcript openings as probes, not hand-written ones. |
| `propose_tags.py` | Deterministic tag-vocabulary proposal; `freeze` writes `tag-vocabulary.json`, after which lint enforces it. |
| `build_wisdom_corpus.py` | Converts a mixed library (txt / epub / pdf / doc) into `wisdom_md/` for qmd. Source files are read-only. |
| `healthcheck.py` | The assertion registry: mechanical state comparisons, three severities, leg blocking, `--watchdog` mode. See §8. No LLM calls, no mutations of what it checks. |
| `fold_pending.py` | The living-thread fold queue: enqueue on new occurrences, batch for the drain, quarantine repeat failures. See §9. |
| `apply_thread.py` | Applies one fold result deterministically: replaces only the Thread section, validates citations against the loop's occurrence list, frontmatter byte-identical. |
| `apply_tags.py` | Applies vocabulary-validated tags; rejects anything outside `tag-vocabulary.json` (rule 4). Backs `bin/tag-backfill.sh`. |
| `convert_cc_sessions.py` | Claude Code session triage + summariser prompt: entrypoint/turn/quiet-hours filters, code-fence collapsing, the `cc-ingested.json` ledger. See §10. |
| `apply_cc_session.py` | Writes one summarised session as a transcript page — or refuses and writes nothing if the page still carries a code fence, a path, or anything `redact()` would have caught. |
| `dreamer_mcp.py` | The MCP server. See §7. |
| `goals.py` | The goal model, state machine, catalog, plan revision and the silence, stale and drift detectors. See §11. |
| `questions.py` | The open-questions queue and owner-write reconciliation. Every quarantine has an owner-facing exit. |
| `apply_assessment.py` | Applies the assessor's output: verified attachments, clocks, questions, assessment folds. |
| `spine.py` | The problem-type to stage-depth table and the check that a conclusion page honours it. See §14. |
| `heartbeat.py`, `freshness.py` | Job heartbeats and per-axis freshness verdicts. Pure readers apart from the heartbeat write. See §12. |
| `freshness_publish.py`, `board_publish.py`, `frontier_publish.py`, `publish_trigger.py` | The opt-in Postgres publishers and the trigger that runs them after an owner write. See §13. |
| `claude_ai_host.py`, `convert_desktop_sessions.py` | The native-messaging host for the claude.ai sync, and the desktop-session front end for the Claude Code converter. See §15. |
| `frontier.py` | The frontier page store and the one quadrant rule. See §16. |
| `frontier_map.py`, `frontier_gaps.py`, `frontier_lessons.py`, `frontier_sync.py` | Map builder, pre-mortem and never-discussed check, lesson research and composition, and the exchange with the lesson consumer. |
| `frontier_llm.py`, `frontier_eval.py` | The sealed model calls with their strict parsers, and the replay harness for their prompts. |
| `owner_text.py`, `qmd_client.py` | Reads only the owner's own turns out of a transcript; a warm qmd client whose failure reads as "cannot judge", never "nothing found". |
| `egress_check.py`, `egress_hook.py`, `egress_results_hook.py`, `verify_passages.py` | The query gate, the hooks that enforce it inside the research call, and the fetcher and passage verifier. See §17. |

---

## 5. The job harness

Every wrapper in `bin/` sources `bin/_common.sh`, which is the only place jobs
touch the model, the lock, git or run state.

| Function | Contract |
|---|---|
| `acquire_lock` | `flock` on `vault/.vault-meta/wiki.lock`, waiting up to `locks.cron_wait_seconds` first (a frontier apply holds it for seconds, so a job that starts inside one waits instead of losing its night). **Failure exits 0**, not 1: an overrunning job is a normal outcome and the successor should log a clean skip, not a failure. |
| `acquire_frontier_lock` | Non-blocking `flock` on `frontier.lock` (`frontier.lock_file`), held for a whole frontier run. Keeps the nightly pass and the backfill from overlapping without making the rest of the system wait behind a model call. |
| `arm_heartbeat` | Installs the exit trap that records the job's heartbeat (§12). Every wrapper calls it. |
| `run_claude <prompt> <max_turns> <out> [restricted\|gated]` | Three tool modes, described in §17. Wraps `claude -p --output-format json`, pins `--model` from `budget.model`, and allowlists exactly `Bash(qmd:*)`, `mcp__qmd`, `WebSearch`, `WebFetch`. Unwraps `.result` into `<out>` and `total_cost_usd` into `<out>.cost`. **A non-zero exit is a deferral, not a failure**: it is logged, recorded, and returns non-zero so the caller can stop cleanly with work resumable. `DREAMER_FAKE_CLAUDE` substitutes a deterministic script for tests. |
| `record_event kind detail` | Appends to `run-state.json → events[]`; the next digest renders these under "What happened this week". |
| `record_deferral code` | Appends to `run-state.json → deferrals[]`. |
| `record_cost file` | Always records cost. Only *gates* if `budget.cost_ceiling_per_run` is set, and then latches `skip_research_next_run` — a latch, not an assignment, because one cheap loop must not clear a breach an expensive one set. |
| `should_skip_research` / `clear_skip_research` | Read and clear that latch. Weekly-dream honours it, writes a quiet-week digest saying so, then clears it. |
| `reindex` | `qmd update` for `vault`, `transcripts`, `conclusions`. Missing `qmd` is **loud**: it logs an error and records a `degraded` event, because a silently dead wisdom route produced a whole night of book-free conclusions once. `_common.sh` also prepends every `~/.nvm/versions/node/*/bin` to `PATH`, since cron has no login shell. |
| `regen_catalog` | `vault.py catalog`. |
| `regen_dashboard` | Best-effort; a broken chart never fails the run that produced it. |
| `commit "msg"` | `git add -- vault logs` — **only** the job surface — then commit as `dreamer <dreamer@localhost>`, then regenerate the dashboard. |

Two details worth keeping when you modify this:

**Why `git add -- vault logs` and not `-A`.** Jobs run at 02:00 but also
resume in-session while you are editing code. `git add -A` swept half-finished
script edits into commits named `nightly-extract: created=3`. Vault pages and
logs are the entire job surface; everything else in the tree is yours.
(In a stock clone both paths are gitignored, so those commits are no-ops until
you version the vault yourself.)

**Why atomic writes rather than relying on the lock.** `wiki.lock` is
*advisory*: it stops other Dreamer jobs, not `dreamer_mcp.py`, `dashboard.py`,
Obsidian, or grep. The real guarantee comes from the writer side —
`dreamer_common.atomic_write()` writes a `.tmp-*.part` file in the same
directory, `fsync`s, then `os.replace()`s. On POSIX that is atomic, so any
reader sees the old file or the new one, never a torn one. This is why the
dashboard can run on a timer with no lock at all.

### Jobs

| Job | Schedule | Sequence |
|---|---|---|
| `sync-claude-ai.sh` | 18:00, opt-in | Wait for `wiki.lock`, convert the drops the browser extension left in `to_ingest/` (§15). Inert until `claude_ai_sync.extension_id` is set. |
| `ingest-cc.sh` | 18:30, ahead of extraction | Sweep Claude Code sessions and Claude desktop agent sessions → triage → summarise → apply as transcripts (§10, §15). Takes no vault lock — conversion is append-only and atomic — but is scheduled clear of the others anyway. |
| `nightly-extract.sh` | 19:00 | healthcheck → convert inbox → batch → extract → apply → mark batch → drain the fold queue (§9) → reindex → catalog → commit. Empty batch is a near-zero-cost no-op that still drains queued resurfacings and folds. |
| `nightly-assess.sh` | 19:00, chained after extraction | healthcheck → settle owner-write verification → (no active goals: clean no-op) → assess the next unassessed transcripts → apply attachments → one restricted fold per applied attachment → catalog → commit (§11). |
| `frontier.sh` | 19:00, chained after the assessor | healthcheck → under `frontier.lock`: pull taps → map one batch → pre-mortems → lessons → push within the ceiling → publish → commit (§16). |
| `frontier-backfill.sh [N]` | once, manual | Maps the existing corpus, up to N batches or until drained. Resumable. |
| `weekly-dream.sh` | the night-cycle dream leg | ingest marks → recover stranded → cost-ceiling check → select → **per-loop**: prompt, mark `researching`, research, apply, grade, commit → merge refresh → lint → digest → reindex → catalog → commit. |
| `decay-archive.sh` | Sunday 19:30, before the night window opens | `run_decay()`, stage `archived` items into `pending.json`, catalog, commit. |
| `backfill.sh [N]` | the night-cycle backfill leg, or manual | Up to N extraction batches, resuming from the checkpoint. A failed apply quarantines the batch by name and continues, rather than stalling the queue forever on one bad transcript. |
| `night-cycle.sh [N]` | 20:00 / 23:00 / 02:00 / 05:00 | healthcheck → backfill leg → healthcheck again (the backfill leg mutates the corpus mid-cycle) → dream leg, in **one** wrapper — the legs share the advisory lock, so co-scheduling them would starve whichever started second. A `blocking` health assertion stops only the leg it names (§8). |
| `ingest.sh` | manual | Standardised drop point: put an export in `to_ingest/`, run with no arguments. Deliberately does **not** take the lock — conversion is append-only and atomic. |
| `tag-backfill.sh` / `thread-backfill.sh` | once, manual | One-time initialization drains for pre-existing vaults (§9 and rule 15's carve-out). Safe to delete once they report complete. |
| `verify.sh` | manual | Acceptance sweep: reindex, catalog, lint, unit tests, `probe_recall.py` gate, conclusion rubric, golden set (`--skip-golden` omits the only paid gate). |
| `dashboard.sh` | manual | `--open` / `--serve`. Sources nothing: no lock, no commit. |
| `install-cron.sh` | once | Installs the block idempotently, with an explicit `PATH` for node/qmd. |
| `install-sync-extension.sh` | once, opt-in | Validates `claude_ai_sync.extension_id` and writes the browser's native-messaging manifest, allowing exactly that one extension. |
| `freshness-setup.sh` | once, opt-in | Creates the publisher database if needed, publishes one snapshot and reads it back. |

---

## 6. Matching and merges

### Two-stage matching (extraction time)

**Stage A — candidates.** The prompt reads `vault/loops/_catalog.md` (one line
per loop: id, title, status, count, last_seen) and picks up to
`matching.stage_a_top_k` plausible candidates *by title alone*. Full loop pages
are opened only for those. Rule 11 exists because a capable tool-using agent
will otherwise infer page paths directly and read everything, which is exactly
the cost the catalog is there to avoid.

**Stage B — judge.** For each candidate pair: same underlying loop, or distinct?
On genuine uncertainty, **create a new loop**. Every decision is recorded with a
one-line justification into `matching-decisions.json`;
`digest.sample_decisions()` puts ten unmarked ones in the weekly digest as
checkboxes, and `digest.ingest_marks()` reads your ✓/✗ back into
`matching-feedback.json` on the next run. `rolling_precision()` reports the last
`matching.precision_window` marks and flags anything under
`matching.precision_floor`.

One deterministic backstop lives in `apply_extraction.py`: two candidates in the
*same batch* are structurally invisible to each other, because `_catalog.md`
cannot list a loop that does not exist yet. So a `new` candidate whose title
overlaps an earlier candidate in the same payload at `>= 0.90` is attached
instead of split (`INTRA_BATCH_THRESHOLD`). That threshold is far above the
merge band on purpose — 0.90 identical titles is not "genuine uncertainty", it
is the same question twice.

### Merge proposals (weekly)

The conservative-split bias is only self-healing if something offers the split
back. `merge_proposals.refresh()`:

1. **Candidates.** All pairs of `open`/`paused`/`decision-only` loops with title
   Jaccard `>= CANDIDATE_FLOOR` (0.05 — deliberately wide), *plus* qmd vector
   neighbours of each loop title, which get a `+1.0` score offset so semantic
   neighbours are always judged before lexical-only pairs. The old 0.55 cut
   recalled 2 of 9 owner-confirmed duplicates; 0.05 plus embeddings recalls 9/9
   while admitting 3 of 17 distinct pairs. A missed candidate is a permanent
   duplicate; an extra one costs one judge call.
2. **Filter, then cap.** Already-proposed and cache-hit pairs are removed
   *before* `MAX_JUDGED` (`matching.max_judged_per_run`) truncates the queue —
   otherwise they consumed judge slots and the queue never drained. The overflow
   is logged as "remain queued", never silently dropped.
3. **Judge.** `golden_set.judge_llm(title_a, title_b)`. A judge *outage* is not
   a `distinct` verdict: it falls back to the deterministic 0.55 token rule,
   increments `judge_errors`, and surfaces in the digest — a run that judged
   nothing must not read as a run that found nothing.
4. **Cache.** See below.
5. **Expire.** A proposal you never confirmed is re-proposed once, then recorded
   as `expired` (never `rejected` — expiry means you did not look) with a
   `merge_proposal_expiry_weeks` cooldown, and the expiry itself is staged to
   the digest.

`confirm --keep --retire` calls `vault.merge_loops()`: occurrence **union**,
`recurrence_count = len(set(union))` (not max, not sum), earliest `first_seen`,
latest `last_seen`, tag union, and a `type: loop-redirect` stub written to
**both** `loops/<retired>.md` and `archive/<retired>.md` so `[[loops/L0037]]`
keeps resolving.

#### The judgment cache

`vault/.vault-meta/merge-judgments.json`, keyed by `_pair_key()` = the two ids
sorted (order-canonical, because keep/retire flips as counts change).

Each entry stores a verdict plus a `(title_hash, evidence_hash)` fingerprint.
`_cache_skips()` reuses a verdict **only** if both hashes still match:

- **titles change** → the judge saw different text, verdict void;
- **either loop gains an occurrence** → `evidence_hash` changes, verdict void.

That second invalidation is the important one. Rule 7 justifies over-splitting
by promising a false split self-heals as both loops accrue occurrences; a
permanently cached `distinct` would retire that promise. `same` verdicts are not
suppressed by the cache at all — the live proposal suppresses them. `error`
verdicts are never cached. Confirming a merge deletes every cached entry naming
the retired id.

Without this cache, deterministic scoring re-selected and re-paid for the same
top-40 pairs roughly four times a night while rank 41+ was never judged.

---

## 7. The MCP server

`scripts/dreamer_mcp.py` is the only long-lived custom component: it reads vault
files, shells out to `qmd`, and holds no state.

**Transport.** Newline-delimited JSON-RPC 2.0 on stdio (`main()` reads
`sys.stdin` line by line). There is no TCP listener, therefore no bearer token
to manage and no local-process attack surface. Protocol version `2025-06-18`.
Wire it up with `.mcp.json.example`.

**Tools.**

| Tool | Returns | Notes |
|---|---|---|
| `search_insights(query, include_archived)` | Ranked loop summaries | Score **fusion**, not concatenation: IDF-weighted lexical scoring over loop titles/bodies/tags (titles ×3) normalised to 1.0, plus the strongest qmd evidence per loop from the `vault`, `transcripts` (mapped back via occurrence links) and `conclusions` collections. Conclusion hits are discounted ×0.7 — rule 13, so Dreamer's own output cannot rank as its own confirmation. Empty result returns an explicit "nothing tracked, reason from scratch" note. |
| `get_loop(id_or_title)` | Full page + occurrences + conclusion text | Falls back id → exact title → substring. |
| `list_open_loops(tag, min_recurrence)` | Open loops by recency-weighted rank | |
| `get_latest_digest()` | Newest `digests/YYYY-WW.md` verbatim | Checkbox lines are preserved so you can mark them in the client. |
| `search_wisdom(query)` | qmd hits from the `wisdom` collection | Empty result returns a note telling the caller to **say the library has nothing**, not to cite a near-miss. |
| `log_resurfacing(loop_id, note)` | Queued filename | The only write among the tools above. |
| `list_active_goals`, `get_goal`, `list_open_questions` | Goal summaries, one goal's page, the open-questions queue | Read-only. |
| `commit_goal`, `revise_plan`, `complete_step`, `set_status`, `assert_evidence`, `answer_question` | A confirmation, sometimes with a question for the owner | Owner writes (§11). Each takes `owner_quote` and `session_id`. |
| `what_do_i_know(topic)`, `get_frontier(goal)`, `get_lesson(lesson_id)` | Map matches, one goal's frontier, one lesson | Read-only (§16). Lists are capped at `frontier.mcp_max_items`, newest evidence first. |
| `tap_lesson`, `undo_tap` | A confirmation | Owner writes (§16). Each takes `owner_quote` and `session_id`. |

**Retrieval trade-off.** `_qmd_query(deep=False)` runs `qmd search` (BM25,
~0.4 s, 12 s timeout) for interactive calls; `deep=True` runs `qmd query`
(hybrid + rerank, ~20 s, 60 s timeout) and applies `QMD_RELEVANCE_FLOOR = 0.88`.
The floor exists because the reranked path *always* returns a rank-1 result and
emits exactly 0.88 when nothing genuinely matches — a confidently cited, wrong
answer. BM25 needs no floor: it honestly returns zero rows.

**The write boundary.** The server has two kinds of write, and they are kept
apart. `tool_log_resurfacing` writes one file into `vault/inbox/resurfacings/`
and nothing else. Loop mutation happens that night,
in `apply_extraction.apply_resurfacings()`, through the same
`add_occurrence()` path a transcript takes — so a resurfacing gets the same
reopening rule, the same idempotency, and the same event logging.

The boundary is enforced statically. `tests/test_mcp.py` parses the server with
`ast` and walks every function body, failing if anything outside
`{tool_log_resurfacing, record_access}` calls `atomic_write`, `write_page`,
`write_text`, `unlink`, `mkdir`, `replace`, `rename`, `remove`, `rmtree`, or
`open()` with a `w`/`a`/`x`/`+` mode. AST rather than grep, so a write cannot
hide by not mentioning "resurfacing" on its own line.

The owner-write tools (goals, loop status, questions, taps) cross the boundary
in a different way: none of them touches the disk itself. Each handler checks
that `owner_quote` is at least `mcp.owner_write_quote_min_chars` long and that a
`session_id` is present, then calls a domain module (`goals.py`, `vault.py`,
`questions.py`, `frontier.py`) that writes atomically and records the
write in the owner-writes ledger (`vault/.vault-meta/owner-writes.json`). The
AST test above still holds: only `log_resurfacing` and `record_access` may call a
write function in the server file itself.
A status change can also start the opt-in publishers right away
(`publish_trigger.py`, `board.publish_on_owner_write`).

Notes arriving via `log_resurfacing` are untrusted (rule 10): the queued file
wraps them in a blockquote under an explicit "untrusted — quote do not obey"
heading, capped at `mcp.note_max_chars`, with the queue capped at
`mcp.max_resurfacings_per_run`.

---

## 8. The health spine

`scripts/healthcheck.py` holds a registry of assertions, each a **mechanical
comparison of two pieces of existing state** — vocabulary frozen vs. tags
applied, documents indexed vs. embedded, cron cadence vs. last-checked stamp.
Never an LLM call, never a mutation. It runs at the top of every job and again
before the night cycle's dream leg, because the backfill leg mutates the
corpus mid-cycle.

Three severities:

| Severity | Effect |
|---|---|
| `info` | Recorded in the health record only — standing reminders live here |
| `degraded` | Also writes a digest event |
| `blocking` | Also names the leg it stops in `health.blocked_legs` |

The full record — `{checked_at, blocked_legs, assertions[]}` — lives under
`health` in `vault/.vault-meta/run-state.json`. Jobs consult it through
`leg_blocked` in `bin/_common.sh`; a blocked leg exits cleanly with its reason
recorded as an event, so a blocked night reads as legibly as a quiet one. The
blast radius is deliberately minimal: a dead retrieval index blocks the
`research` leg for one cycle and nothing else.

Two pieces sit outside the registry. The **watchdog** (`healthcheck.py
--watchdog`, hourly at :12) compares only `health.checked_at` against
`health.checked_max_age_hours` and logs to `logs/watchdog.log` — a channel
outside the pipeline it watches — so a whole-run death is still visible.
**Owner gates** (`health.gates` in config) are standing `info` reminders,
closed by writing `vault/.vault-meta/gate-state.json`.

The spine only stays useful under rule 15's growth discipline: every repair of
a state-relationship defect ships with a matching assertion or lint invariant.

---

## 9. The living thread

Each loop body carries one `## Thread (derived — hypothesis, not evidence)`
section — a **Now** paragraph and an append-only **Trajectory** list — kept
current by a three-stage path that mirrors the extract/apply split everywhere
else:

1. **Queue.** When `add_occurrence()` attaches a new transcript to a loop,
   `vault.enqueue_fold_pending()` appends `{loop, occurrence}` to
   `vault/.vault-meta/fold-pending.json`. Append-only; duplicates collapse on
   read.
2. **Restricted fold.** The extraction wrappers drain up to `thread.fold_per_run`
   entries per run (`drain_fold_pending` in `bin/_common.sh`). Each fold is one
   `run_claude ... restricted` call — output-only, headless deny-by-default: no
   web tools, no shell, no file tools — rendering `skills/thread-fold/PROMPT.md`
   over the current thread plus the **one** new occurrence, never the loop's
   accumulated history.
3. **Deterministic applier.** `scripts/apply_thread.py` replaces only the
   Thread section, validates every citation against the loop's own occurrence
   list, and leaves frontmatter byte-identical.

The thread is derived tier (rule 13): its citations carry a `via thread`
marker that `apply_conclusion.py` grades as derived, capping any claim copied
from a thread at `contested` until re-derived from primary sources. The dream
prompt receives the thread as a hypothesis to re-test, after the primary
occurrences.

Failure handling is loud, not looping: an entry that fails
`thread.fold_max_attempts` times moves to `fold-quarantine.json` with a
`degraded` event, and queue depth/age have their own health assertion
(`thread.fold_queue_max`, `thread.fold_queue_max_age_days`).
`bin/thread-backfill.sh` is the one-time drain that builds initial threads for
a pre-existing vault, oldest-first — the only path allowed to touch `paused`
and `decision-only` pages (rule 15's carve-out).

---

## 10. Claude Code session ingestion

`bin/ingest-cc.sh` (18:30, before extraction so its output is in that night's
queue) sweeps `corpora.claude_code_sessions` — one `.jsonl` per session under
`~/.claude/projects` — and lands ordinary transcripts in
`vault/sources/transcripts/YYYY/MM/`, distinguished only by frontmatter
`source_agent: claude-code`. Everything downstream reads one tree.

Three stages:

1. **Triage** (`scripts/convert_cc_sessions.py`). Only sessions that are
   actually conversations qualify: `entrypoint: cli` (headless cron runs and
   spawned subagents — including Dreamer's own jobs — are `sdk-cli` and never
   ingested, or the system would feed on its own output), floors on human
   turns and characters (`cc_ingest.min_human_turns`, `min_human_chars`), and
   a quiet window (`min_quiet_hours`) so a session still being written is
   never half-captured. Rejections are recorded with a reason in the ledger,
   `vault/.vault-meta/cc-ingested.json` (keyed by session id), so an
   over-strict filter and a quiet week do not look the same.
2. **Summarise.** A coding session is mostly code, tool calls and diffs, none
   of which belongs in the vault. One model call per session
   (`budget.max_turns_cc_ingest`) rewrites it as a plain conversation plus a
   four-field abstract — goal, solution reached, outcome, and what was left
   unresolved — which is the field extraction actually feeds on. Long fenced
   blocks are collapsed before the call (`cc_ingest.max_code_lines`), and
   `cc_ingest.max_sessions_per_run` bounds a run's spend.
3. **Apply** (`scripts/apply_cc_session.py`). Writes one transcript page — or
   refuses and writes nothing: a page that still carries a fenced code block,
   a filesystem path, or anything `redact()` would have caught is rejected,
   because `vault/sources/` is immutable and committed, so a page written
   wrong cannot be quietly fixed.

Everything on such a page is Dreamer-generated and therefore derived tier
under rule 13 — which is fine, because what a transcript occurrence feeds is
*relevance* (recurrence and recency), and relevance and evidence are separate
axes by construction.

---

## 11. Goals and the assessor

A goal is a sibling of a loop: a page in `vault/goals/<id>.md` with a finish
line, a reason, a numbered plan and a derived `## Assessment` section (a **Now**
paragraph and an append-only **Trajectory**), the same shape as a loop's thread.
Loops track curiosity and goals track commitment; neither converts to the other.
The owner authors goals through the MCP write tools (§7), and only the owner:
Dreamer has no code path that creates one.

Three pieces make the writes trustworthy:

- **Authorization by quote.** An owner write carries the owner's verbatim words.
  The handler checks only length and presence. Truth is checked later.
- **Reconciliation.** `questions.reconcile_owner_writes()` runs at the top of
  every assessor run. A write whose quote appears in an ingested Human turn is
  marked verified. One still unmatched after `mcp.owner_write_verify_lag_days`
  is quarantined (`verified: false`, never reverted) and enqueued as a question,
  so a quarantine always has an owner-facing exit. The healthcheck reads the same
  ledger.
- **Domain modules only.** `goals.py`, `vault.py` and `questions.py` hold every
  vault mutation; `tests/test_mcp.py` walks the handlers to prove it.

`bin/nightly-assess.sh` follows extraction on the 19:00 line and runs whether or
not extraction succeeded. With no active goal it is a clean no-op with no model
call. Otherwise it follows the same extract/apply split as everything else:

1. `apply_assessment.py select` picks every unassessed transcript (or the next
   `goals.assess_batch_size` when that is set), oldest first, from the ledger
   `vault/.vault-meta/assessed.json`.
2. One research-surface model call (`budget.max_turns_assess`) proposes
   attachments: a transcript, a goal and an anchor quote.
3. `apply_assessment.py apply` accepts an attachment only when the quote appears
   in a Human turn of that transcript, and dates it from the transcript, never
   from model output. It then updates the mention and movement clocks.
4. The detectors run over those clocks, even on a night with nothing new to
   read, because silence is exactly when they matter: **silence** (no mention for
   `goals.silence_ping_days`), **stale** (no movement for
   `goals.movement_stale_days`) and **drift** (an open loop that serves no active
   goal but has recurred `goals.drift_recurrence_min` times). Each raises a
   question, subject to `goals.max_open_questions` and `goals.back_off_days`.
5. Each applied attachment gets one restricted fold
   (`budget.max_turns_assess_fold`) that rewrites that goal's Assessment from the
   current assessment plus the one attachment.

The batch is checkpointed only after it applied, so a deferral re-selects it
tomorrow. A goal page shows a zero diff on a night with no attachment to it
(rule 2, asserted by `goal-quiet-diff`), and goal pages carry no numeric
progress (`no-numeric-progress`).

---

## 12. Freshness and heartbeats

A quiet system and a stopped system look alike, so the question "when did this
last run, last produce something, last receive input?" gets its own module.

**Heartbeats.** The `arm_heartbeat` exit trap in `bin/_common.sh` records one
entry per job in `vault/.vault-meta/heartbeats.json` through `heartbeat.py`: when
it started, its outcome (`ok`, `failed`, `skipped`) and whether it produced
anything. `freshness.run_history_keep` bounds the history. The file is
git-ignored, so a fresh clone starts empty and each axis recovers when its job
next runs.

**Verdicts.** `freshness.py` reads the heartbeats and the existing ledgers and
computes one verdict per axis, of four kinds: *job* (ran in time and did not
fail), *source* (new input arrived), *convergence* (input that arrived has been
processed) and *output* (reported for context, never judged). Reasons are built
from numbers only. The module calls no model and writes nothing, and the
healthcheck, the dashboard and the publisher all call the same `collect()`
instead of recomputing.

**Assertions.** The healthcheck turns stale verdicts into `degraded` events and
never into `blocking` ones: blocking a leg because the system is stale would
make it staler. "Produced nothing" is not stale on its own. Thresholds live under
`freshness:`.

The healthcheck also carries the goal, owner-write and frontier assertions that
guard §11 and §16, such as `owner-writes-verified`, `goal-quiet-diff`,
`frontier-released-integrity` and `frontier-ceiling`.

---

## 13. The opt-in publishers

Three scripts project vault state into a Postgres database you control, for a
display. All are read-only with respect to the vault, all share one connection
file (`~/.config/dreamer/postgres.env`, override with `DREAMER_PG_ENV`), and each
does nothing, exits 0 and reports `info` / `disabled` until you enable it.

| Publisher | Schema | Carries | Enabled by |
|---|---|---|---|
| `freshness_publish.py` | `health` | Operational metadata only: axis ids, timestamps, counts, verdicts | The env file existing |
| `board_publish.py` | `board` | Loop titles and an allow-list of conclusion sections. Never web excerpts or the egress record | `DREAMER_PUBLISH_BOARD=1` |
| `frontier_publish.py` | `frontier` | Goal and item titles, summaries, lesson text. Never evidence quotes, passages, web excerpts or any count | `DREAMER_PUBLISH_FRONTIER=1` |

They run on the hourly `:12` line after the watchdog, in the order board,
freshness, frontier, so the health snapshot reports on the content publishes. The
board and frontier jobs also publish at the end of their own runs. Each replaces
its tables in one transaction and caps its own lock waits, statement time and
total runtime (`freshness.publish_*`, `board.publish_deadline_seconds`,
`frontier.publish_*`), so a stuck database delays a display and never a job.
`frontier_publish.py` waits at most `frontier.publish_lock_wait_seconds` for
`wiki.lock` and otherwise skips the hour. The contracts for readers are
[freshness-schema.md](freshness-schema.md), [board-schema.md](board-schema.md) and
[frontier-schema.md](frontier-schema.md).

---

## 14. The solution spine

`scripts/spine.py` is the single source for CLAUDE.md rule 5b: which problem
types exist (`factual`, `diagnostic`, `design`, `decision`, `dilemma`), which six
stages every conclusion walks (Define, Diagnose, Options, Evaluate, Recommend,
Check) and how deep each type takes each stage (`full`, `brief` or omitted). Three
places use that one table:

- `make_dream_prompt.py` renders the spine request into the research prompt from
  it, but only while `solving.enabled` is true.
- `apply_conclusion.py` validates the model's page against it. It adds headings
  only (existing headings keep their names and order, and Recommend is the
  `## Synthesis`), requires `solving.min_options` options besides "Leave it",
  drops a criterion that is not a phrase quoted from a Human turn of the loop's
  own conversations, and may lower `confidence` to `low`. It never raises it.
  The loop's `problem_type` is written to its frontmatter.
- `grade_conclusions.py`, `digest.py`, `healthcheck.py` (`spine-complete`) and
  `board_publish.py` read the result.

A conclusion with no owner criterion on record is provisional, and the question
it waits on goes to the open-questions queue. When the owner answers,
`questions.apply_criteria_answers()` adds the conversation as an occurrence of
the loop and marks it `reresearch_requested`, the one re-research that bypasses
the cooldown (rule 14).

---

## 15. More ingestion sources

All of these end as ordinary transcripts (§10), so nothing downstream knows the
difference.

**Desktop agent sessions.** `scripts/convert_desktop_sessions.py` is the Claude
Code converter with a different front end. It discovers
`<root>/<account>/<org>/local_<id>/audit.jsonl` under
`corpora.claude_desktop_sessions`, ignores replayed turns on resume, and never
opens the credential files that sit beside the audit log. Everything goes through
`redact()` first. It writes the same `.cc-input-*` payloads, so the existing
`bin/ingest-cc.sh` summarises and applies them. Settings are under
`desktop_ingest:`, and it keeps its own ledger (`desktop-ingested.json`) so the
Claude Code source axis keeps meaning Claude Code.

**The claude.ai daily sync (opt-in).**

```
Chrome extension ──native messaging──▶ scripts/claude_ai_host.py ──▶ to_ingest/claude-ai-sync-<UTC>.json
                                                                           │ 18:00 bin/sync-claude-ai.sh
                                                                           ▼
                                           convert_claude_export.py (under wiki.lock) ──▶ transcripts
```

The extension in `extension/dreamer-sync/` fetches your own chats from your
logged-in browser, incrementally, and hands them to the host over native
messaging. The host spools them, and on a `commit` writes one atomic drop and
updates `vault/.vault-meta/claude-ai-sync.json`. The cron job converts waiting
drops while holding `wiki.lock`, waiting up to
`claude_ai_sync.lock_wait_seconds`, so a chat that grew cannot be rewritten
between an extraction's read and its checkpoint. The cursor moves only to the
newest chat actually delivered, and any failure sends a report and no commit, so
the next run refetches. Fetching is the extension's job and conversion is the
cron job's, which is why a login problem shows up as the `source.claude-ai`
freshness axis and not as a failed job. Permissions, limits and failure states:
[claude-ai-sync.md](claude-ai-sync.md), [claude-ai-sync-state.md](claude-ai-sync-state.md).

**Regrown chats.** `make_batch.py` records each transcript's `message_count` when
it is marked extracted (`extracted_through` in `extracted.json`). A chat that
later grows is selected again, and the extraction prompt is told to read only the
turns after that boundary.

---

## 16. The knowledge frontier

The frontier is a map of what the owner knows against what each active goal
needs, plus a stream of lessons for the gaps. It lives in `vault/frontier/`
(`items/`, `lessons/`, `premortems/`), is indexed as its own qmd collection
(`frontier`), and is written only through `scripts/frontier.py`, which owns the
page store and the one rule that places an item in a quadrant:

1. a `got_it` tap in effect: known known;
2. an aired lesson pins the quadrant it aired in (a `dig_deeper` tap keeps the pin);
3. owner evidence (explained, decided, applied): unknown known if any active goal
   relation is unconnected, else known known;
4. asked or reconstructed evidence, or a linked loop: known unknown;
5. otherwise, including a pre-mortem origin or assistant-only exposure: unknown
   unknown.

Only the owner's words reach a "known" quadrant (rule 13 and 17). `owner_text.py`
is the reader that enforces it: it takes `## Human` turns, strips pasted
material, keeps head and tail of a very long turn (`frontier.owner_turn_max_chars`)
and treats reconstructed and assistant turns as exposure.

The nightly pass, `bin/frontier.sh`, holds `frontier.lock` for the run and takes
`wiki.lock` only around each apply and the commit, so the owner's MCP writes and
the other jobs never wait behind a model call. A step that cannot get
`wiki.lock` exits 2: deferred, nothing marked. A failed step records a
`degraded` event and the independent later steps still run, so a map failure
never blocks taps and a show outage never blocks the map.

| Step | Script | What it does |
|---|---|---|
| Pull | `frontier_sync.py pull` | Marks lessons aired or failed from the consumer's status, applies taps in id order, advances a cursor only after the page writes commit. A tap id the lesson already records with another kind or time is reported as `taps_reused` and not applied. |
| Map | `frontier_map.py run-batch` | One batch a night, up to `frontier.map_batch_chars` of owner text. Select, extract concepts with a sealed call, verify every quote verbatim against a Human turn, retrieve neighbours from the `frontier` collection, match with a sealed judge (new on doubt, as rule 7), then apply under `wiki.lock`. Evidence is keyed by transcript and normalised quote, so a crashed batch resumes without duplicating. A degraded retrieval defers the batch. |
| Gaps | `frontier_gaps.py run` | For each goal whose last pre-mortem is older than `frontier.premortem_every_days` (or whose plan changed): a sealed pre-mortem ("it is a year on and the goal failed, why?"), then a never-discussed check per candidate over passages from several collections. A degraded search or an unverifiable quote defers the candidate. It is never treated as "unknown unknown". A consequence judge keeps only the gaps that matter. The record is `vault/frontier/premortems/<goal>/<date>.md`. |
| Lessons | `frontier_lessons.py run` | The queue is derived each run: follow-ups for `dig_deeper` taps first, then new lessons from the latest pre-mortem. One lesson is built in memory and written once with its final status: library passages, sealed queries, the egress gate, a gated search, a code fetch, sealed claims, passage verification, sealed compose, an independent sealed check that fails closed, then code gates. |
| Push | `frontier_sync.py push` | Releases ready lessons within `frontier.weekly_ceiling` per `frontier.ceiling_window_days`, re-sends lessons the consumer lost, and replaces its known list. No HTTP call is made while `wiki.lock` is held. |
| Publish | `frontier_publish.py` | The opt-in map display (§13). |

`bin/frontier-backfill.sh` is the same map step in a loop, to drain an existing
corpus once. `frontier_sync.py withdraw <lesson> --cause "..."` stops a lesson
before it airs. The consumer contract is in
[frontier-schema.md](frontier-schema.md); with `frontier.show_url` empty, no token
is read and nothing is sent. Every quadrant change records its cause, and a tap is
applied once, so only an owner write (`undo_tap`) reverses it.

---

## 17. Model call modes and the egress gate

`run_claude` has three tool surfaces, chosen by its fourth argument.

| Mode | Tools | Used by |
|---|---|---|
| full (no argument) | `qmd` through Bash, WebSearch, WebFetch, edit permission for the job | Jobs whose contract permits egress: the weekly dream and the assessor's attachment pass |
| `restricted` (sealed) | None. The built-in tool set is empty, no MCP servers are loaded, slash commands are disabled, an explicit deny list repeats the ban, and turns are capped at 4 | Every call whose prompt inlines private text: tag backfill, the thread and assessment folds, and every frontier call except research |
| `gated` | WebSearch only, behind a PreToolUse hook | The frontier research call |

Sealing means a tool the deny list forgot is absent rather than denied.
`tests/test_run_claude_argv.py` pins the argument order.

In `gated` mode `egress_hook.py` allows a search only if its query is a line in
the approved file for that call (`frontier.research_approved_file`), and writes
every decision, allowed or denied, to `frontier.egress.log_path`.
`egress_results_hook.py` records each URL a search returned. The call refuses to
start if either the approved file or the log path is missing. `egress_check.py`
approves the queries beforehand and again inside the hook: it blocks names from
`frontier.egress.blocked_names_path`, capitalised terms not in the dictionary or
the allowed list, and any run of `frontier.egress.phrase_run_words` words copied
from the goal page or the item's evidence. A missing list blocks everything (fail
closed), and a failing query is dropped, never rewritten.

The model never fetches a page. `verify_passages.py` does, and only for a URL a
search returned: https on the default port, a dotted host name, every resolved
address globally routable, the connection pinned to the address that was checked,
redirects followed by hand with every hop re-checked, and bytes, time and
redirects capped (`frontier.fetch_*`). A quoted passage counts only if
`frontier.passage_min_run_tokens` of its tokens appear as one run in the fetched
text, and what is kept is the source's own wording. Fetched text matching an
instruction pattern is rejected (rule 10).

Every sealed reply must be exactly the schema's JSON, optionally in one fence.
Prose around it, extra keys, unknown enum values and ids that name nothing are a
`ParseFailure`: retried `frontier.parse_retries` times, then the item is deferred,
and skipped with a `degraded` event after `frontier.max_parse_attempts`. A
usage-limit exit stops the remaining model steps and leaves the ledger unmarked
(rule 9).

---

## 18. Where to look when something breaks

Start here, in this order:

1. **`logs/<job>.log`** — every job logs `[timestamp] [job] message` and
   `run_claude` appends the CLI's stderr. This is where you find
   `qmd not on PATH`, `deferred`, `REJECT candidate #3`, `apply failed`.
2. **`vault/.vault-meta/run-state.json`** — `events[]` (deferrals, recoveries,
   reopenings, degradations, quality warnings, apply failures), `deferrals[]`,
   `costs[]`, and the `skip_research_next_run` latch. If research mysteriously
   did not run, check that flag first.
3. **The latest digest** — the same events render at the top under "What
   happened this week", above the content. A quiet digest with no event block
   really did mean a quiet week; a quiet digest *with* one did not.
4. **`python3 scripts/vault.py lint`** — catches malformed/duplicate ids, illegal
   statuses, empty titles, `recurrence_count` disagreeing with the occurrence
   set, broken occurrence and conclusion wikilinks, `paused` without a
   conclusion, out-of-vocabulary tags, and orphan conclusion pages. Exit code 1
   on any problem.
5. **`git log -- vault/`** — every job commits with a message naming what it did
   (`nightly-extract 2026-08-02: created=3 matched=1`,
   `weekly-dream 2026-08-02: L0012 researched`, `... apply FAILED`). Because
   `DREAMER_TODAY` drives the logical date, history reconstructs a run exactly.
6. **`./bin/dashboard.sh --open`** — loop population, status mix, routes,
   recurrence distribution, conclusion quality, run history at a glance.

Common failures and their signature:

| Symptom | Look at | Likely cause |
|---|---|---|
| Conclusions cite zero books | `logs/`, `run-state.json` `degraded` events | `qmd` not on `PATH` under cron; the wisdom route is dead and `reindex` says so |
| A loop is stuck and never re-selected | `vault.py lint`, its `status` | Left in `researching` by a deferral — run `vault.py recover` (weekly-dream does this automatically) |
| Weekly run says "quiet week" every week | `scripts/calibrate.py histogram` | `matching.recurrence_min` too high for your corpus |
| Research never runs, no error | `run-state.json` → `skip_research_next_run` | A previous run breached `budget.cost_ceiling_per_run` |
| Backfill makes no progress | `vault/.vault-meta/quarantine.json` | A batch's apply failed and was quarantined by name; the transcripts are recorded, not lost |
| Merge backlog never drains | the `merge candidates:` log line | `remain queued > 0` — raise `matching.max_judged_per_run` |
| Goals are never assessed, no questions appear | `logs/assess.log`, `assessed.json`, `python3 scripts/goals.py lint` | No active goal (clean no-op), or the assess leg is blocked by the healthcheck. `python3 scripts/goals.py detectors` prints what each detector sees |
| An owner write came back as a question | `owner-writes.json`, `questions.py` | The quote was not found in an ingested Human turn within `mcp.owner_write_verify_lag_days`. The write stands, quarantined. Answer the question |
| A freshness axis reads `never` or `stale` | `scripts/freshness.py --json`, `heartbeats.json` | The file is git-ignored and starts empty on a fresh clone, or the job's wrapper never ran. The `reason` field names the clock |
| The publisher says `not configured` or `disabled` | `logs/*-publish.log`, `~/.config/dreamer/postgres.env` | The env file is missing, or the board/frontier flag is unset. This is the off state, not a fault |
| `source.claude-ai` is stale or disabled | `vault/.vault-meta/claude-ai-sync.json`, `logs/claude-ai-host.log` | `claude_ai_sync.extension_id` is empty (disabled), or the login lapsed, a bot check fired, or the site changed. See [claude-ai-sync.md](claude-ai-sync.md) |
| Desktop sessions never arrive | `desktop-ingested.json`, `logs/ingest-cc.log` | `corpora.claude_desktop_sessions` points at nothing, or the triage floors refused them. The ledger records each refusal |
| The frontier does nothing | `logs/frontier.log`, `frontier.lock` | Another run holds the lock, `leg_blocked "frontier"`, or a step is deferred on `wiki.lock`. The log line per step names its status |
| Map or pre-mortem steps keep deferring | `frontier-map-ledger.json`, `frontier.qmd_timeout_seconds` | qmd is unreachable or the `frontier` collection is unregistered. A degraded search is deferred, never read as "nothing found". Run `python3 scripts/qmd_client.py --probe` |
| No lessons are released | `frontier-research-ledger.json`, `frontier_sync.py status` | The ceiling (`frontier.weekly_ceiling`) is reached, `frontier.show_url` is empty, or every candidate was rejected by verification. A week with none is normal |
| A research query is missing from the log | `logs/frontier-egress.jsonl` | The egress gate denied it. Each line records the verdict and reason. Check the blocked-names file exists and is not empty |
| A loop lost a body section | `git diff` on the loop page | Something wrote `loop.body = ""` and fell back to `default_body()`; use `refresh_occurrences_section()` instead |

### Testing changes

```bash
python3 -m unittest discover -s tests   # unit suite
tests/simulated_week.sh                 # end-to-end, real state machine, fake model
./bin/verify.sh --skip-golden           # acceptance sweep against your real vault
```

`DREAMER_TODAY=YYYY-MM-DD` overrides the logical date everywhere
(`dreamer_common.today()` and `_common.sh :: TODAY`), which is what makes the
decay clock and the digest week deterministic in tests.

If your change alters what the reasoning engine *does*, amend
[CLAUDE.md](../CLAUDE.md) in the same commit. A rule the code does not follow,
or behaviour no rule describes, is the failure mode this whole structure exists
to prevent.
