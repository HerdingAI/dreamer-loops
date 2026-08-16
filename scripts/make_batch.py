#!/usr/bin/env python3
"""Select the next batch of un-extracted transcripts and render the prompt.

Checkpointing lives here (§6.8): `.vault-meta/extracted.json` records every
transcript that has been through extraction. Backfill resumes by asking for the
next batch — there is no separate resume path to get out of sync with.

Chronological, oldest first, so recurrence accretes the way it did in life and
Stage-A always matches against the loop set as it existed at that time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, atomic_write, atomic_write_json, p, read_json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def all_transcripts() -> list[Path]:
    base = p("sources")
    return sorted(base.rglob("*.md"), key=lambda f: (f.parent.parts, f.name))


def extracted_set() -> set[str]:
    data = read_json(p("meta") / "extracted.json", default={"done": []}) or {}
    return set(data.get("done") or [])


def mark_extracted(paths: list[str]) -> None:
    path = p("meta") / "extracted.json"
    data = read_json(path, default={"done": []}) or {"done": []}
    done = set(data.get("done") or [])
    done.update(paths)
    atomic_write_json(path, {"done": sorted(done)})


def rel_of(f: Path) -> str:
    """Vault-relative, suffix-free — the form used in occurrence wikilinks."""
    return str(f.relative_to(p("vault"))).removesuffix(".md")


def select(limit: int) -> list[Path]:
    done = extracted_set()
    out = []
    for f in all_transcripts():
        if rel_of(f) in done:
            continue
        out.append(f)
        if len(out) >= limit:
            break
    return out


def _vocabulary_section() -> str:
    """The frozen tag vocabulary as a prompt section, or '' pre-freeze.

    Injected here rather than hardcoded in PROMPT.md so the prompt always
    matches vault/.vault-meta/tag-vocabulary.json — the same file lint and
    apply_extraction enforce against. While no vocabulary exists, no section
    is injected and PROMPT.md instructs the model to emit no tags at all
    (CLAUDE.md rule 4's pre-freeze state).
    """
    try:
        import propose_tags
        vocab = propose_tags.vocabulary()
    except Exception:  # noqa: BLE001 — batch selection must not die on this
        vocab = None
    if not vocab:
        return ""
    lines = ["## Approved tag vocabulary", "",
             "The frozen controlled vocabulary (CLAUDE.md rule 4). A candidate's",
             "`tags` array may draw ONLY from this list:", ""]
    lines += [f"- `{t}`" for t in sorted(vocab)]
    return "\n".join(lines) + "\n\n"


def render_prompt(batch: list[Path]) -> str:
    template = (ROOT / "skills" / "extract" / "PROMPT.md").read_text(encoding="utf-8")
    vocab_section = _vocabulary_section()
    if vocab_section:
        # The vocabulary is context, not batch content: it belongs before the
        # "Tonight's batch" heading that closes the template.
        marker = "## Tonight's batch"
        if marker in template:
            template = template.replace(marker, vocab_section + marker, 1)
        else:
            template += "\n" + vocab_section
    lines = [template, ""]
    lines.append(f"{len(batch)} transcript(s) to process. Read each with the Read "
                 f"tool at the absolute path given.")
    lines.append("")
    for f in batch:
        lines.append(f"- `{rel_of(f)}`")
        lines.append(f"  - absolute: `{f}`")
    lines.append("")
    lines.append("Return the JSON object now. Nothing else.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=int(CFG["extraction"]["batch_size"]))
    ap.add_argument("--out", default=None, help="write prompt here")
    ap.add_argument("--mark", nargs="*", default=None,
                    help="mark these vault-relative paths as extracted")
    ap.add_argument("--mark-batch", action="store_true",
                    help="mark the selected batch as extracted")
    ap.add_argument("--count-remaining", action="store_true")
    args = ap.parse_args()

    if args.mark is not None:
        mark_extracted(args.mark)
        return 0

    if args.count_remaining:
        remaining = len(all_transcripts()) - len(extracted_set())
        print(json.dumps({"remaining": remaining,
                          "total": len(all_transcripts()),
                          "done": len(extracted_set())}))
        return 0

    batch = select(args.limit)
    if not batch:
        if args.out:
            atomic_write(Path(args.out), "")
        print(json.dumps({"batch": [], "remaining": 0}))
        return 0

    prompt = render_prompt(batch)
    if args.out:
        atomic_write(Path(args.out), prompt)
    if args.mark_batch:
        mark_extracted([rel_of(f) for f in batch])

    remaining = len(all_transcripts()) - len(extracted_set())
    print(json.dumps({"batch": [rel_of(f) for f in batch],
                      "count": len(batch), "remaining": remaining}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
