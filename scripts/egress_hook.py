#!/usr/bin/env python3
"""PreToolUse hook for the gated research call.

Allows a WebSearch only when its normalised query is in the approved-query
file; denies every other tool, WebFetch included. Every decision is logged. Any
failure (bad JSON, missing env var, unwritable log, unexpected exception) is an
explicit deny. stdout carries exactly one decision JSON document, nothing else.

Environment: DREAMER_EGRESS_APPROVED (approved-query file),
DREAMER_EGRESS_LOG (JSON-lines log).
"""

from __future__ import annotations

import json
import os
import sys

INPUT_SUMMARY_CHARS = 500  # log bound for a tool_input dump, not a policy limit


def decide(payload, approved_path: str):
    """Return (decision, reason, tool, query, input_summary)."""
    import egress_check as ec  # inside the caller's try: an ImportError must deny
    if not isinstance(payload, dict):
        return "deny", "payload is not a JSON object", None, None, ""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    summary = json.dumps(tool_input, ensure_ascii=False)[:INPUT_SUMMARY_CHARS]
    if tool != "WebSearch":
        return "deny", f"tool not permitted: {tool}", tool, None, summary
    query = tool_input.get("query") if isinstance(tool_input, dict) else None
    if not isinstance(query, str) or not query.strip():
        return "deny", "WebSearch without a query", tool, None, summary
    if ec.normalize_query(query) in ec.read_approved(approved_path):
        return "allow", "query approved", tool, query, summary
    return "deny", "query not approved", tool, query, summary


def main() -> int:
    decision, reason = "deny", "hook did not complete"
    entry = {"tool": None, "query": None, "input": ""}
    log_path = os.environ.get("DREAMER_EGRESS_LOG")
    # Deliberately broad: this hook's contract is that nothing escapes as a
    # traceback or an empty stdout (which the CLI would read as "no opinion").
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            payload = None
            entry["input"] = raw[:INPUT_SUMMARY_CHARS]
            decision, reason = "deny", f"bad JSON on stdin: {exc}"
        else:
            approved = os.environ.get("DREAMER_EGRESS_APPROVED")
            if not approved:
                decision, reason = "deny", "approved-query file env var DREAMER_EGRESS_APPROVED not set"
                if isinstance(payload, dict):
                    entry["tool"] = payload.get("tool_name")
                    entry["input"] = json.dumps(payload.get("tool_input"), ensure_ascii=False)[:INPUT_SUMMARY_CHARS]
            else:
                decision, reason, entry["tool"], entry["query"], entry["input"] = decide(payload, approved)
    except Exception as exc:  # noqa: BLE001 - fail closed, see above
        decision, reason = "deny", f"hook error: {exc!r}"

    if not log_path:
        decision, reason = "deny", "egress log env var DREAMER_EGRESS_LOG not set; " + reason
    else:
        try:
            import egress_check as ec
            ec.log_decision(log_path, decision=decision, reason=reason, **entry)
        except Exception as exc:  # noqa: BLE001 - an unlogged egress is a denied one
            decision, reason = "deny", f"egress log unwritable ({exc!r}); " + reason

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
