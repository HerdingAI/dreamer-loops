#!/usr/bin/env python3
"""Apply tag-backfill output to untagged loops (U7).

The deterministic half of the one-time tag backfill (bin/tag-backfill.sh).
The LLM chooses which vocabulary tags fit each loop; this module decides
where bytes go and enforces CLAUDE.md rule 4.

Differences from the nightly applier (apply_extraction.py) are deliberate:

- A missing vocabulary is a HARD error here, not a degrade-to-no-tags. The
  nightly path must survive the pre-freeze state; a backfill run before the
  freeze is meaningless and must refuse to touch anything.
- Only untagged loops are ever written. An existing `tags:` list — however it
  got there — is never overwritten: an already-tagged loop shows a zero diff.
- The write is frontmatter-only. The body is loaded and saved untouched
  through the vault.py path (temp file + os.replace, rule 8).

Input: {"loops": [{"id": "L0042", "tags": [...]}]} on stdin or via --input,
per skills/tag-backfill/PROMPT.md. Malformed JSON fails loudly before any
page is modified.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import log  # noqa: E402
import propose_tags  # noqa: E402
import vault as V  # noqa: E402

LOOP_ID_RE = re.compile(r"^L\d{4,}$")


def parse_payload(raw: str) -> dict:
    """The {"loops": [...]} object out of whatever the model returned.

    Tolerates a markdown fence or a short preamble (the house pattern —
    observed live on the first cc-ingest session), but nothing structural:
    anything that does not parse to the contract raises ValueError, and the
    caller must not have touched a page yet.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty tag-backfill output")
    from apply_extraction import _extract_json
    obj = _extract_json(raw)
    if obj is None:
        raise ValueError("no JSON object in tag-backfill output")
    if not isinstance(obj.get("loops"), list):
        raise ValueError('tag-backfill output must be {"loops": [...]}')
    return obj


def _load_vocabulary() -> set[str]:
    """The frozen vocabulary, or a hard stop.

    Unlike apply_extraction._load_vocabulary, there is no degrade path: a
    backfill exists only to apply the frozen vocabulary, so running without
    one is a configuration error, never a quiet no-tags night.
    """
    vocab = propose_tags.vocabulary()
    if not vocab:
        raise SystemExit(
            "FATAL: no frozen tag vocabulary at vault/.vault-meta/"
            "tag-vocabulary.json — a tag backfill without a vocabulary is "
            "meaningless (CLAUDE.md rule 4). Freeze one first: "
            "python3 scripts/propose_tags.py freeze --tags ...")
    return vocab


def apply_tags(payload: dict, *, vocabulary: set[str] | None = None) -> dict:
    stats = {"tagged": 0, "skipped_already_tagged": 0,
             "skipped_unknown_id": 0, "tags_dropped": [], "empty": 0}
    # Vocabulary is resolved BEFORE any page is touched, so the hard error
    # above cannot leave a half-applied batch behind.
    if vocabulary is None:
        vocabulary = _load_vocabulary()

    # Active loops only: the selection in bin/tag-backfill.sh never offers an
    # archived page, so an archived (or otherwise unknown) id in the reply is
    # the model inventing — skip it, never resolve it creatively.
    by_id = {l.id: l for l in V.load_loops()}

    for i, entry in enumerate(payload.get("loops") or []):
        if not isinstance(entry, dict):
            stats["skipped_unknown_id"] += 1
            log(f"tag-backfill: entry #{i} is not an object — skipped",
                job="tags")
            continue
        loop_id = str(entry.get("id") or "").strip()
        loop = by_id.get(loop_id)
        if loop is None:
            stats["skipped_unknown_id"] += 1
            log(f"tag-backfill: entry #{i} id {loop_id!r} does not resolve "
                f"to an active loop — skipped", job="tags")
            continue
        if loop.tags:
            # Never overwrite. Whatever tagged this loop (extraction, a merge,
            # an earlier backfill run) already satisfied rule 4; re-tagging
            # would silently discard that judgment.
            stats["skipped_already_tagged"] += 1
            log(f"tag-backfill: {loop_id} already tagged {loop.tags} — "
                f"zero diff", job="tags")
            continue

        valid, dropped = propose_tags.filter_against_vocabulary(
            entry.get("tags"), vocabulary, loop_id, "tags")
        stats["tags_dropped"].extend(dropped)
        if not valid:
            # Zero tags is a valid answer — the loop simply stays untagged,
            # with a zero diff on its page.
            stats["empty"] += 1
            continue

        loop.tags = valid
        # vault.py write path: temp file + os.replace (rule 8). The body was
        # loaded from disk and is written back untouched — frontmatter-only.
        loop.save()
        stats["tagged"] += 1

    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="-")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    try:
        payload = parse_payload(raw)
    except ValueError as exc:
        log(f"FATAL: {exc}", job="tags")
        return 1

    stats = apply_tags(payload)
    log(f"tagged={stats['tagged']} "
        f"skipped_already_tagged={stats['skipped_already_tagged']} "
        f"skipped_unknown_id={stats['skipped_unknown_id']} "
        f"tags_dropped={len(stats['tags_dropped'])} empty={stats['empty']}",
        job="tags")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
