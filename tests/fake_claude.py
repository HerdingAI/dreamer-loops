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
            # Fixture-driven so the sim exercises BOTH tag paths: a
            # vocabulary tag that must land in frontmatter, and an
            # out-of-vocabulary one the applier must drop (CLAUDE.md rule 4).
            "tags": spec.get("tags", []),
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
    # Thread rebuild (rules 13/15): when the prompt carries the derived
    # thread block, a researching dream may return a rebuilt Now citing only
    # this loop's occurrences — modelled here so apply_conclusion's
    # replace_now path is exercised end to end by the sim.
    if "### What Dreamer currently holds" in prompt:
        occs = re.findall(r"^- `(\[\[[^`]+\]\])`", prompt, re.M)
        if occs:
            reply["now"] = ("Refreshed after research: the question remains "
                            f"open ({occs[-1]} via thread).")
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


def summarize_reply(prompt: str) -> dict:
    """Deterministic stand-in for the session summariser (ingest-cc).

    Held to the same bar as the real one: whatever it returns has to survive
    apply_cc_session.check_clean, so it emits no fenced code, no filesystem
    paths and nothing secret-shaped. A fake that could not pass the production
    assertion would make that assertion untestable.
    """
    project = re.search(r"^- project: `([^`]+)`", prompt, re.M)
    hint = re.search(r'working title, as a hint only: "([^"]+)"', prompt)
    first = re.search(r"^\[human\] (.+)$", prompt, re.M)

    if hint:
        subject = hint.group(1)
    elif project:
        subject = project.group(1).strip("-").replace("-", " ")
    else:
        subject = "untitled session"

    # Only word characters and light punctuation survive, so a path or a key
    # in the source session cannot ride through the fake into the page.
    opener = re.sub(r"[^A-Za-z0-9 ,.'?-]", " ", first.group(1))[:80].strip() \
        if first else ""

    return {
        "title": subject,
        "goal": f"Wanted to work through {subject}.",
        "solution": "Settled on the approach talked through in the session.",
        "outcome": "The work described was carried out.",
        "unresolved": "Whether the approach holds once it meets real data.",
        "turns": [
            {"role": "human", "text": opener or "Opened the session."},
            {"role": "assistant",
             "text": "Laid out the options and asked which one to take."},
        ],
    }


def thread_fold_reply(prompt: str) -> dict | str:
    """Deterministic stand-in for the thread-fold skill (rule 15).

    Held to the same discipline the real prompt demands: it cites ONLY the
    occurrence wikilink named in the prompt header, and it never reproduces
    transcript content — directive-shaped text in the occurrence is described,
    never copied (rule 10). That restraint is what makes the sim's
    "directive never reaches the page" assertion meaningful; the applier-level
    guarantees live in scripts/apply_thread.py's validation contract.
    """
    occ = re.search(r"^- wikilink: (\[\[[^\]]+\]\])$", prompt, re.M)
    title = re.search(r"^- title: (.+)$", prompt, re.M)
    link = occ.group(1) if occ else "[[sources/transcripts/unknown]]"

    # Failure mode for the sim's bounded-retry scenario: when the env names
    # this fold's loop id or an occurrence substring, the responder violates
    # the JSON contract the way a real model does — prose instead of payload
    # (returned raw so main() emits it verbatim and apply_thread refuses it).
    fail = os.environ.get("DREAMER_FAKE_FOLD_FAIL", "")
    if fail:
        loop_id = re.search(r"^- id: (L\d+)$", prompt, re.M)
        ident = (loop_id.group(1) if loop_id else "") + " " + link
        if fail in ident:
            return ("I looked at the occurrence but the thread feels "
                    "unchanged, so I will just describe it in prose instead "
                    "of the JSON contract.")
    t = (title.group(1).strip().rstrip("?").lower()
         if title else "the idea")
    now = (f"The owner returned to {t} without settling it "
           f"({link} via thread). The latest pass restates the question "
           f"rather than resolving it ({link} via thread).")
    return {"now": now,
            "trajectory_line": "returned to the question; still unresolved"}


def main() -> int:
    prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
    # Thread-fold first: its prompt embeds raw transcript content, which could
    # contain any of the other responders' marker strings.
    if "# Thread fold" in prompt:
        out = thread_fold_reply(prompt)
    elif "ingest-cc" in prompt or "Session reconstruction" in prompt:
        out = summarize_reply(prompt)
    elif "Stage-B pairwise judgment" in prompt:
        out = judge_reply(prompt)
    elif "weekly-dream" in prompt or "Step 1 — Route" in prompt:
        out = dream_reply(prompt)
    else:
        out = extraction_reply(prompt)
    sys.stdout.write(out if isinstance(out, str) else json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
