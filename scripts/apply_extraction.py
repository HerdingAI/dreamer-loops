#!/usr/bin/env python3
"""Apply an extraction result to the vault (§6.9 Job 1).

The LLM decides WHAT is a loop and WHICH existing loop it matches. This module
decides where bytes go and enforces the state machine. Keeping that split is
what makes the simulated-week acceptance test meaningful: everything here is
deterministic and independently testable.

Input: the JSON contract in skills/extract/PROMPT.md, on stdin or via --input.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, as_date, atomic_write_json, log, p, read_json, today,
)
import vault as V  # noqa: E402

LOOP_ID_RE = re.compile(r"^L\d{4,}$")

# Auto-attach only at near-certainty. The merge-proposal threshold is 0.55; the
# gap between them is the band where the conservative bias rule still applies
# and the owner decides.
INTRA_BATCH_THRESHOLD = 0.90


def _resolve_batch_ref(ref, current_index: int,
                       batch_created: list[tuple[int, "V.Loop"]]) -> "V.Loop | None":
    """Resolve a model-supplied reference to an earlier candidate in this batch.

    Accepts an integer index or a loop id already created in this payload.
    A forward or self reference is rejected — a candidate can only match one
    that has already been decided.
    """
    if isinstance(ref, str) and LOOP_ID_RE.match(ref.strip()):
        for _, loop in batch_created:
            if loop.id == ref.strip():
                return loop
        return None
    try:
        idx = int(ref)
    except (TypeError, ValueError):
        return None
    if idx >= current_index or idx < 0:
        return None
    for cand_idx, loop in batch_created:
        if cand_idx == idx:
            return loop
    return None


def _intra_batch_twin(title: str,
                      batch_created: list[tuple[int, "V.Loop"]]) -> "V.Loop | None":
    """Highest-similarity loop created earlier in this batch, if near-identical."""
    from merge_proposals import similarity
    best, best_score = None, 0.0
    for _, loop in batch_created:
        score = similarity(title, loop.title)
        if score > best_score:
            best, best_score = loop, score
    return best if best_score >= INTRA_BATCH_THRESHOLD else None


def _wikilink(transcript: str) -> str:
    ref = transcript.strip().strip("[]").removesuffix(".md")
    return f"[[{ref}]]"


def _load_vocabulary() -> set[str] | None:
    """The frozen tag vocabulary, or None while rule 4's pre-freeze state
    holds (no file = no tags may be written, ever)."""
    try:
        import propose_tags
        return propose_tags.vocabulary()
    except Exception as exc:  # noqa: BLE001 — a broken vocabulary module must
        # degrade to "no tags", never take the night's extraction down with it.
        log(f"WARN could not load tag vocabulary ({exc}) — emitting no tags",
            job="apply")
        return None


def _filter_tags(raw, index: int, vocabulary: set[str] | None,
                 stats: dict) -> list[str]:
    """Enforce CLAUDE.md rule 4 on model-emitted tags for a NEW loop.

    Valid vocabulary tags pass through (order preserved, de-duplicated).
    Anything else is DROPPED with a logged reason — a plausible-sounding
    invented tag is exactly the case the rule exists for, and one bad tag must
    not reject an otherwise good candidate. With no frozen vocabulary, every
    tag is dropped: the pre-freeze state writes no tags at all.
    """
    try:
        from propose_tags import filter_against_vocabulary
    except Exception as exc:  # noqa: BLE001 — same degradation contract as
        # _load_vocabulary: a broken propose_tags module must cost the tags,
        # never the candidate (an unguarded import here rejected it).
        log(f"WARN could not import the tag filter ({exc}) — emitting "
            f"no tags for candidate #{index}", job="apply")
        return []
    valid, dropped = filter_against_vocabulary(
        raw, vocabulary, f"candidate #{index}", "apply")
    stats["tags_dropped"].extend(dropped)
    return valid


def apply_result(payload: dict, *, dry_run: bool = False) -> dict:
    stats = {"created": 0, "matched": 0, "intra_batch_matched": 0,
             "rejected": 0, "skipped_reported": 0,
             "decisions": [], "problems": [], "tags_dropped": []}

    # Loaded once per payload: the vocabulary gate applies uniformly to every
    # candidate in the batch. Tags attach only to NEW loops — a matched
    # candidate's tags are deliberately ignored (retro-tagging existing loops
    # is the backfill pass's job, not the nightly applier's).
    vocabulary = _load_vocabulary()

    candidates = payload.get("candidates") or []
    skipped = payload.get("skipped") or []
    stats["skipped_reported"] = len(skipped)
    # Log what the meta-idea filter threw away. Without this the filter is a
    # black box: extraction precision is measurable, but a filter that is
    # discarding real loops looks identical to a quiet week.
    for s in skipped:
        if isinstance(s, dict):
            log(f"filtered: {s.get('topic', '?')} — {s.get('reason', 'no reason given')}",
                job="apply")

    by_id = {l.id: l for l in V.load_loops(include_archived=True)}
    # Loops created during THIS payload, in candidate order. Stage A queries
    # loops/_catalog.md, which cannot contain a loop that does not exist yet, so
    # two candidates in one batch are structurally invisible to each other and
    # both come back `new`. Observed live: L0036/L0037 and L0038/L0039 —
    # byte-identical titles, same batch. Since batches are chronological, this
    # fires precisely when a topic recurs, which is the signal the whole system
    # is built on.
    batch_created: list[tuple[int, V.Loop]] = []

    for i, cand in enumerate(candidates):
        try:
            title = (cand.get("title") or "").strip()
            transcript = (cand.get("transcript") or "").strip()
            date = as_date(cand.get("date")) or today()
            match = cand.get("match") or {}
            decision = (match.get("decision") or "").strip().lower()

            if not title:
                raise ValueError("candidate has no title")
            if not transcript:
                raise ValueError("candidate has no transcript")

            # The transcript must exist. An LLM-invented path would create a
            # loop whose provenance link is broken from birth, and lint would
            # only catch it after the fact.
            if V._resolve_wikilink(transcript) is None:
                raise ValueError(f"transcript does not exist: {transcript}")

            occ = _wikilink(transcript)

            batch_ref = match.get("batch_ref")

            if decision == "matched" and batch_ref is not None:
                # The model recognised a duplicate inside this batch and pointed
                # at an earlier candidate by index, because the loop it wants
                # has no id yet.
                target = _resolve_batch_ref(batch_ref, i, batch_created)
                if target is None:
                    raise ValueError(f"batch_ref {batch_ref!r} does not resolve")
                if not dry_run:
                    V.add_occurrence(target, occ, date)
                loop_id = target.id
                stats["matched"] += 1
                stats["intra_batch_matched"] += 1
            elif decision == "matched":
                loop_id = (match.get("loop_id") or "").strip()
                if not LOOP_ID_RE.match(loop_id):
                    raise ValueError(f"matched but loop_id malformed: {loop_id!r}")
                loop = by_id.get(loop_id)
                if loop is None:
                    raise ValueError(f"matched to nonexistent loop {loop_id}")
                if not dry_run:
                    V.add_occurrence(loop, occ, date)
                stats["matched"] += 1
            elif decision == "new":
                # Deterministic safety net for what the model missed. Threshold
                # is deliberately far above the merge-proposal threshold: at
                # >=0.90 title overlap this is not the "genuine uncertainty" the
                # conservative bias rule protects, it is the same question
                # twice. Anything below still splits and goes to the owner as a
                # merge proposal, so the bias rule is complemented, not weakened.
                twin = _intra_batch_twin(title, batch_created)
                if twin is not None:
                    log(f"intra-batch duplicate: candidate #{i} {title[:52]!r} "
                        f"attached to {twin.id} instead of creating a new loop",
                        job="apply")
                    if not dry_run:
                        V.add_occurrence(twin, occ, date)
                    loop_id = twin.id
                    decision = "matched"
                    match = dict(match)
                    match["justification"] = (
                        f"Intra-batch duplicate of {twin.id} (title overlap "
                        f">={INTRA_BATCH_THRESHOLD:.0%}); attached rather than split. "
                        + (match.get("justification") or ""))
                    stats["matched"] += 1
                    stats["intra_batch_matched"] += 1
                elif not dry_run:
                    tags = _filter_tags(cand.get("tags"), i, vocabulary, stats)
                    loop = V.create_loop(title, occ, date, tags=tags)
                    by_id[loop.id] = loop
                    note = (cand.get("theme_note") or "").strip()
                    if note:
                        loop.body = V.default_body(loop) + f"\n## Theme\n\n{note}\n"
                        loop.save()
                    loop_id = loop.id
                    batch_created.append((i, loop))
                    stats["created"] += 1
                else:
                    loop_id = "(dry-run)"
                    stats["created"] += 1
            else:
                raise ValueError(f"illegal decision {decision!r}")

            stats["decisions"].append({
                "loop_id": loop_id,
                "title": title,
                "decision": decision,
                "considered": match.get("considered") or [],
                "justification": (match.get("justification") or "").strip(),
                "transcript": transcript,
                "date": date.isoformat(),
                "mark": "",   # owner writes ✓/✗ here via the digest
            })

        except Exception as exc:  # noqa: BLE001 — one bad candidate must not
            # discard the rest of the night's work.
            stats["rejected"] += 1
            stats["problems"].append(f"candidate #{i}: {exc}")
            log(f"REJECT candidate #{i}: {exc}", job="apply")

    if not dry_run and stats["decisions"]:
        _append_decisions(stats["decisions"])
    return stats


def _append_decisions(decisions: list[dict]) -> None:
    """Persist Stage-B decisions so the weekly digest can sample them (§6.6)."""
    path = p("meta") / "matching-decisions.json"
    existing = read_json(path, default=[]) or []
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    for d in decisions:
        d = dict(d)
        d["recorded_at"] = stamp
        existing.append(d)
    atomic_write_json(path, existing)


# --------------------------------------------------------------------------
# Resurfacings (§6.7) — applied through the SAME path as transcripts
# --------------------------------------------------------------------------

def apply_resurfacings() -> dict:
    """Consume inbox/resurfacings/. This is the single-write-path invariant in
    action: MCP queued the entry, the nightly job performs the mutation."""
    outdir = p("resurfacings")
    stats = {"applied": 0, "rejected": 0}
    if not outdir.exists():
        return stats
    by_id = {l.id: l for l in V.load_loops(include_archived=True)}
    for entry in sorted(outdir.glob("*.md")):
        try:
            from dreamer_common import read_page
            fm, body = read_page(entry)
            loop_id = str(fm.get("loop_id") or "")
            date = as_date(fm.get("date")) or today()
            if not LOOP_ID_RE.match(loop_id):
                raise ValueError(f"bad loop_id {loop_id!r}")
            loop = by_id.get(loop_id)
            if loop is None:
                raise ValueError(f"no such loop {loop_id}")
            was_terminal = loop.status in ("paused", "decision-only", "archived")
            # A resurfacing is a distinct occurrence, sourced from the live
            # session rather than a transcript. Keep the entry file as its
            # provenance so the occurrence link resolves.
            dest_rel = f"sources/resurfacings/{entry.stem}"
            dest = p("vault") / f"{dest_rel}.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
            V.add_occurrence(loop, f"[[{dest_rel}]]", date)
            entry.unlink()
            stats["applied"] += 1
            # A live resurfacing is the single strongest relevance signal the
            # system receives (owner decision: live recurrence defines
            # relevance), and a reopening is a state transition. Neither may
            # happen invisibly: log it and put it on the digest's event
            # channel, else a "no-op" night quietly mutates the vault.
            if was_terminal and loop.status == "open":
                log(f"resurfacing REOPENED {loop.id} ({loop.title[:50]!r})",
                    job="apply")
                _record_event("reopened",
                              f"{loop.id} reopened by a live resurfacing "
                              f"(was terminal): {loop.title[:80]}")
            else:
                log(f"resurfacing applied to {loop.id} "
                    f"(recurrence now {loop.recurrence_count})", job="apply")
                _record_event("resurfaced",
                              f"{loop.id} recurred live in an MCP session "
                              f"(recurrence now {loop.recurrence_count}): "
                              f"{loop.title[:80]}")
        except Exception as exc:  # noqa: BLE001
            stats["rejected"] += 1
            log(f"REJECT resurfacing {entry.name}: {exc}", job="apply")
    return stats


def _record_event(kind: str, detail: str) -> None:
    """Append to the run-event channel the digest reads (same schema and same
    locked read-merge-replace as bin/_common.sh record_event — an unlocked
    write here would race healthcheck's locked writer)."""
    import datetime as _dt
    import os
    path = p("meta") / "run-state.json"
    from dreamer_common import update_run_state

    def mutate(d: dict) -> None:
        d.setdefault("events", []).append({
            "at": _dt.datetime.now().isoformat(timespec="seconds"),
            "job": os.environ.get("JOB", "apply"),
            "kind": kind, "detail": detail,
        })

    update_run_state(path, mutate)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="-")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-resurfacings", action="store_true")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    payload = _extract_json(raw)
    if payload is None:
        log("FATAL: no JSON object found in extraction output", job="apply")
        return 1

    stats = apply_result(payload, dry_run=args.dry_run)
    if not args.skip_resurfacings and not args.dry_run:
        stats["resurfacings"] = apply_resurfacings()

    log(f"created={stats['created']} matched={stats['matched']} "
        f"rejected={stats['rejected']} skipped_reported={stats['skipped_reported']} "
        f"tags_dropped={len(stats['tags_dropped'])}",
        job="apply")
    print(json.dumps(stats, indent=2))
    return 0


_MAX_BRACE_REPAIRS = 50


def _repair_unclosed_objects(raw: str) -> tuple[str | None, int]:
    """Close objects the model forgot to close, and nothing else.

    Observed live (backfill batch 10, 2026-08-02): the model ended a candidate
    with the closing brace of its nested `match` object and went straight to
    `,{"title": ...`, leaving the candidate object open. One missing brace threw
    away a batch that had cost $12.55 and held ten good candidates.

    The repair is narrow on purpose. Inside an object, `,{` cannot be valid
    JSON — a member must begin with a string key — so when the decoder fails
    with "expecting property name" exactly at a `{` whose preceding non-space
    character is `,`, there is one minimal correction: close the object before
    the comma. Anything else is left to fail. This buys back malformed-but-
    unambiguous output without becoming a general "make it parse" pass, which
    is how a repair starts inventing structure the model never emitted.
    """
    fixes = 0
    for _ in range(_MAX_BRACE_REPAIRS):
        try:
            json.loads(raw)
            return raw, fixes
        except json.JSONDecodeError as e:
            if "property name" not in e.msg or e.pos >= len(raw):
                return None, fixes
            if raw[e.pos] != "{":
                return None, fixes
            j = e.pos - 1
            while j >= 0 and raw[j].isspace():
                j -= 1
            if j < 0 or raw[j] != ",":
                return None, fixes
            raw = raw[:j] + "}" + raw[j:]
            fixes += 1
    return None, fixes


def _extract_json(raw: str) -> dict | None:
    """Tolerate a model that wrapped its JSON in a fence or added a preamble,
    without silently accepting garbage."""
    raw = raw.strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidate = raw[start:end + 1]
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            repaired, fixes = _repair_unclosed_objects(candidate)
            if repaired is None:
                return None
            # Never repair silently: a batch that needed structural surgery is
            # a batch whose contents deserve a second look.
            log(f"WARN repaired {fixes} unclosed object(s) in extraction "
                f"output — model emitted invalid JSON", job="apply")
            obj = json.loads(repaired)
            return obj if isinstance(obj, dict) else None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
