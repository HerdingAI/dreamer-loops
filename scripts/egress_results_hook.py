#!/usr/bin/env python3
"""PostToolUse hook for the gated research call (provenance for returned URLs).

Records every http(s) URL found anywhere in a WebSearch's `tool_response` to the
file named by DREAMER_EGRESS_RESULTS, one normalised URL per line. Code fetches a
URL the research call returns only if it is in this file, so a hijacked result
cannot make the model invent a URL that carries context out. Fail closed: any
failure is logged and records nothing, which means nothing is fetched.
stdout stays empty (the hook has no opinion on the result).

Environment: DREAMER_EGRESS_RESULTS (the record file), DREAMER_EGRESS_LOG
(JSON-lines log, where a failure is noted).
"""

from __future__ import annotations

import json
import os
import sys

# Trailing punctuation that prose and markdown add after a URL but a URL rarely ends in.
_TRAILING = ".,;:!?)]}'\""


def result_urls(payload) -> set[str]:
    """Normalised http(s) URLs anywhere in the serialised tool_response."""
    import egress_check as ec
    if not isinstance(payload, dict) or payload.get("tool_name") != "WebSearch":
        return set()
    text = json.dumps(payload.get("tool_response"), ensure_ascii=False)
    out: set[str] = set()
    for m in ec.URL_IN_TEXT_RE.finditer(text):
        raw = m.group(0)
        # a markdown link or sentence puts punctuation after the URL: keep both readings
        for cand in {raw, raw.rstrip(_TRAILING)}:
            norm = ec.normalize_url(cand)
            if norm:
                out.add(norm)
    return out


def main() -> int:
    results_path = os.environ.get("DREAMER_EGRESS_RESULTS")
    log_path = os.environ.get("DREAMER_EGRESS_LOG")
    # Deliberately broad: nothing may escape as a traceback, and a failure here
    # must leave the record empty rather than half-written.
    try:
        if not results_path:
            raise RuntimeError("DREAMER_EGRESS_RESULTS not set")
        payload = json.loads(sys.stdin.read())
        urls = result_urls(payload)
        if urls:
            body = "".join(u + "\n" for u in sorted(urls))
            fd = os.open(results_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, body.encode("utf-8"))
            finally:
                os.close(fd)
    except Exception as exc:  # noqa: BLE001 - fail closed, see above
        try:
            import egress_check as ec
            if log_path:
                ec.log_decision(log_path, decision="record_failed", tool="WebSearch",
                                reason=f"result URLs not recorded: {exc!r}")
        except Exception:  # noqa: BLE001 - the log itself failed; nothing more to do
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
