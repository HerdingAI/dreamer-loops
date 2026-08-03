#!/usr/bin/env python3
"""Deterministic stand-in for `claude -p`, used by the simulated-week test.

DoD 6.9 requires an end-to-end run producing a correct digest, correct states
and a clean lint. That is only a meaningful gate if it is repeatable, so the
LLM half is replaced by a scripted responder while every deterministic
component — converter, state machine, decay clock, catalog, digest, lint, git —
runs for real.

Reads the rendered prompt on argv[1], returns the JSON the real model would.
Behaviour is driven by tests/fixtures/script.json so the fixtures, not this
file, describe the scenario.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = json.loads((HERE / "fixtures" / "script.json").read_text(encoding="utf-8"))


def extraction_reply(prompt: str) -> dict:
    # The prompt lists batch members as "- `sources/transcripts/...`".
    paths = re.findall(r"^- `(sources/transcripts/[^`]+)`", prompt, re.M)
    candidates, skipped = [], []
    for path in paths:
        # Batch entries are vault-relative and suffix-free; fixture keys may or
        # may not carry .md. Normalise both sides rather than trusting either.
        slug = path.rsplit("--", 1)[-1].removesuffix(".md")
        spec = (SCRIPT["transcripts"].get(slug)
                or SCRIPT["transcripts"].get(slug + ".md"))
        if spec is None:
            skipped.append({"topic": slug, "reason": "no loop in this conversation"})
            continue
        if spec.get("skip"):
            skipped.append({"topic": slug, "reason": spec["skip"]})
            continue
        date = re.search(r"/(\d{4})/(\d{2})/(\d{4}-\d{2}-\d{2})--", path)
        candidates.append({
            "title": spec["title"],
            "transcript": path,
            "date": date.group(3) if date else "2026-07-01",
            "theme_note": spec.get("theme", "untagged theme note"),
            "evidence": spec.get("evidence", "left open at end of conversation"),
            "match": {
                "decision": spec["decision"],
                "loop_id": spec.get("loop_id"),
                "considered": spec.get("considered", []),
                "justification": spec.get("justification", "scripted fixture decision"),
            },
        })
    return {"candidates": candidates, "skipped": skipped}


def dream_reply(prompt: str) -> dict:
    m = re.search(r"- \*\*id\*\*: `(L\d+)`", prompt)
    loop_id = m.group(1) if m else "L0001"
    title = ""
    tm = re.search(r"- \*\*title\*\*: (.+)", prompt)
    if tm:
        title = tm.group(1).strip()
    spec = SCRIPT["dreams"].get(loop_id, SCRIPT["dreams"]["default"])

    # Reproduces the L0012 live failure (2026-08-02): the model answers in
    # sensible prose instead of the JSON contract. Returned as a raw string so
    # main() emits it verbatim and apply_conclusion gets genuinely unparseable
    # output.
    if os.environ.get("DREAMER_FAKE_MALFORMED") == loop_id:
        return ("I already researched this loop earlier today — see the "
                "existing conclusion. Nothing new to add, so no JSON payload.")

    if spec["route"] == "decision-only":
        return {
            "loop_id": loop_id, "route": "decision-only",
            "title": f"Decision: {title}", "confidence": "high",
            "sections": {"restated": f"{title} — no research resolves this."},
            "decision_framing": spec.get("framing", "You must simply choose."),
            "web_queries": [], "fetched_urls": [], "proposed_tags": [],
        }

    reply = {
        "loop_id": loop_id, "route": spec["route"],
        "title": f"Conclusion: {title}", "confidence": spec.get("confidence", "medium"),
        "sections": {
            "restated": f"The loop asks: {title}",
            "wisdom_says": spec.get("wisdom_says", []),
            "web_says": spec.get("web_says", []),
            "owner_previously_concluded": spec.get("past", []),
            "synthesis": spec.get("synthesis", "A synthesised answer."),
            "open_sub_questions": spec.get("subs", []),
        },
        "web_queries": spec.get("web_queries", []),
        "fetched_urls": spec.get("fetched_urls", []),
        "proposed_tags": spec.get("proposed_tags", []),
    }
    return reply


def judge_reply(prompt: str) -> dict:
    """Stage-B pairwise judgment (CLAUDE.md rule 7).

    The merge proposer routes every candidate pair through this judge. Before
    this existed the judge prompt fell through to extraction_reply, which
    returned a candidates object with no `verdict` — judge_llm read that as an
    error, and the simulated week reported zero merge proposals while looking
    like a clean pass. Deterministic token overlap stands in for the model so
    the merge leg is actually exercised end to end.
    """
    a = re.search(r"^- \*\*A\*\*: (.+)$", prompt, re.M)
    b = re.search(r"^- \*\*B\*\*: (.+)$", prompt, re.M)
    if not (a and b):
        return {"verdict": "distinct", "confidence": "low",
                "reason": "fake judge could not read the pair"}
    ta = set(re.findall(r"[a-z0-9]+", a.group(1).lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.group(1).lower()))
    overlap = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    same = overlap >= 0.55
    return {"verdict": "same" if same else "distinct",
            "confidence": "high" if same else "medium",
            "reason": f"fake judge: token overlap {overlap:.0%}"}


def main() -> int:
    prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
    if "Stage-B pairwise judgment" in prompt:
        out = judge_reply(prompt)
    elif "weekly-dream" in prompt or "Step 1 — Route" in prompt:
        out = dream_reply(prompt)
    else:
        out = extraction_reply(prompt)
    sys.stdout.write(out if isinstance(out, str) else json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
