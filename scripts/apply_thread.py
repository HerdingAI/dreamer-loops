#!/usr/bin/env python3
"""Apply one thread-fold result to a loop page (living thread, rule 15).

The LLM decides what the thread NOW says; this module decides whether that
output is allowed to touch the page. The validation contract — every check
fails loudly and leaves the page unmodified:

- only the Thread section body changes; frontmatter stays byte-identical and
  every other section is untouched (asserted, not assumed);
- exactly one trajectory line is appended per fold, dated to the occurrence
  and citing it; existing trajectory lines are carried forward verbatim
  (append-only);
- every wikilink cited in `now` or the trajectory line must already be in the
  loop's occurrence list — a fold cannot mint provenance;
- `now` claims carry the ` via thread` derived marker (rule 13/15); the
  applier adds it when the model forgot;
- refolding an occurrence the trajectory already cites is a zero-diff no-op;
- the fold-pending queue entry is removed ONLY after a successful write.

Fold output is flattened to single-line text before it goes anywhere near the
page, so a heading or frontmatter fence inside it can never become structure.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import _FM_RE, as_date, atomic_write, log  # noqa: E402
import fold_pending as FP  # noqa: E402
import vault as V  # noqa: E402

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_TRAJ_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) — (.+?) — (\[\[[^\]]+\]\])$")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class FoldError(ValueError):
    """A fold that violated the contract. The page was not modified."""


def _flatten(text: str) -> str:
    """Collapse to one line. This is the structural safety valve: markdown
    headings and YAML fences only bite at line starts, so fold output that is
    never allowed a newline can never escape its section."""
    return " ".join((text or "").split())


def _mark_via_thread(now: str) -> str:
    """Ensure EVERY citation carries the derived marker (rule 15).

    Exhaustive by construction: the marker, not the wrapper, carries the tier
    (rule 13), so a wikilink the shape-specific passes miss — "([[x]] — note)",
    "([[a]] [[b]])" — still gets marked in place by the final pass. A link
    the model already marked never matches twice.
    """
    # ([[x]]) -> ([[x]] via thread); already-marked ones don't match.
    now = re.sub(r"\(\s*(\[\[[^\]]+\]\])\s*\)", r"(\1 via thread)", now)
    # bare [[x]] with no marker -> ([[x]] via thread)
    now = re.sub(r"(?<!\()(\[\[[^\]]+\]\])(?!\s+via thread\b)",
                 r"(\1 via thread)", now)
    # anything still unmarked (e.g. right after an opening paren with more
    # content in it) -> marked in place, no extra wrapping
    now = re.sub(r"(\[\[[^\]]+\]\])(?!\s+via thread\b)",
                 r"\1 via thread", now)
    return now


def _existing_trajectory(section: str | None) -> list[str]:
    if not section:
        return []
    return [ln.strip() for ln in section.splitlines()
            if _TRAJ_LINE.match(ln.strip())]


def _outside_thread(text: str) -> str:
    return V._THREAD_SECTION.sub("", text).rstrip()


def apply_fold(loop_id: str, occurrence: str, date: _dt.date,
               payload: dict) -> dict:
    loop = V.load_loop(loop_id)
    if loop is None:
        raise FoldError(f"no such loop: {loop_id}")
    if occurrence not in loop.occurrences:
        raise FoldError(f"{loop_id}: {occurrence!r} is not in the loop's "
                        f"occurrence list — a fold cannot mint provenance")
    m = _DATE.search(occurrence)
    if m and m.group(1) != date.isoformat():
        raise FoldError(f"{loop_id}: occurrence is dated {m.group(1)} but the "
                        f"fold claims {date.isoformat()}")

    raw = loop.path.read_text(encoding="utf-8")
    fmm = _FM_RE.match(raw)
    if not fmm:
        raise FoldError(f"{loop_id}: page has no frontmatter — refusing")
    fm_raw, body = raw[:fmm.start(2)], raw[fmm.start(2):]

    existing = V.thread_section(body)
    trajectory = _existing_trajectory(existing)
    occ_norm = V._norm(occurrence)

    # Idempotency: an occurrence the trajectory already cites has been folded.
    # The queue entry (if any) is stale work — clear it, touch nothing.
    for line in trajectory:
        tm = _TRAJ_LINE.match(line)
        if tm and V._norm(tm.group(3)) == occ_norm:
            FP.remove(loop_id, occurrence)
            return {"loop": loop_id, "occurrence": occurrence, "noop": True}

    if not isinstance(payload, dict):
        raise FoldError("fold payload is not a JSON object")
    now = _flatten(str(payload.get("now") or ""))
    traj_text = _flatten(str(payload.get("trajectory_line") or ""))
    if not now:
        raise FoldError("fold payload has no 'now' text")
    if not traj_text:
        raise FoldError("fold payload has no 'trajectory_line'")

    # The model may have emitted the full line; unwrap and validate its parts
    # rather than trusting them. Date and citation are the applier's to write.
    traj_text = re.sub(r"^-\s+", "", traj_text)
    dm = re.match(r"^(\d{4}-\d{2}-\d{2}) — (.+)$", traj_text)
    if dm:
        if dm.group(1) != date.isoformat():
            raise FoldError(f"trajectory line dated {dm.group(1)} but the "
                            f"occurrence is dated {date.isoformat()}")
        traj_text = dm.group(2)
    lm = re.match(r"^(.*?) — (\[\[[^\]]+\]\])$", traj_text)
    if lm:
        if V._norm(lm.group(2)) != occ_norm:
            raise FoldError(f"trajectory line cites {lm.group(2)} but this "
                            f"fold is for {occurrence}")
        traj_text = lm.group(1)
    if not traj_text.strip():
        raise FoldError("trajectory line is empty once unwrapped")

    # Citation containment: every wikilink in the fold output must already be
    # an occurrence of THIS loop.
    occ_norms = {V._norm(o) for o in loop.occurrences}
    for ref in _WIKILINK.findall(now) + _WIKILINK.findall(traj_text):
        if V._norm(ref) not in occ_norms:
            raise FoldError(f"fold cites [[{ref}]] which is not in "
                            f"{loop_id}'s occurrence list")

    now = _mark_via_thread(now)
    new_line = f"- {date.isoformat()} — {traj_text.strip()} — {occurrence}"
    if not _TRAJ_LINE.match(new_line):
        raise FoldError(f"constructed trajectory line is malformed: "
                        f"{new_line!r}")
    trajectory.append(new_line)

    content = ("**Now**\n\n" + now + "\n\n**Trajectory**\n\n"
               + "\n".join(trajectory))
    new_body = V.replace_thread_section(body, content)
    if _outside_thread(new_body) != _outside_thread(body):
        raise FoldError("fold would modify content outside the Thread "
                        "section — refused")

    # Frontmatter byte-identical by construction: the original frontmatter
    # bytes are reattached untouched. Atomic via dreamer_common (rule 8).
    atomic_write(loop.path, fm_raw + new_body)
    if not loop.path.read_text(encoding="utf-8").startswith(fm_raw):
        raise FoldError(f"{loop_id}: frontmatter changed across the write — "
                        f"investigate before folding again")

    # Only now is the queue entry done.
    FP.remove(loop_id, occurrence)
    return {"loop": loop_id, "occurrence": occurrence, "noop": False,
            "trajectory_lines": len(trajectory)}


def replace_now(loop_id: str, now: str) -> dict:
    """Rebuild ONLY the thread's **Now** paragraph; the trajectory is
    append-only and stays verbatim (rule 15 drift correction).

    Used by apply_conclusion when a dream re-derived the loop from primary
    occurrences (rule 13) and returned a refreshed `now`. Same validation
    contract as apply_fold: citations must already be occurrences of THIS
    loop, output is flattened so it can never become structure, nothing
    outside the Thread section may change, and every violation raises
    FoldError with the page untouched.
    """
    loop = V.load_loop(loop_id)
    if loop is None:
        raise FoldError(f"no such loop: {loop_id}")
    raw = loop.path.read_text(encoding="utf-8")
    fmm = _FM_RE.match(raw)
    if not fmm:
        raise FoldError(f"{loop_id}: page has no frontmatter — refusing")
    fm_raw, body = raw[:fmm.start(2)], raw[fmm.start(2):]

    existing = V.thread_section(body)
    if existing is None:
        raise FoldError(f"{loop_id}: no Thread section — nothing to rebuild")
    now = _flatten(now)
    if not now:
        raise FoldError("replacement 'now' is empty")
    occ_norms = {V._norm(o) for o in loop.occurrences}
    for ref in _WIKILINK.findall(now):
        if V._norm(ref) not in occ_norms:
            raise FoldError(f"now cites [[{ref}]] which is not in "
                            f"{loop_id}'s occurrence list")
    now = _mark_via_thread(now)
    trajectory = _existing_trajectory(existing)
    content = ("**Now**\n\n" + now + "\n\n**Trajectory**\n\n"
               + "\n".join(trajectory))
    new_body = V.replace_thread_section(body, content)
    if _outside_thread(new_body) != _outside_thread(body):
        raise FoldError("now rebuild would modify content outside the Thread "
                        "section — refused")
    atomic_write(loop.path, fm_raw + new_body)
    return {"loop": loop_id, "now_replaced": True,
            "trajectory_lines": len(trajectory)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", required=True)
    ap.add_argument("--occurrence", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--input", default="-")
    args = ap.parse_args()
    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    from apply_extraction import _extract_json
    payload = _extract_json(raw)
    if payload is None:
        log(f"FATAL: no JSON in fold output for {args.loop}", job="fold")
        return 1
    date = as_date(args.date)
    if date is None:
        log(f"FATAL: bad --date {args.date!r}", job="fold")
        return 1
    try:
        out = apply_fold(args.loop, args.occurrence, date, payload)
    except FoldError as exc:
        log(f"REFUSED fold for {args.loop}: {exc}", job="fold")
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
