#!/usr/bin/env python3
"""Render the weekly-dream prompt for one loop (§6.9 Job 2)."""
from __future__ import annotations
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dreamer_common import CFG, atomic_write, p  # noqa: E402
import vault as V  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    loops = {l.id: l for l in V.load_loops()}
    loop = loops.get(args.loop)
    if loop is None:
        print(f"no such loop: {args.loop}", file=sys.stderr)
        return 1

    tmpl = (ROOT / "skills" / "dream" / "PROMPT.md").read_text(encoding="utf-8")
    lines = [tmpl, "",
             f"- **id**: `{loop.id}`",
             f"- **title**: {loop.title}",
             f"- **status**: {loop.status}",
             f"- **recurrence_count**: {loop.recurrence_count} distinct conversations",
             f"- **first_seen**: {loop.first_seen}",
             f"- **last_seen**: {loop.last_seen}",
             f"- **max fetches**: {CFG['research']['max_fetches_per_loop']}",
             "",
             "### Occurrences (the owner's own words — read these first)",
             ""]
    for occ in loop.occurrences:
        target = V._resolve_wikilink(occ)
        lines.append(f"- `{occ}`" + (f" — absolute: `{target}`" if target else " — MISSING"))
    lines += ["", "### Loop page body", "", "```", loop.body.strip(), "```", ""]

    # Rule 14: a loop with an existing conclusion gets it injected as a labeled
    # hypothesis so the dream can make the serve/re-research call (Step 0) and,
    # if re-researching, re-test claims instead of silently re-deriving them
    # from its own prior page.
    if loop.conclusion:
        target = V._resolve_wikilink(loop.conclusion)
        if target and target.exists():
            lines += ["### Prior conclusion (derived — hypothesis under "
                      "re-test, not evidence)", "",
                      f"From `{loop.conclusion}`. Apply Step 0 first: serve it "
                      "unless the occurrences above genuinely add, contradict, "
                      "or dispute something. Never cite this page as support.",
                      "", "```",
                      target.read_text(encoding="utf-8").strip(), "```", ""]

    lines += [
              "Return the JSON object now. Nothing else."]
    atomic_write(Path(args.out), "\n".join(lines))
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
