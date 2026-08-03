#!/usr/bin/env python3
"""Conclusion-quality rubric (§6.5 outcome grading).

Matching has the golden set; conclusions had nothing. A run could produce a
page that cited correctly, lint cleanly, and still be worthless — and the only
detector was someone reading it. Two live runs of L0004 on identical input, one
restating a framework and one committing to a recommendation, made that gap
concrete: the variance was real and unmeasured.

WHAT THIS DOES AND DOES NOT MEASURE
-----------------------------------
These checks are STRUCTURAL. They ask whether a conclusion has the properties a
useful one must have — did it commit, did it cite, did it grade its evidence,
did it stay honest about gaps. They cannot judge whether the answer is *correct*;
only the owner can. A high score means "worth reading", never "right".

That distinction is the whole point. The failure mode this guards against is a
fluent page that hedges everything and decides nothing, which reads as
thoughtful and is useless. Treat the score as a floor on quality, not a verdict.

Usage:
    python3 scripts/grade_conclusions.py             # grade all, table + summary
    python3 scripts/grade_conclusions.py --json      # machine-readable
    python3 scripts/grade_conclusions.py --loop L0004  # variance across re-runs
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import p, read_page  # noqa: E402

# Hedges that signal a conclusion declining to conclude. Counted only in the
# synthesis: hedging inside a graded claim is honest, hedging in the answer is
# the failure.
_HEDGE = re.compile(
    r"\b(it depends|hard to say|difficult to say|could go either way|"
    r"there is no right answer|no single answer|various factors|"
    r"ultimately up to you|your mileage may vary|it varies)\b", re.I)

# Language that names what would settle the question — the single most useful
# property a conclusion can have, because it converts an opinion into a test.
_DECIDING = re.compile(
    r"\b(the (?:decisive|deciding|key) (?:factor|number|question|measurement)|"
    r"would settle|settles? (?:this|it)|what would change|"
    r"(?:never|not) been (?:measured|run|executed|taken|supplied)|unmeasured|"
    r"measure (?:what|how|the)|the honest test|decides? (?:this|whether)|"
    r"remains? open|still (?:needs?|open|unanswered)|"
    r"requires? (?:running|executing|the owner)|"
    r"(?:only|until) .{0,40}(?:resolves?|settles?|answers?) (?:this|it)|"
    r"an action the owner needs to take|owner decision|"
    # Added 2026-08-03 from confirmed false negatives: pages that named the
    # deciding test in plain words this list happened not to carry. Measured
    # against the pre-existing corpus BEFORE the dream prompt was told to ask
    # for this, so these are genuine misses — not the rubric being taught to
    # match its own instructions.
    r"remains? untested|\buntested\b|\bunverified\b|"
    r"no (?:controlled |direct )?evidence|not (?:yet )?been tested|"
    r"would (?:resolve|confirm|refute|answer)|"
    # The trailing space here used to sit inside the group, so `\b` demanded a
    # word character after it and "cannot be answered." never matched.
    r"undocumented|not established|cannot be (?:computed|answered))\b",
    re.I)

# A recommendation actually committed to, rather than options laid out.
_COMMITS = re.compile(
    r"\b(do not|don't|the answer is|the move is|choose|pick|stop|start|"
    r"should (?:be|do|go|stay|run|use|keep)|recommend|the right (?:move|answer)|"
    r"first-order task|concrete answer)\b", re.I)


def _section(body: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
                  body, re.M | re.S)
    return m.group(1).strip() if m else ""


def grade(path: Path) -> dict:
    fm, body = read_page(path)
    synth = _section(body, "Synthesis")
    subs = _section(body, "Open sub-questions")
    ledger = _section(body, "Evidence ledger")

    claims = re.findall(r"^\s*-\s+\*\*\[(.+?)\]\*\*", body, re.M)
    graded = [c for c in claims
              if any(k in c for k in ("accepted", "provisional",
                                      "contested", "unsupported"))]
    accepted = sum(1 for c in claims if "accepted" in c)
    weak = len(graded) - accepted
    citations = len(re.findall(r"^\s+- source: `", body, re.M))

    checks: dict[str, bool] = {}

    # 1. It answered. A synthesis that only restates the question is the
    #    single most common way to look thoughtful and help nobody.
    checks["has_synthesis"] = len(synth) > 300
    checks["commits_to_an_answer"] = bool(_COMMITS.search(synth))
    checks["not_pure_hedge"] = not (
        len(_HEDGE.findall(synth)) >= 2 and not _COMMITS.search(synth))

    # 2. It is checkable. Claims cited AND graded; a citation alone proves a
    #    source said it, nothing more (rule 10).
    checks["every_claim_cited"] = bool(claims) and citations >= len(claims)
    checks["claims_are_graded"] = bool(claims) and len(graded) == len(claims)
    checks["has_evidence_ledger"] = bool(ledger)
    checks["weak_evidence_flagged"] = (
        weak <= accepted or "provisional or weaker" in body)

    # 3. It is honest about its own limits.
    checks["names_what_would_settle_it"] = bool(_DECIDING.search(synth + subs))
    checks["leaves_open_questions"] = subs.count("\n- ") + subs.count("- ") > 0
    checks["confidence_declared"] = str(fm.get("confidence", "")) in (
        "high", "medium", "low")

    # 4. It used the owner's own prior work — the entire reason this system
    #    exists rather than asking a chatbot. Only PRIMARY provenance counts:
    #    transcripts are the owner; conclusion pages and resurfacing notes are
    #    Dreamer's own output (rule 13) and rewarding them here bred the very
    #    self-citation chain this rubric now checks for.
    checks["uses_owner_prior_work"] = bool(
        _section(body, "What you previously concluded")
        or re.search(r"\[\[sources/transcripts/", body))

    # 5. It did not treat its own prior output as evidence (rule 13). A
    #    derived citation may only appear inside the quarantined "Prior
    #    conclusions (derived …)" section, graded contested or worse.
    derived_cites = re.findall(
        r"^\s+- source: `\s*\[\[\s*(?:conclusions/|sources/resurfacings/|"
        r"loops/)", body, re.M)
    quarantine = _section(body, "Prior conclusions (derived — hypothesis, "
                          "not evidence)")
    quarantined_cites = re.findall(
        r"^\s+- source: `\s*\[\[\s*(?:conclusions/|sources/resurfacings/|"
        r"loops/)", quarantine, re.M)
    checks["no_derived_citations_as_evidence"] = (
        len(derived_cites) == len(quarantined_cites))

    score = sum(checks.values()) / len(checks)
    return {
        "file": path.name,
        "loop": fm.get("loop", ""),
        "route": fm.get("route", ""),
        "confidence": fm.get("confidence", ""),
        "created": str(fm.get("created", "")),
        "superseded": bool(fm.get("superseded_by")),
        "claims": len(claims), "accepted": accepted, "weak": weak,
        "wisdom_citations": body.count("qmd://"),
        "score": round(score, 3),
        "failed": [k for k, v in checks.items() if not v],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--loop", help="only this loop (shows re-run variance)")
    args = ap.parse_args()

    rows = [grade(f) for f in sorted(p("conclusions").glob("*.md"))]
    if args.loop:
        rows = [r for r in rows if r["loop"] == args.loop]
    if not rows:
        print("no conclusions to grade")
        return 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"{'loop':6} {'route':14} {'conf':7} {'qmd':>4} {'claims':>6} "
          f"{'score':>6}  file")
    for r in sorted(rows, key=lambda x: (x["loop"], x["created"])):
        tag = "  (superseded)" if r["superseded"] else ""
        print(f"{r['loop']:6} {r['route']:14} {r['confidence']:7} "
              f"{r['wisdom_citations']:>4} {r['claims']:>6} "
              f"{r['score']:>6.0%}  {r['file'][:44]}{tag}")
        if r["failed"]:
            print(f"{'':38}fails: {', '.join(r['failed'])}")

    scores = [r["score"] for r in rows]
    print(f"\nn={len(rows)}  mean={statistics.mean(scores):.0%}  "
          f"min={min(scores):.0%}  max={max(scores):.0%}")
    if len(scores) > 1:
        spread = max(scores) - min(scores)
        print(f"spread={spread:.0%}"
              + ("  <- re-runs of one loop should not differ this much"
                 if args.loop and spread > 0.15 else ""))

    # A superseded page and its replacement are successive VERSIONS, not two
    # samples of the same process. Reporting 73% -> 100% as "variance" would
    # describe a fix landing as instability, and would hide real variance
    # underneath it. Only same-generation re-runs measure variance.
    current = [r for r in rows if not r["superseded"]]
    superseded = [r for r in rows if r["superseded"]]
    if superseded and not args.loop:
        print(f"\ncurrent conclusions: n={len(current)} "
              f"mean={statistics.mean([r['score'] for r in current]):.0%}  "
              f"(superseded: n={len(superseded)} "
              f"mean={statistics.mean([r['score'] for r in superseded]):.0%})")
        # Compare the newest superseded version against the current one, per
        # loop. Listing every historical version turns one transition into
        # several confusing rows.
        moves = []
        for loop in sorted({s["loop"] for s in superseded}):
            prev = sorted([s for s in superseded if s["loop"] == loop],
                          key=lambda x: x["created"])
            cur = [c for c in current if c["loop"] == loop]
            if prev and cur:
                moves.append((loop, prev[-1]["score"],
                              max(c["score"] for c in cur)))
        if moves:
            print("version change (newest superseded -> current):")
            for loop, before, after in moves:
                delta = after - before
                # A re-run that scores LOWER is the signal worth having. Left
                # unmarked it reads as ordinary churn, and re-researching a
                # loop would quietly degrade it.
                flag = ("  <- REGRESSION" if delta < -0.001
                        else "" if delta < 0.001 else "  improved")
                print(f"  {loop}: {before:.0%} -> {after:.0%}{flag}")

    by_loop: dict[str, list[float]] = {}
    for r in current:
        by_loop.setdefault(r["loop"], []).append(r["score"])
    repeats = {k: v for k, v in by_loop.items() if len(v) > 1}
    if repeats and not args.loop:
        print("\nre-run variance (same loop, same generation):")
        for k, v in sorted(repeats.items()):
            spread = max(v) - min(v)
            print(f"  {k}: {len(v)} runs, {min(v):.0%}-{max(v):.0%}, "
                  f"spread {spread:.0%}"
                  + ("  <- unstable" if spread > 0.15 else ""))

    print("\nStructural only: a high score means 'worth reading', never "
          "'correct'. Only the owner can judge correctness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
