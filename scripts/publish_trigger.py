#!/usr/bin/env python3
"""Publish straight after an owner write.

A change made through the MCP (resolving a loop, say) otherwise reaches a
display only at the next job or the hourly publish. This starts the two
publishers in the background right away: the board first, then health, the
same order the job wrapper uses.

Armed only by the running MCP server (`ENABLED = True` in its main()), so a
test, a script or an import never publishes anything. It never blocks and
never fails the write that called it: the child is detached, and its output
goes to log files — never to the server's stdout, which is the MCP protocol
channel.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, ROOT, p  # noqa: E402

ENABLED = False


def command() -> str:
    """One shell line: board publish, then health publish, each to its log."""
    py, scripts, logs = sys.executable, ROOT / "scripts", p("logs")
    return (f'"{py}" "{scripts / "board_publish.py"}" >> "{logs / "board-publish.log"}" 2>&1; '
            f'"{py}" "{scripts / "freshness_publish.py"}" >> "{logs / "freshness-publish.log"}" 2>&1')


def _spawn(cmd: str, log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        # A new session, no inherited descriptors: the child can outlive the
        # server, cannot hold wiki.lock, and cannot write to the MCP channel.
        subprocess.Popen(["/bin/sh", "-c", cmd], stdin=subprocess.DEVNULL, stdout=fh,
                         stderr=subprocess.STDOUT, start_new_session=True,
                         close_fds=True, cwd=str(ROOT))


def fire() -> bool:
    """Start a publish if armed and allowed. True when one was started."""
    if not ENABLED or not (CFG.get("board") or {}).get("publish_on_owner_write"):
        return False
    try:
        _spawn(command(), p("logs") / "publish-trigger.log")
    except Exception:  # noqa: BLE001 — a display must never cost an owner write
        return False
    return True
