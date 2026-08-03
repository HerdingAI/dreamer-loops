#!/usr/bin/env python3
"""Phase 0.5a calibration tooling (§6.8 DoD).

Three owner-facing artefacts, none of which the system may decide for itself:

  histogram  — the recurrence_count distribution. RECURRENCE_MIN is set from
               THIS, not from the default, because the threshold's selectivity
               is entirely scale-dependent: at multi-year scale a threshold of 2
               admits nearly everything and the real filter silently becomes
               "top N by count".
  sample     — 25 random loops for the blocking ≥70% "genuinely open/recurring
               as stated" gate.
  golden     — scaffold for the 20-pair matching golden set, of which ≥5 must be
               owner-authored adversarial near-misses (sampling only from
               pipeline output measures the easy region and biases upward).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, atomic_write, atomic_write_json, p, today  # noqa: E402
import vault as V  # noqa: E402


def histogram() -> dict:
    loops = V.load_loops(include_archived=True)
    counts = Counter(l.recurrence_count for l in loops)
    total = len(loops)
    lines = ["# Recurrence histogram", "",
             f"_{total} loops from the Phase 0.5a slice, {today().isoformat()}._", "",
             "`RECURRENCE_MIN` is set from this distribution (§6.8), not from the "
             "default. The question it answers: at what threshold does the filter "
             "actually filter?", "",
             "| recurrence_count | loops | share | cumulative ≥ |",
             "|---|---|---|---|"]
    cumulative = {}
    for k in sorted(counts):
        at_or_above = sum(v for kk, v in counts.items() if kk >= k)
        cumulative[k] = at_or_above
        share = counts[k] / total * 100 if total else 0
        lines.append(f"| {k} | {counts[k]} | {share:.0f}% | {at_or_above} "
                     f"({at_or_above / total * 100 if total else 0:.0f}%) |")

    lines += ["", "## Reading this", ""]
    if total:
        eligible_at_2 = cumulative.get(2, 0)
        pct2 = eligible_at_2 / total * 100
        lines.append(f"- At `RECURRENCE_MIN=2`, **{eligible_at_2} of {total} loops "
                     f"({pct2:.0f}%)** are research-eligible.")
        if pct2 > 60:
            lines.append("- That is permissive: the threshold is not doing the "
                         "filtering, ranking is. Consider raising it, or accept "
                         "that recency-weighted rank is the real filter.")
        elif pct2 < 5:
            lines.append("- That is restrictive: expect many consecutive quiet "
                         "weeks. Consider lowering it, or widening the slice.")
        else:
            lines.append("- That is a workable filter: the threshold excludes the "
                         "long tail without starving the weekly run.")
        for cand in (2, 3, 4):
            n = cumulative.get(cand, 0)
            lines.append(f"- `RECURRENCE_MIN={cand}` -> {n} eligible loop(s).")
    lines += ["", "## To apply", "",
              "Set `matching.recurrence_min` in `config.yaml` and change "
              "`recurrence_min_source` to cite this file.", ""]
    dest = p("digests") / "recurrence-histogram.md"
    atomic_write(dest, "\n".join(lines) + "\n")
    return {"path": str(dest), "total": total,
            "distribution": {str(k): v for k, v in sorted(counts.items())}}


def sample(n: int = 25) -> dict:
    loops = V.load_loops()
    rng = random.Random(today().toordinal())
    picked = rng.sample(loops, min(n, len(loops)))
    lines = ["# Phase 0.5a calibration sample", "",
             f"_{len(picked)} of {len(loops)} loops, {today().isoformat()}._", "",
             "**Blocking gate: ≥70% must be judged 'genuinely open/recurring as "
             "stated'.** Below that, the extraction prompt iterates and the "
             "affected batches re-run (git rewind to the batch boundary) before "
             "anything downstream proceeds.", "",
             "Tick the box if the loop is genuinely open and stated fairly.", ""]
    for l in picked:
        lines.append(f"- [ ] `{l.id}` — **{l.title}**")
        lines.append(f"  - seen {l.recurrence_count}× · first {l.first_seen} · "
                     f"last {l.last_seen}")
        for occ in l.occurrences[:3]:
            lines.append(f"  - {occ}")
    lines += ["", "---", "",
              "## Also needed here: the golden set (§6.6)", "",
              "While reading these, note pairs for the 20-pair matching golden "
              "set. **At least 5 must be adversarial near-misses you write "
              "yourself** — same words different question, opposite polarity, "
              "same topic different decision. Pairs sampled only from what the "
              "extractor already produced cannot contain the cases it never "
              "generated, so they measure the easy region and read high.", ""]
    dest = p("digests") / "calibration-sample.md"
    atomic_write(dest, "\n".join(lines) + "\n")
    return {"path": str(dest), "sampled": len(picked), "population": len(loops)}


def golden_scaffold() -> dict:
    loops = V.load_loops()
    rng = random.Random(today().toordinal() + 1)
    pairs = []
    if len(loops) >= 2:
        for _ in range(min(15, len(loops))):
            a, b = rng.sample(loops, 2)
            pairs.append({"a": a.id, "a_title": a.title,
                          "b": b.id, "b_title": b.title,
                          "owner_label": "", "source": "sampled"})
    # Three archetypes the extractor cannot generate, because it never produced
    # the negative case. Naming them beats "<near-miss A>" as a prompt.
    archetypes = [
        ("same topic, opposite polarity",
         "Should we adopt <X>?", "Should we drop <X>?", "distinct"),
        ("same topic, different decision",
         "Which <X> should we choose?", "How should we host the <X> we chose?",
         "distinct"),
        ("same words, different scope",
         "How do I handle this one <X>?", "How should I handle <X> in general?",
         "distinct"),
        ("same question, different words",
         "<restate a real loop in your own words>",
         "<the original loop's title>", "same"),
        ("adjacent but genuinely one thread",
         "<a narrower phrasing of a real loop>",
         "<a broader phrasing of the same loop>", "same"),
    ]
    scaffold = {
        "_instructions": (
            "owner_label must be 'same' or 'distinct'. Target 10 of each. "
            "At least 5 pairs must have source='adversarial' and be written by "
            "hand — near-misses the extractor would never have produced, because "
            "it never generated the negative case. Replace the <angle-bracket> "
            "text with real phrasings from your own loops; the runner rejects "
            "any pair still carrying template text. "
            "Score with: python3 scripts/golden_set.py run --judge llm"),
        "pairs": pairs + [
            {"a": "", "a_title": a, "b": "", "b_title": b,
             "owner_label": "", "suggested_label": label,
             "archetype": name, "source": "adversarial"}
            for name, a, b, label in archetypes],
    }
    dest = p("meta") / "golden-set.json"
    if dest.exists():
        return {"path": str(dest), "skipped": "already exists — not overwritten"}
    atomic_write_json(dest, scaffold)
    return {"path": str(dest), "pairs": len(scaffold["pairs"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["histogram", "sample", "golden", "all"])
    args = ap.parse_args()
    out = {}
    if args.command in ("histogram", "all"):
        out["histogram"] = histogram()
    if args.command in ("sample", "all"):
        out["sample"] = sample()
    if args.command in ("golden", "all"):
        out["golden"] = golden_scaffold()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
