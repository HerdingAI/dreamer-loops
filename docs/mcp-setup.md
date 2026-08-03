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
delete, or rename. That is the enforcement behind "zero write paths from MCP
into loops/, conclusions/, concepts/, sources/".
