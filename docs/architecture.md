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

---

## 5. The job harness

Every wrapper in `bin/` sources `bin/_common.sh`, which is the only place jobs
touch the model, the lock, git or run state.

| Function | Contract |
|---|---|
| `acquire_lock` | `flock -n` on `vault/.vault-meta/wiki.lock`. **Failure exits 0**, not 1: an overrunning job is a normal outcome and the successor should log a clean skip, not a failure. |
| `run_claude <prompt> <max_turns> <out>` | Wraps `claude -p --output-format json`, pins `--model` from `budget.model`, and allowlists exactly `Bash(qmd:*)`, `mcp__qmd`, `WebSearch`, `WebFetch`. Unwraps `.result` into `<out>` and `total_cost_usd` into `<out>.cost`. **A non-zero exit is a deferral, not a failure**: it is logged, recorded, and returns non-zero so the caller can stop cleanly with work resumable. `DREAMER_FAKE_CLAUDE` substitutes a deterministic script for tests. |
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
| `ingest-cc.sh` | 18:30, ahead of extraction | Sweep Claude Code sessions → triage → summarise → apply as transcripts (§10). Takes no vault lock — conversion is append-only and atomic — but is scheduled clear of the others anyway. |
| `nightly-extract.sh` | 19:00 | healthcheck → convert inbox → batch → extract → apply → mark batch → drain the fold queue (§9) → reindex → catalog → commit. Empty batch is a near-zero-cost no-op that still drains queued resurfacings and folds. |
| `weekly-dream.sh` | the night-cycle dream leg | ingest marks → recover stranded → cost-ceiling check → select → **per-loop**: prompt, mark `researching`, research, apply, grade, commit → merge refresh → lint → digest → reindex → catalog → commit. |
| `decay-archive.sh` | Sunday 19:30, before the night window opens | `run_decay()`, stage `archived` items into `pending.json`, catalog, commit. |
| `backfill.sh [N]` | the night-cycle backfill leg, or manual | Up to N extraction batches, resuming from the checkpoint. A failed apply quarantines the batch by name and continues, rather than stalling the queue forever on one bad transcript. |
| `night-cycle.sh [N]` | 20:00 / 23:00 / 02:00 / 05:00 | healthcheck → backfill leg → healthcheck again (the backfill leg mutates the corpus mid-cycle) → dream leg, in **one** wrapper — the legs share the advisory lock, so co-scheduling them would starve whichever started second. A `blocking` health assertion stops only the leg it names (§8). |
| `ingest.sh` | manual | Standardised drop point: put an export in `to_ingest/`, run with no arguments. Deliberately does **not** take the lock — conversion is append-only and atomic. |
| `tag-backfill.sh` / `thread-backfill.sh` | once, manual | One-time initialization drains for pre-existing vaults (§9 and rule 15's carve-out). Safe to delete once they report complete. |
| `verify.sh` | manual | Acceptance sweep: reindex, catalog, lint, unit tests, `probe_recall.py` gate, conclusion rubric, golden set (`--skip-golden` omits the only paid gate). |
| `dashboard.sh` | manual | `--open` / `--serve`. Sources nothing: no lock, no commit. |
| `install-cron.sh` | once | Installs the block idempotently, with an explicit `PATH` for node/qmd. |

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
| `log_resurfacing(loop_id, note)` | Queued filename | The only write. |

**Retrieval trade-off.** `_qmd_query(deep=False)` runs `qmd search` (BM25,
~0.4 s, 12 s timeout) for interactive calls; `deep=True` runs `qmd query`
(hybrid + rerank, ~20 s, 60 s timeout) and applies `QMD_RELEVANCE_FLOOR = 0.88`.
The floor exists because the reranked path *always* returns a rank-1 result and
emits exactly 0.88 when nothing genuinely matches — a confidently cited, wrong
answer. BM25 needs no floor: it honestly returns zero rows.

**The write boundary.** `tool_log_resurfacing` writes one file into
`vault/inbox/resurfacings/` and nothing else. Loop mutation happens that night,
in `apply_extraction.apply_resurfacings()`, through the same
`add_occurrence()` path a transcript takes — so a resurfacing gets the same
reopening rule, the same idempotency, and the same event logging.

The boundary is enforced statically. `tests/test_mcp.py` parses the server with
`ast` and walks every function body, failing if anything outside
`{tool_log_resurfacing, record_access}` calls `atomic_write`, `write_page`,
`write_text`, `unlink`, `mkdir`, `replace`, `rename`, `remove`, `rmtree`, or
`open()` with a `w`/`a`/`x`/`+` mode. AST rather than grep, so a write cannot
hide by not mentioning "resurfacing" on its own line.

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

## 11. Where to look when something breaks

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
