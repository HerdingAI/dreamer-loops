#!/usr/bin/env python3
"""Tag vocabulary bootstrap (§6.8 step 4, owner decision Q6).

Clusters backfilled loops thematically and proposes 15–25 tags with example
loops each. The owner edits and approves; the approved set is frozen into
`vault/.vault-meta/tag-vocabulary.json`, after which a final pass tags all
backfilled loops from it and lint enforces zero out-of-vocabulary tags.

Until that file exists, loops carry free-prose theme notes in the body and NO
frontmatter tags at all — preserving the no-invented-tags invariant (CLAUDE.md
rule 4) rather than letting a plausible-sounding tag leak into a page.

Clustering is a deterministic token-frequency pass, not an LLM call: the owner
is the judge of a vocabulary, and a cheap proposal they rewrite beats an
expensive one they rubber-stamp.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import atomic_write, atomic_write_json, log, p, read_json, today  # noqa: E402
import vault as V  # noqa: E402

STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "is", "are", "be", "been", "should", "how", "what", "why", "when",
    "do", "does", "can", "could", "would", "will", "i", "my", "it", "this",
    "that", "we", "owner", "they", "them", "their", "keep", "still", "actually",
    "really", "across", "between", "into", "from", "about", "than", "then",
    "there", "here", "which", "who", "whom", "whose", "not", "no", "yes", "more",
    "most", "less", "least", "own", "one", "two", "any", "some", "all", "each",
    "other", "such", "make", "makes", "made", "get", "gets", "got", "use",
    "uses", "using", "used", "way", "ways", "thing", "things", "actual",
    "without", "within", "while", "worth", "given", "being", "have", "has",
    # Comparatives, qualifiers and discourse glue read as themes to a frequency
    # counter but name no subject. A tag has to denote something.
    "rather", "high", "higher", "highest", "low", "lower", "best", "better",
    "good", "great", "right", "wrong", "actually", "reliable", "reliably",
    "defensible", "repeatable", "problem", "problems", "question", "questions",
    "data", "thing", "stuff", "kind", "sort", "type", "types", "level",
    "levels", "point", "points", "case", "cases", "part", "parts", "next",
    "first", "last", "same", "different", "specific", "general", "real",
    "true", "false", "much", "many", "long", "short", "hard", "easy", "new",
    "old", "just", "only", "even", "also", "well", "back", "over", "under",
    "before", "after", "where", "whether", "toward", "towards", "onto",
}

VOCAB_PATH_KEY = "meta"
VOCAB_NAME = "tag-vocabulary.json"


def tokens(text: str) -> list[str]:
    return [w for w in re.split(r"\W+", (text or "").lower())
            if len(w) > 3 and w not in STOP and not w.isdigit()]


def propose(target_min: int = 15, target_max: int = 25) -> dict:
    loops = V.load_loops(include_archived=True)
    if not loops:
        raise SystemExit("no loops to cluster — run the backfill first")

    freq = Counter()
    by_token: dict[str, list[V.Loop]] = defaultdict(list)
    for loop in loops:
        seen = set()
        for tok in tokens(f"{loop.title} {loop.body}"):
            if tok in seen:
                continue
            seen.add(tok)
            freq[tok] += 1
            by_token[tok].append(loop)

    # A useful tag groups several loops but is not near-universal: a token in
    # 90% of pages carries no information, and one in a single page is a note,
    # not a category.
    floor = max(2, int(len(loops) * 0.03))
    ceiling = max(floor + 1, int(len(loops) * 0.45))
    candidates = [(t, n) for t, n in freq.items() if floor <= n <= ceiling]
    candidates.sort(key=lambda x: (-x[1], x[0]))

    chosen: list[tuple[str, int]] = []
    claimed: set[str] = set()
    for tok, n in candidates:
        ids = {l.id for l in by_token[tok]}
        # Skip a token whose loops are already almost entirely covered — that is
        # a synonym of a tag we already picked, not a new theme.
        if ids and len(ids - claimed) / len(ids) < 0.4:
            continue
        chosen.append((tok, n))
        claimed |= ids
        if len(chosen) >= target_max:
            break

    lines = ["# Proposed tag vocabulary", "",
             f"_{len(chosen)} candidate tags from {len(loops)} loops, "
             f"{today().isoformat()}._", "",
             "This is a **proposal**, not a decision. Edit freely: rename, merge,",
             "delete, add. The point is a vocabulary you would actually use.", "",
             "When you are happy with it, freeze it:", "", "```bash",
             "python3 scripts/propose_tags.py freeze --tags tag-one tag-two ...",
             "```", "",
             "Until frozen, loops carry free-prose theme notes and no frontmatter",
             "tags at all — the agent may propose tags but never invent them",
             "(CLAUDE.md rule 4).", "",
             "| tag | loops | examples |", "|---|---|---|"]
    for tok, n in chosen:
        examples = "; ".join(f"`{l.id}` {l.title[:48]}" for l in by_token[tok][:3])
        lines.append(f"| `{tok}` | {n} | {examples.replace('|', '/')} |")

    if len(chosen) < target_min:
        lines += ["", f"> Only {len(chosen)} tags cleared the thresholds. That "
                  f"usually means the slice is small or thematically narrow — "
                  f"widen the backfill before freezing."]

    dest = p("digests") / "tag-vocabulary-proposal.md"
    atomic_write(dest, "\n".join(lines) + "\n")
    return {"path": str(dest), "proposed": [t for t, _ in chosen],
            "loops": len(loops)}


def freeze(tags: list[str]) -> dict:
    tags = sorted({t.strip().lower() for t in tags if t.strip()})
    if not tags:
        raise SystemExit("refusing to freeze an empty vocabulary")
    dest = p(VOCAB_PATH_KEY) / VOCAB_NAME
    atomic_write_json(dest, {"frozen_on": today().isoformat(), "tags": tags})
    log(f"vocabulary frozen: {len(tags)} tags", job="tags")
    return {"path": str(dest), "tags": tags}


def vocabulary() -> set[str] | None:
    data = read_json(p(VOCAB_PATH_KEY) / VOCAB_NAME, default=None)
    if not data:
        return None
    return set(data.get("tags") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["propose", "freeze", "show"])
    ap.add_argument("--tags", nargs="*", default=[])
    args = ap.parse_args()
    if args.command == "propose":
        print(json.dumps(propose(), indent=2))
    elif args.command == "freeze":
        print(json.dumps(freeze(args.tags), indent=2))
    else:
        vocab = vocabulary()
        print(json.dumps({"frozen": vocab is not None,
                          "tags": sorted(vocab) if vocab else []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
