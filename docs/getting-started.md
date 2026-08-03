# Getting started

From a fresh clone to your first researched conclusion. Expect the calibration
step to take the longest — that is where you decide what counts as a loop worth
researching in *your* material.

---

## 1. Requirements

| | |
|---|---|
| **Python 3.11+** | with `pyyaml` (`pip install pyyaml`) |
| **git** | the vault's history is the audit trail; this is not optional |
| **[Claude Code](https://claude.com/claude-code)** | the `claude` CLI provides the reasoning steps |
| **[qmd](https://github.com/pirate/qmd)** | *strongly recommended* — local semantic search. Without it the `wisdom` and `past-reasoning` routes have nothing to read and every loop escalates to web |
| **flock** | recommended (util-linux); provides the job lock |

Everything runs locally except the reasoning calls. If you use qmd, embedding
and reranking are local too.

---

## 2. Install

```bash
git clone https://github.com/HerdingAI/dreamer-loops.git
cd dreamer-loops
./bin/setup.sh
```

`setup.sh` creates `config.yaml` from the example, sets up local MCP wiring,
and checks your dependencies. It never overwrites a file you have edited, so it
is safe to re-run.

Then edit `config.yaml`. The only key you must set to get started:

```yaml
corpora:
  claude_export:   /path/to/your/claude-export
```

Point it at an unpacked [claude.ai data
export](https://support.anthropic.com/en/articles/9450526-how-can-i-export-my-claude-ai-data)
— or skip it and drop transcripts into `vault/inbox/` yourself.

If you have a reference library (books, papers, lecture transcripts), point
`wisdom_sources` at it too. If you don't, leave it — the system will route
around the gap and tell you it did.

> **Leave `decay.go_live_date` as `null`.** Decay is inert while it is null, so
> nothing can archive while you are still calibrating. You will set it later.

Confirm the install:

```bash
./bin/verify.sh
```

---

## 3. Ingest your conversations

```bash
./bin/ingest.sh
```

Drop any export — a zip, a folder, or a bare `conversations.json` — into
`to_ingest/` and run it with no arguments. It converts through a dedupe ledger,
so it is idempotent: re-running never duplicates a conversation.

**Secrets are redacted here**, before anything touches disk. Real exports
routinely contain database passwords and API keys pasted into conversations;
without this step they would land in git permanently. Check what was caught:

```bash
grep -c REDACTED vault/sources/transcripts/**/*.md
```

What you now have in `vault/sources/transcripts/` is immutable. Nothing in the
system ever modifies it.

---

## 4. Calibrate before you backfill

This is the step worth not rushing. You are deciding how many distinct
conversations a thread needs before it is worth researching — and the right
answer depends entirely on your own material.

Run extraction on a pilot slice rather than the whole archive:

```bash
./bin/nightly-extract.sh          # processes a batch
python3 scripts/calibrate.py all  # histogram + gate sample + golden set
```

That produces three things in `vault/digests/`:

**A recurrence histogram.** How many loops exist at each recurrence count. Read
`matching.recurrence_min` off this, don't guess it:

- Too **low** and the weekly run drowns in one-offs — things you mentioned once
  and never again.
- Too **high** and it starves; nothing qualifies and the run reports a quiet
  week forever.

A reasonable target is a threshold where somewhere between 10% and 30% of loops
qualify. Set it in `config.yaml`.

**A calibration sample.** Extracted loops for you to check by hand. This is the
real quality gate — go through them and ask, for each: *is this actually an
open thread of mine, or did the extractor invent structure that wasn't there?*
If a substantial fraction are wrong, the problem is upstream and no amount of
tuning downstream will fix it.

**A tag vocabulary proposal.** Expect to rewrite it heavily; single-token
frequency is a crude clusterer and it will surface real themes alongside noise.
Until you approve a vocabulary at `vault/.vault-meta/tag-vocabulary.json`, pages
carry prose theme notes and emit no tags at all — by design (rule 4).

---

## 5. Look at what it found

```bash
./bin/dashboard.sh --open
```

The dashboard shows loop population, recurrence distribution, routes, tags,
conclusion quality, and run history. It is a read-only projection — safe to run
at any moment, including while a job is mid-research.

`./bin/dashboard.sh --serve` runs a local server at `127.0.0.1:8787` that
regenerates on every request, which is the only mode that gives a genuinely
live view. It binds to loopback only: the page has no authentication and
reports on your private material.

Read a few loop pages directly too. They are plain Markdown in `vault/loops/`.

---

## 6. Backfill the rest

Once the pilot looks right:

```bash
./bin/backfill.sh
```

It works in batches with a commit per batch, so it is interruptible and
resumable. A large archive takes hours — that is expected, and it is the one
genuinely expensive operation in the system. Run it in the background and check
the dashboard as it goes.

---

## 7. Your first research run

```bash
./bin/weekly-dream.sh
```

This selects the top recurring loops, routes each one, researches it, and
writes a cited conclusion. Then read the digest in `vault/digests/`.

Judge the output honestly. For each conclusion:

- Do the citations exist, and do they say what the page claims?
- Is the synthesis committing to an answer, or surveying considerations?
- Are the open sub-questions real, or filler?

The system grades conclusions structurally, but **that rubric cannot tell you
whether a conclusion is correct** — only whether it decided anything. A low
score means "probably not worth your ten minutes". A high score does not mean
"true".

---

## 8. Go live

Once you trust the output:

```yaml
decay:
  go_live_date: 2026-01-15   # today's date
```

```bash
./bin/install-cron.sh
```

That installs the nightly extraction, the weekly dream, and the weekly decay
pass. Every job takes an advisory lock, so an overrun never collides with its
successor.

Nothing can archive for a full `decay_weeks` window from the date you set —
including loops the backfill minted with old timestamps. That floor is
deliberate (see [the rules](the-rules.md#rule-3--the-decay-rule)).

---

## 9. Optional: query the vault from a chat client

Set up the MCP server and your assistant can search your loops and conclusions
mid-conversation, and log when a topic resurfaces live. See
[mcp-setup.md](mcp-setup.md).

---

## Troubleshooting

**"No loop qualifies — quiet week."** `recurrence_min` is higher than your
corpus supports. Re-read the histogram.

**Every conclusion cites zero library sources.** qmd is not reachable. It ships
via nvm, whose PATH only exists in an interactive login shell — so cron and
headless runs start without it. The job harness prepends nvm bin directories,
but verify with `command -v qmd` in a clean shell. A dead `wisdom` route is
silent unless you look for it.

**A loop is stuck in `researching`.** A run deferred mid-loop. This is the
designed recovery path, not a fault: `python3 scripts/vault.py recover` resets
it, and the next weekly run picks it up first.

**Jobs commit things you were editing.** They shouldn't — jobs stage only
`vault/` and `logs/`. If you see otherwise, that is a bug worth reporting.

**Extraction finds nothing in conversations you know are open threads.** Read
[rule 6](the-rules.md#rule-6--the-meta-idea-filter) first — conversations that
end in an executable plan are correctly excluded. If they genuinely are open
threads, `scripts/probe_recall.py` helps you measure it, but only you can
supply the list of threads you know you circled.
