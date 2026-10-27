# MCP client setup

Both servers speak **stdio**. Nothing listens on a TCP port, so there is no
local-process or DNS-rebinding exposure to close and no token to manage.

## Claude Code

`.mcp.json` in the repo root is picked up automatically when you run Claude Code
from `/path/to/dreamer`. To use Dreamer from *any* directory:

```bash
claude mcp add dreamer -- python3 /path/to/dreamer/scripts/dreamer_mcp.py
claude mcp add qmd -- qmd mcp
```

## Claude Desktop

Add to `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dreamer": {
      "command": "python3",
      "args": ["/path/to/dreamer/scripts/dreamer_mcp.py"]
    },
    "qmd": {
      "command": "qmd",
      "args": ["mcp"],
      "cwd": "/path/to/dreamer"
    }
  }
}
```

Restart Claude Desktop. Verify with: *"what open loops do I have?"* — the client
should call `list_open_loops` without being told to.

## The tools

Read tools search and fetch; they never change anything.

| Tool | Use it for |
|---|---|
| `search_insights`, `get_loop`, `list_open_loops` | Finding tracked loops and their conclusions |
| `search_wisdom` | Searching your reference library |
| `get_latest_digest` | The newest weekly digest |
| `list_active_goals`, `get_goal` | What you are working toward, and one goal's finish line, plan and assessment |
| `list_open_questions` | Everything Dreamer is waiting on you for |
| `what_do_i_know`, `get_frontier`, `get_lesson` | What you already know about a topic, the knowledge frontier for a goal, and one lesson |

Write tools:

| Tool | What it changes |
|---|---|
| `log_resurfacing` | Queues a note that a loop came up again. Applied by the nightly job, and the note is untrusted input |
| `commit_goal` | Creates a goal: title, finish line, why, plan |
| `revise_plan`, `complete_step` | Replaces a goal's plan; marks one step done |
| `set_status` | Pauses, resumes, achieves or abandons a goal, or settles a tracked loop |
| `assert_evidence` | Records something you did outside any captured conversation |
| `answer_question` | Records your reply to a question Dreamer raised, or defers it |
| `tap_lesson`, `undo_tap` | Records, or undoes, a "Got it" or "Dig deeper" on a lesson |

Every tool that changes a goal, a loop's status, a question or a tap requires two
arguments besides its own: `owner_quote`, your exact words from the current
conversation (at least `mcp.owner_write_quote_min_chars` characters), and
`session_id`. The tool descriptions tell the assistant to commit only on your
clear say-so, to quote you verbatim rather than paraphrase, and to ask when it is
unsure. Dreamer later checks each quote against the transcripts it ingests, and a
write it cannot match is quarantined and raised as a question for you. See
[rule 16](the-rules.md#rule-16--owner-writes-the-worried-friend).

## The convention block

Belt and suspenders with the tool descriptions. Paste into your project
instructions — but note the cold-trigger test (§6.7 DoD) must pass **without**
it. If triggering only works with this block present, the descriptions have
failed and the fix is the descriptions, not this paragraph.

> I keep a personal knowledge system called Dreamer, exposed over MCP. It tracks
> ideas I keep circling back to and holds researched conclusions on some of them.
> When I raise a problem, a design question, an architecture idea, a recurring
> frustration, or anything starting "I've been thinking about…", search it before
> reasoning from scratch — I may have already concluded something, or it may
> already be a tracked open loop. If it is, and we're genuinely discussing that
> same topic, log the resurfacing.

## Verifying the write boundary

```bash
python3 -m unittest tests.test_mcp -v
```

`test_source_has_no_write_call_outside_resurfacings` walks the AST and fails if
any function other than `tool_log_resurfacing` / `record_access` can write,
delete, or rename. That is the enforcement behind "the server file has one write path, into
`inbox/resurfacings/`".

The owner-write tools pass that test because none of them writes in the server
file. Each calls a domain module (`goals.py`, `vault.py`, `questions.py`,
`frontier.py`), which writes atomically and records the write in
`vault/.vault-meta/owner-writes.json`.
