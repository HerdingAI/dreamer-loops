#!/usr/bin/env python3
"""Ten-probe retrieval baseline (DoD 6.4).

"Ten owner-written natural-language probe queries against `wisdom` return the
expected book/passage in top-5 for >=8 of 10 (recorded as the retrieval
baseline)."

The probes below are seeded from books actually present in the corpus. They are
meant to be EDITED: the point is queries phrased the way the owner would really
ask, not the way a search engine likes. Re-run this after any retrieval change
(Q10) and after the monthly vault-health check.

Usage:
    python3 tests/retrieval_baseline.py              # BM25 (works without embeddings)
    python3 tests/retrieval_baseline.py --mode query # full hybrid + rerank
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (probe, substring that must appear in a top-5 result path)
PROBES: list[tuple[str, str]] = [
    ("what does stoicism say about controlling what you can influence", "Stoic"),
    ("how do fragile systems differ from ones that gain from disorder", "Antifragile"),
    # NOTE: probes originally targeted Algorithms to Live By, 48 Laws of Power
    # and The Age of Surveillance Capitalism. All three were purged as DRM-
    # encrypted, so those probes tested for content that is not in the corpus --
    # an invalid test, not a retrieval failure. Replaced with confirmed-present
    # targets. Questions were written before running, not fitted to results.
    ("how did shared fictions let large groups of strangers cooperate", "sapiens"),
    ("what makes a game infinite rather than finite", "Finite"),
    ("what common reasoning errors should I watch for in my own judgment", "Thinking"),
    ("what drives long-run inequality and political instability", "Discord"),
    ("is there an economic model that respects ecological limits", "Doughnut"),
    ("what separates a startup that scales from one that stalls", "Startup"),
    ("how should I think about deliberate practice and expertise", "Psycology"),
    ("what does the research say about incentives and motivation", "Economics"),
]


def run_probe(query: str, mode: str, n: int = 5) -> list[str]:
    cmd = ["qmd", mode, query, "-c", "wisdom", "-n", str(n)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           cwd=str(ROOT))
    except subprocess.SubprocessError:
        return []
    paths = []
    for line in r.stdout.splitlines():
        if line.startswith("qmd://wisdom/"):
            paths.append(line.split()[0])
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="search", choices=["search", "query", "vsearch"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    results, hits = [], 0
    for probe, expect in PROBES:
        paths = run_probe(probe, args.mode)
        hit = any(expect.lower() in p.lower() for p in paths)
        hits += hit
        results.append({"probe": probe, "expect": expect, "hit": hit,
                        "top": paths[:5]})
        mark = "\033[32m✓\033[0m" if hit else "\033[31m✗\033[0m"
        print(f"  {mark} {probe[:64]:<64} -> {expect}")
        if not hit and paths:
            print(f"      got: {', '.join(p.split('/')[-1] for p in paths[:3])}")

    gate = 8
    print(f"\n  baseline ({args.mode}): {hits}/10  "
          f"{'PASS' if hits >= gate else 'BELOW GATE (need 8/10)'}")
    payload = {"mode": args.mode, "hits": hits, "gate": gate, "results": results}
    dest = Path(args.out) if args.out else ROOT / "vault/.vault-meta/retrieval-baseline.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  recorded: {dest}")
    return 0 if hits >= gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
