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
    # The solution spine (rule 5b): a scripted loop may carry spine fields,
    # which the applier must validate exactly as it would a real reply.
    if spec.get("problem_type"):
        reply["problem_type"] = spec["problem_type"]
        for key in ("define", "diagnosis", "assumptions", "options", "evaluation",
                    "recommended_option", "success_signal"):
            if key in spec:
                reply["sections"][key] = spec[key]
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


def assess_reply(prompt: str) -> dict:
    attachments = []
    observations = []
    if "G0001" in prompt:
        tr_links = re.findall(r"### (\[\[sources/transcripts/[^\]]+\]\])", prompt)
        for tr in tr_links:
            if "chess-movement" in tr:
                attachments.append({
                    "goal_id": "G0001",
                    "transcript": tr,
                    "kind": "movement",
                    "date": "2026-08-04",
                    "anchor": "I completed 40 puzzles today",
                    "summary": "Completed 40 puzzles",
                })
            elif "chess-mention" in tr:
                attachments.append({
                    "goal_id": "G0001",
                    "transcript": tr,
                    "kind": "mention",
                    "date": "2026-08-03",
                    "anchor": "I was thinking about the tournament date",
                    "summary": "Discussed tournament timing",
                })
    return {
        "attachments": attachments,
        "unattached": [],
        "observations": observations,
    }


def assess_fold_reply(prompt: str) -> dict:
    occ = re.search(r"^- wikilink: (\[\[[^\]]+\]\])$", prompt, re.M)
    title = re.search(r"^- title: (.+)$", prompt, re.M)
    link = occ.group(1) if occ else "[[sources/transcripts/unknown]]"
    t = title.group(1).strip() if title else "the goal"
    return {
        "now": f"The owner made progress on {t} ({link} via thread).",
        "trajectory_line": f"Advancement recorded on {t}",
    }

# --------------------------------------------------------------------------
# Knowledge-frontier calls. Each prompt's FIRST LINE is its marker:
# transcripts and goal pages can contain any marker string, so routing never
# looks past the first line. Every frontier PROMPT.md must have an entry here
# (tests/test_frontier_llm.py checks that each marker reaches its own responder).
# --------------------------------------------------------------------------

FRONTIER_SCRIPT = Path(os.environ.get(
    "DREAMER_FAKE_FRONTIER_SCRIPT", str(HERE / "fixtures" / "frontier" / "script.json")))


def _frontier_script() -> dict:
    return json.loads(FRONTIER_SCRIPT.read_text(encoding="utf-8"))


def frontier_extract_reply(prompt: str) -> dict:
    """Concepts per transcript, by slug, from the script's "extract" table."""
    table = _frontier_script().get("extract", {})
    concepts = []
    for rel in re.findall(r"^=== TRANSCRIPT (\S+) ===$", prompt, re.M):
        for c in table.get(rel.rsplit("/", 1)[-1], []):
            concepts.append({**c, "transcript": rel})
    return {"concepts": concepts}


def frontier_match_reply(prompt: str) -> dict:
    """attach when a neighbour has the concept's title, batch_ref when an
    earlier concept does, else new. The script's "match" table overrides by
    lower-cased title: "new", or {"attach": "K7001"}."""
    overrides = _frontier_script().get("match", {})
    earlier = {int(i): t.strip().lower() for i, t in
               re.findall(r"^- #(\d+) \| ([^|]*) \|", prompt, re.M)}
    for i, t in re.findall(r"^### Concept (\d+)\n- title: (.*)$", prompt, re.M):
        earlier[int(i)] = t.strip().lower()
    decisions = []
    for m in re.finditer(r"^### Concept (\d+)\n- title: (.*)\n- summary: .*\n((?:  - K.*\n?)*)",
                         prompt, re.M):
        idx, title = int(m.group(1)), m.group(2).strip().lower()
        neighbours = {t.strip().lower(): k for k, t in
                      re.findall(r"^  - (K\d+) \| ([^|]*) \|", m.group(3), re.M)}
        d = {"concept": idx, "decision": "new", "item": None, "batch_ref": None,
             "justification": "scripted: no clear match"}
        ov = overrides.get(title)
        if ov == "new":
            pass
        elif isinstance(ov, dict) and "attach" in ov:
            d.update(decision="attach", item=ov["attach"], justification="scripted attach")
        elif title in neighbours:
            d.update(decision="attach", item=neighbours[title], justification="same title")
        else:
            ref = next((i for i, t in sorted(earlier.items()) if t == title and i < idx), None)
            if ref is not None:
                d.update(decision="batch_ref", batch_ref=ref, justification="same title earlier")
        decisions.append(d)
    return {"decisions": decisions}


def frontier_premortem_reply(prompt: str) -> dict:
    """The script's "premortem" table by goal id; else one scripted cause."""
    m = re.search(r"^- id: (G\d+)$", prompt, re.M)
    table = _frontier_script().get("premortem", {})
    if m and m.group(1) in table:
        return table[m.group(1)]
    return {"causes": [{"cause": "Scripted failure cause",
                        "candidates": [{"title": "Scripted gap", "summary": "A scripted gap."}]}]}


def _candidate_title(prompt: str, label: str) -> str:
    m = re.search(rf"^- {label}: (.*)$", prompt, re.M)
    return m.group(1).strip().lower() if m else ""


def frontier_check_reply(prompt: str) -> dict:
    """The script's "check" table by candidate title (lower-cased); else none.
    A "requires" string models a judge that can only answer from what reached
    the prompt: if it is absent the reply is none."""
    none = {"owner_discussed": "none", "quote": "", "matches_item": None, "exposed_in": []}
    entry = dict(_frontier_script().get("check", {}).get(_candidate_title(prompt, "candidate title"), none))
    need = entry.pop("requires", None)
    return none if need and need not in prompt else entry


def frontier_consequence_reply(prompt: str) -> dict:
    table = _frontier_script().get("consequence", {})
    return table.get(_candidate_title(prompt, "candidate"),
                     {"verdict": "tactical", "reason": "scripted default"})


def _concept(prompt: str) -> str:
    return _candidate_title(prompt, "concept")


def frontier_queries_reply(prompt: str) -> dict:
    """The script's "queries" table by concept title (lower-cased)."""
    return _frontier_script().get("queries", {}).get(
        _concept(prompt), {"queries": ["how do practitioners test this idea"]})


def frontier_research_reply(prompt: str) -> dict:
    script = _frontier_script()
    reply = script.get("research", {}).get(_concept(prompt), {"results": []})
    # Stand in for the PostToolUse hook (scripts/egress_results_hook.py): record the URLs the
    # "search" returned. The script's "research_recorded" table overrides which ones, so a test
    # can have the model return a URL no search produced.
    path = os.environ.get("DREAMER_EGRESS_RESULTS")
    if path:
        recorded = script.get("research_recorded", {}).get(
            _concept(prompt), [r["url"] for r in reply.get("results", [])])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join(u + "\n" for u in recorded))
    return reply


def frontier_claims_reply(prompt: str) -> dict:
    return _frontier_script().get("claims", {}).get(_concept(prompt), {"claims": []})


def frontier_compose_reply(prompt: str) -> dict:
    return _frontier_script().get("compose", {}).get(
        _concept(prompt), {"teachable": False, "title": "", "body": ""})


def frontier_lesson_check_reply(prompt: str) -> dict:
    return _frontier_script().get("lesson_check", {}).get(
        _concept(prompt), {"verdict": "pass", "failed": [], "reasons": []})


FRONTIER_MARKERS = {
    "# Frontier extract": frontier_extract_reply,
    "# Frontier match": frontier_match_reply,
    "# Frontier premortem": frontier_premortem_reply,
    "# Frontier check": frontier_check_reply,
    "# Frontier consequence": frontier_consequence_reply,
    "# Frontier queries": frontier_queries_reply,
    "# Frontier research": frontier_research_reply,
    "# Frontier claims": frontier_claims_reply,
    "# Frontier compose": frontier_compose_reply,
    "# Frontier lesson-check": frontier_lesson_check_reply,
}


def frontier_route(prompt: str):
    """(call name, responder) when the first line is a frontier marker, else None."""
    first = prompt.split("\n", 1)[0].strip()
    fn = FRONTIER_MARKERS.get(first)
    return (first, fn) if fn else None


def frontier_failure(call: str, valid: dict, state_dir: Path):
    """Failure injection: DREAMER_FAKE_FRONTIER_FAIL="extract=malformed:1,match=exit".

    Modes: malformed (truncated JSON), empty, prose (valid JSON inside chatty
    prose), xml (a tool call written as text, what a sealed model does when it
    "wants" a tool), fenced (valid JSON in a ```json fence: tolerated), exit
    (nonzero exit, no output). ":n" limits the mode to the first n calls of
    that kind; the count lives in state_dir so it survives between calls.
    Returns the raw reply, or None for a normal answer."""
    for part in filter(None, os.environ.get("DREAMER_FAKE_FRONTIER_FAIL", "").split(",")):
        name, _, spec = part.partition("=")
        if name.strip() != call:
            continue
        mode, _, limit = spec.partition(":")
        counter = state_dir / f".fake-frontier-count-{call}"
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        if limit and n >= int(limit):
            return None
        body = json.dumps(valid)
        if mode == "malformed":
            return body[: max(1, len(body) // 2)]
        if mode == "empty":
            return ""
        if mode == "prose":
            return f"Sure, here is the JSON you asked for:\n{body}\nLet me know if you want changes."
        if mode == "xml":
            return "<function_calls><invoke name=\"Read\"><parameter name=\"file_path\">x</parameter></invoke></function_calls>"
        if mode == "fenced":
            return f"```json\n{body}\n```"
        if mode == "exit":
            raise SystemExit(3)
        raise SystemExit(f"unknown DREAMER_FAKE_FRONTIER_FAIL mode {mode!r}")
    return None


def main() -> int:
    prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
    routed = frontier_route(prompt)
    if routed:
        marker, fn = routed
        valid = fn(prompt)
        call = marker.split()[-1]
        raw = frontier_failure(call, valid, Path(sys.argv[1]).resolve().parent)
        sys.stdout.write(raw if raw is not None else json.dumps(valid, indent=2))
        return 0
    # Thread-fold first: its prompt embeds raw transcript content, which could
    # contain any of the other responders' marker strings.
    if "# Thread fold" in prompt:
        out = thread_fold_reply(prompt)
    elif "# Assessment fold" in prompt:
        out = assess_fold_reply(prompt)
    elif "# Dreamer — The Assessor" in prompt:
        out = assess_reply(prompt)
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
