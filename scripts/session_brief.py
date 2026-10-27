#!/usr/bin/env python3
"""Session-start surface for the Friend (spec §6.1, §6.7).

Run as a Claude Code SessionStart hook: whatever this prints is added to the
session's context. It is the one place Dreamer speaks first, so it says only
two things, each at most once:

- pending silence questions, capped by goals.max_open_questions (§6.2), and
- that a new weekly digest is ready to review, when the Friend has business in it.

The owner answers in conversation; that conversation is ingested as a
transcript, so the answer arrives as a Human turn — primary tier (rule 13).

It never fails a session: any error prints nothing and exits 0.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import goals as G  # noqa: E402
import questions as Q  # noqa: E402
from dreamer_common import p  # noqa: E402

DIGEST_NAME = re.compile(r"^\d{4}-\d{2}\.md$")


def latest_digest_name() -> str | None:
    names = sorted(f.name for f in p("digests").glob("*.md") if DIGEST_NAME.match(f.name))
    return names[-1] if names else None


def review_offer() -> str:
    """One line offering the weekly review, or "" if there is nothing to offer."""
    name = latest_digest_name()
    if not name or name == Q.last_review_offer():
        return ""
    # A digest with no goals and no open questions has no Friend section worth
    # a conversation; offering it anyway would be the nag §6.2 rules out.
    if not G.active_goals_count() and not Q.list_questions(surface="review"):
        return ""
    Q.mark_review_offered(name)
    return (f"Dreamer: the weekly review is ready (digest {name[:-3]}). Offer to go "
            "through it with the owner now — fetch it with the dreamer "
            "`get_latest_digest` tool and take its Friend section one item at a time.")


def build_brief() -> str:
    parts = []
    pending = Q.list_questions(surface="async")
    if pending:
        # Marked before printing: an ask that is shown twice is worse than one
        # that waits for the review, which is where unanswered pings go anyway.
        Q.mark_asked([q["id"] for q in pending], surface="async")
        parts.append(Q.render_for_owner(pending))
    offer = review_offer()
    if offer:
        parts.append(offer)
    return "\n\n".join(parts)


def main() -> int:
    # Only an interactive session has an owner in it. Dreamer's own jobs and
    # spawned subagents are `sdk-cli`; a ping spent there is never seen.
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") != "cli":
        return 0
    try:
        text = build_brief()
    except Exception as exc:  # noqa: BLE001 — a hook must never break a session
        print(f"session_brief: {exc}", file=sys.stderr)
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
