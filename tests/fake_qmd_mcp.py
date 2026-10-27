#!/usr/bin/env python3
"""Deterministic stand-in for `qmd mcp` (stdio JSON-RPC, one message per line).

The response shape follows the real qmd 2.5.3 server; the content is synthetic. Behaviour is chosen by FAKE_QMD_MODE:

  ok         answer normally (default)
  crash      exit as soon as a tools/call arrives
  hang       never answer a tools/call
  hang_init  never answer initialize
  badjson    answer a tools/call with a non-JSON line
  error      answer a tools/call with a JSON-RPC error
  iserror    answer a tools/call with isError: true
  noline     return a hit that lacks `line`
  noshape    return a result without structuredContent

FAKE_QMD_LOG, when set, receives every tools/call `arguments` object as one
JSON line, so a test can assert what the client actually sent.
FAKE_QMD_COLLECTIONS overrides the collections named in `instructions`.
"""

from __future__ import annotations

import json
import os
import sys

MODE = os.environ.get("FAKE_QMD_MODE", "ok")
LOG = os.environ.get("FAKE_QMD_LOG")
COLLECTIONS = os.environ.get("FAKE_QMD_COLLECTIONS", "vault, conclusions, transcripts, frontier")

HITS = [
    {"docid": "#aaa111", "file": "transcripts/2026/01/2026-01-02-firing-test-tile.md",
     "title": "Firing the test tile", "score": 1, "context": None, "line": 321,
     "snippet": "321: @@ -320,4 @@ (319 before, 4256 after)\n322: We fired the test tile on the top shelf."},
    {"docid": "#bbb222", "file": "transcripts/2026/02/2026-02-03-cone-notes.md",
     "title": "Cone notes", "score": 0.5, "context": None, "line": 12,
     "snippet": "12: @@ -11,4 @@ (10 before, 9 after)\n13: Cone and schedule."},
]


def reply(msg_id, result=None, error=None) -> None:
    body = {"jsonrpc": "2.0", "id": msg_id}
    body.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            if MODE == "hang_init":
                continue
            reply(mid, {"protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "qmd", "version": "2.5.3"},
                        "instructions": "QMD is your local search engine over 3 markdown "
                                        "documents.\n\nCollections (scope with `collection` "
                                        f"parameter): {COLLECTIONS}\nCall the `status` tool."})
        elif method == "tools/call":
            args = msg["params"]["arguments"]
            if LOG:
                with open(LOG, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(args) + "\n")
            if MODE == "crash":
                return 3
            if MODE == "hang":
                continue
            if MODE == "badjson":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                continue
            if MODE == "error":
                reply(mid, error={"code": -32603, "message": "boom"})
                continue
            if MODE == "iserror":
                reply(mid, {"isError": True, "content": [{"type": "text", "text": "bad"}]})
                continue
            if MODE == "noshape":
                reply(mid, {"content": [{"type": "text", "text": "hmm"}]})
                continue
            if any("\n" in str(q.get("query", "")) for q in args.get("searches") or []):
                # real qmd refuses a multi-line structured search
                reply(mid, {"isError": True, "content": [{"type": "text", "text":
                    "Structured search (vec): queries must be single-line. Remove newline characters."}]})
                continue
            wanted = args.get("collections") or []
            hits = [dict(h) for h in HITS] if "transcripts" in wanted or not wanted else []
            if MODE == "noline" and hits:
                del hits[0]["line"]
            text = (f"Found {len(hits)} results" if hits else "No results found")
            reply(mid, {"content": [{"type": "text", "text": text}],
                        "structuredContent": {"results": hits[: int(args.get("limit", 10))]}})
        # notifications (no id) get no reply
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
