#!/usr/bin/env python3
"""Retrieval recall against the owner's actual speech-to-text.

This gate exists because the owner rejected the probes I wrote. Hand-written
probes scored 6/6 while real conversation openings — long, rambling, dictated,
grammatically broken — scored 5/14. Probes authored by the same party that
wrote the expectations measure agreement with myself, not recall.

Method: sample transcripts that a loop cites as an occurrence, take the raw
opening of the owner's first message verbatim, and ask whether
`search_insights` returns that loop in the top 3. The probe text is never
cleaned; the noise IS the test.

Deterministic by default (fixed seed) so the number is comparable across runs
as the corpus grows.

    python3 scripts/probe_recall.py            # sampled, table + gate verdict
    python3 scripts/probe_recall.py --n 40     # bigger sample
    python3 scripts/probe_recall.py --json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dreamer_mcp as M  # noqa: E402
import vault as V  # noqa: E402
from dreamer_common import p  # noqa: E402

GATE = 0.80
PROBE_CHARS = 400


def _openings() -> dict[str, list[str]]:
    """transcript stem -> loop ids citing it."""
    out: dict[str, list[str]] = {}
    for loop in V.load_loops():
        for occ in loop.occurrences or []:
            m = re.search(r"transcripts/([^\]|]+)", occ)
            if m:
                out.setdefault(m.group(1).strip(), []).append(loop.id)
    return out


def run(n: int, seed: int) -> dict:
    tmap = _openings()
    rng = random.Random(seed)
    pool = sorted(tmap.items())
    picks = rng.sample(pool, min(n, len(pool)))

    rows = []
    for rel, ids in picks:
        # p("sources") already resolves to .../vault/sources/transcripts.
        path = p("sources") / f"{rel}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"## Human\n(.*?)(?=\n## )", text, re.S)
        if not m:
            continue
        probe = " ".join(m.group(1).split())[:PROBE_CHARS]
        data = json.loads(M.tool_search_insights(probe))
        got = [r.get("id") for r in data.get("results", [])][:3]
        rows.append({"transcript": rel, "expected": ids, "top3": got,
                     "hit": any(i in got for i in ids)})

    hits = sum(1 for r in rows if r["hit"])
    total = len(rows)
    return {"hits": hits, "total": total,
            "rate": (hits / total) if total else 0.0,
            "gate": GATE, "passed": total > 0 and (hits / total) >= GATE,
            "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=14)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = run(args.n, args.seed)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res["passed"] else 1

    for r in res["rows"]:
        mark = "HIT " if r["hit"] else "MISS"
        name = r["transcript"].split("--")[-1][:44]
        print(f"{mark} expect {r['expected']} got {r['top3']}  [{name}]")
    print(f"\n{res['hits']}/{res['total']} top-3 = {res['rate']:.0%} "
          f"(gate {GATE:.0%})")
    print("GATE PASS" if res["passed"] else "GATE FAIL")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
