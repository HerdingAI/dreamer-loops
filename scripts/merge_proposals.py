#!/usr/bin/env python3
"""Propose merges for near-duplicate loops (§6.6).

The conservative bias rule deliberately over-splits: on genuine uncertainty the
matcher creates a new loop. That is only self-healing if something later
notices the split and offers it back to the owner — otherwise "self-healing" is
a claim with no mechanism behind it.

Detection is deterministic (token overlap on titles), because a proposal only
has to be plausible enough to be worth a glance; the owner is the judge.
Confirmation is explicit: a proposal is never applied on its own.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, as_date, atomic_write_json, log, p, read_json, today)
import vault as V  # noqa: E402

STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "is", "are", "be", "should", "how", "what", "why", "when", "do",
    "does", "can", "i", "my", "it", "this", "that", "we", "owner", "they",
    "keep", "still", "actually", "really", "across", "between", "into",
}
THRESHOLD = 0.55


def tokens(title: str) -> set[str]:
    return {w for w in re.split(r"\W+", (title or "").lower())
            if len(w) > 2 and w not in STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Candidate band. Titles are DISTILLED restatements, so two pages describing
# the same underlying loop routinely share almost no vocabulary: measured
# against the owner-labelled golden set, a 0.55 Jaccard cut caught 2 of 9
# confirmed duplicates, and both were identical-title trivia. Rule 7 justifies
# the conservative split-bias by promising false splits self-heal via this
# proposer — at 22% recall that promise was false, and it degrades further as
# the corpus grows. So: cast a wide, cheap net here and let the judge decide.
# Tuned against the owner-labelled set: 0.05 recalls 9/9 confirmed duplicates
# while admitting only 3/17 distinct pairs. Asymmetric on purpose — a missed
# candidate is a permanent duplicate, an extra one costs a single judge call.
CANDIDATE_FLOOR = 0.05
# Bound the paid leg; highest-similarity candidates first. Module-level on
# purpose: tests monkeypatch this name, so a per-call CFG lookup would silently
# stop honouring the cap they set.
MAX_JUDGED = int(CFG["matching"].get("max_judged_per_run") or 40)


# --- judgment cache -------------------------------------------------------
# Without it, a pair judged `distinct` was recorded nowhere and re-judged every
# run. Scoring is deterministic, so the same top-40 was re-selected and re-paid
# ~4x a night while rank 41+ was never judged at all: the "N deferred to the
# next run" log was false — nothing was deferred, the overflow was discarded.

def _judgments_path() -> Path:
    return p("meta") / "merge-judgments.json"


def _load_judgments() -> dict:
    return read_json(_judgments_path(), default={}) or {}


def _pair_key(ida: str, idb: str) -> str:
    """Order-canonical. `keep`/`retire` order flips as recurrence counts
    change, so a key derived from it silently misses on the very next run."""
    return "|".join(sorted((ida, idb)))


def _fingerprint(a, b) -> tuple[str, str]:
    """(title_hash, evidence_hash), both taken in id-sorted order.

    The judge sees only titles, so a cached verdict holds only while those
    titles do. evidence_hash is the more important lever: rule 7 justifies the
    conservative-split bias by promising a false split self-heals as both loops
    accrue occurrences, and a permanently cached `distinct` would retire that
    promise — the pairs most deserving a second look are exactly the ones
    gaining occurrences under an unchanged title. New evidence reopens them.
    """
    first, second = sorted((a, b), key=lambda l: l.id)

    def h(parts: list[str]) -> str:
        return hashlib.sha1(
            "\x1f".join(parts).encode("utf-8")).hexdigest()[:16]

    def occ(loop) -> list[str]:
        return sorted(getattr(loop, "occurrences", None) or [])

    return (h([first.title or "", second.title or ""]),
            h(occ(first) + ["\x00"] + occ(second)))


def _cache_skips(entry: dict, fp: tuple[str, str], cooldown_weeks: int) -> bool:
    """True when a cached verdict still stands and the pair need not be judged."""
    if not entry:
        return False
    if (entry.get("title_hash"), entry.get("evidence_hash")) != fp:
        return False  # a loop changed — the verdict no longer describes it
    verdict = entry.get("verdict")
    if verdict == "distinct":
        return True
    if verdict == "expired":
        # Expiry means the owner did not review it, NOT that they rejected it
        # (G5: an unconfirmed proposal must never block the pipeline). So it
        # buys a cooldown, never a permanent suppression.
        judged = as_date(entry.get("judged_on"))
        return bool(judged and (today() - judged).days <= cooldown_weeks * 7)
    return False  # `same` is suppressed by the live proposal, not by cache


def _record(judgments: dict, key: str, verdict: str,
            fp: tuple[str, str], score: float | None) -> None:
    judgments[key] = {
        "verdict": verdict,
        "judged_on": today().isoformat(),
        "score": None if score is None else round(score, 3),
        "title_hash": fp[0],
        "evidence_hash": fp[1],
    }


def _semantic_neighbours(loops: list, limit: int = 4) -> set[tuple[str, str]]:
    """qmd vector neighbours of each loop page, as unordered id pairs.

    Lexical overlap alone cannot see that "reposting top creators" and
    "reverse-engineering their structure" are one loop. The embedding can.
    Failure here is non-fatal: an empty set just means candidates come from
    the lexical band alone.
    """
    import subprocess
    out: set[tuple[str, str]] = set()
    ids = {l.id for l in loops}
    for loop in loops:
        try:
            r = subprocess.run(
                ["qmd", "vsearch", loop.title, "-c", "vault", "--json",
                 "-n", str(limit + 1)],
                capture_output=True, text=True, timeout=30,
                cwd=str(Path(__file__).resolve().parent.parent))
            if r.returncode != 0:
                continue
            hits = json.loads(r.stdout)
        except Exception:  # noqa: BLE001 — retrieval is an optimisation here
            continue
        for h in hits if isinstance(hits, list) else []:
            m = re.search(r"(L\d{4,})",
                          str(h.get("file") or h.get("path") or ""))
            if not m or m.group(1) == loop.id or m.group(1) not in ids:
                continue
            out.add(tuple(sorted((loop.id, m.group(1)))))
    return out


def detect(judge: str = "llm") -> list[dict]:
    """Two-stage, mirroring CLAUDE.md rule 7: cheap recall, then real judgment.

    Stage A widens the net (lexical band + embedding neighbours). Stage B asks
    the same `claude -p` judge the golden set scores 96%/89% on. Passing
    judge="lexical" restores the old cheap behaviour for tests and dry runs.
    """
    loops = [l for l in V.load_loops() if l.status in ("open", "paused",
                                                       "decision-only")]
    existing = {(m["keep"], m["retire"]) for m in _load()}
    by_id = {l.id: l for l in loops}

    candidates: dict[tuple[str, str], float] = {}
    for a, b in combinations(loops, 2):
        score = similarity(a.title, b.title)
        if score >= CANDIDATE_FLOOR:
            candidates[tuple(sorted((a.id, b.id)))] = score
    # Embedding neighbours outrank the lexical band. Ranking candidates by
    # Jaccard and cutting at MAX_JUDGED would drop exactly the pairs this
    # rewrite exists for: semantically identical, lexically distant. A +1.0
    # offset guarantees every semantic neighbour is judged before any
    # lexical-only pair.
    for pair in _semantic_neighbours(loops):
        candidates[pair] = candidates.get(pair, 0.0) + 1.0

    # Filter BEFORE the cap, not inside the judging loop. Filtering after it
    # let already-proposed and already-judged pairs consume judge slots, so the
    # queue never drained however long it ran.
    judgments = _load_judgments()
    cooldown = int(CFG["matching"]["merge_proposal_expiry_weeks"])
    n_proposed = n_cached = 0
    queue: list[tuple[tuple[str, str], float]] = []
    for (ida, idb), score in sorted(candidates.items(), key=lambda kv: -kv[1]):
        if (ida, idb) in existing or (idb, ida) in existing:
            n_proposed += 1
            continue
        if _cache_skips(judgments.get(_pair_key(ida, idb), {}),
                        _fingerprint(by_id[ida], by_id[idb]), cooldown):
            n_cached += 1
            continue
        queue.append(((ida, idb), score))

    # Never truncate silently: a bounded run that reports full coverage reads
    # as "no duplicates exist" when it means "we stopped looking".
    log(f"merge candidates: judging {min(len(queue), MAX_JUDGED)} unjudged "
        f"pair(s); {n_cached} previously judged, {n_proposed} already "
        f"proposed, {max(0, len(queue) - MAX_JUDGED)} remain queued",
        job="merge")
    ordered = queue[:MAX_JUDGED]

    proposals: list[dict] = []
    judge_errors = 0
    judged_any = False
    for (ida, idb), score in ordered:
        a, b = by_id[ida], by_id[idb]
        fp = _fingerprint(a, b)
        key = _pair_key(ida, idb)
        keep, retire = ((a, b)
                        if (a.recurrence_count, b.id) >= (b.recurrence_count, a.id)
                        else (b, a))

        if judge == "lexical":
            if score < THRESHOLD:
                continue
            reason = f"token overlap {score:.0%}"
        else:
            from golden_set import judge_llm
            v = judge_llm(a.title, b.title)
            verdict = v.get("verdict")
            if verdict == "error":
                # A judge outage must not read as "distinct". Rule 7 justifies
                # the conservative-split bias on the promise that this run
                # proposes the merge back; silently dropping unjudgeable pairs
                # retires that promise while still reporting a confident zero.
                # Degrade to the deterministic v1 rule instead of to nothing.
                # Deliberately NOT cached: an outage verdict is a transient
                # claude failure, not a judgment. Caching it would freeze a
                # whole run's worth of pairs on the strength of an exception.
                judge_errors += 1
                if score < THRESHOLD:
                    continue
                reason = (f"judge unavailable ({v.get('reason')}) — "
                          f"proposed on token overlap {score:.0%} alone")
            elif verdict != "same":
                _record(judgments, key, "distinct", fp, score)
                judged_any = True
                continue
            else:
                _record(judgments, key, "same", fp, score)
                judged_any = True
                reason = str(v.get("reason") or "judged the same underlying loop")

        shared = sorted(tokens(a.title) & tokens(b.title))
        proposals.append({
            "keep": keep.id, "keep_title": keep.title,
            "retire": retire.id, "retire_title": retire.title,
            "similarity": round(score, 2),
            "judge": judge,
            "reason": f"{reason} (token overlap {score:.0%}"
                      + (f"; shared: {', '.join(shared)}" if shared else "")
                      + ") — conservative bias split these; confirm to merge.",
            "proposed_on": today().isoformat(),
            "expires_weeks": int(CFG["matching"]["merge_proposal_expiry_weeks"]),
        })
    if judge_errors:
        log(f"WARN judge failed on {judge_errors} of {len(ordered)} candidate "
            f"pair(s) — those fell back to the token-overlap rule; a run with "
            f"many of these has NOT really judged its candidates",
            job="merge")
    if judged_any:
        atomic_write_json(_judgments_path(), judgments)
    detect.last_judge_errors = judge_errors
    return proposals


def _path() -> Path:
    return p("meta") / "merge-proposals.json"


def _load() -> list[dict]:
    return read_json(_path(), default=[]) or []


def refresh(judge: str = "llm") -> dict:
    """Expire stale proposals, add new ones. G5: an unconfirmed proposal must
    never block the pipeline — it expires and is re-proposed once."""
    kept, expired = [], 0
    weeks = int(CFG["matching"]["merge_proposal_expiry_weeks"])
    lapsed: list[dict] = []
    for m in _load():
        proposed = as_date(m.get("proposed_on"))
        age_weeks = ((today() - proposed).days / 7) if proposed else 0
        if age_weeks > weeks:
            if m.get("reproposed"):
                expired += 1
                lapsed.append(m)
                continue
            m["reproposed"] = True
            m["proposed_on"] = today().isoformat()
        kept.append(m)

    # A proposal that lapsed twice is recorded as `expired`, never `rejected`:
    # expiry means the owner did not look, not that they decided. It buys a
    # cooldown so detect() stops re-paying for it every run, and the digest
    # says so out loud — silent suppression is how a false split becomes
    # permanent, which is exactly what rule 7 promises cannot happen.
    if lapsed:
        by_id = {l.id: l for l in V.load_loops()}
        judgments = _load_judgments()
        for m in lapsed:
            a, b = by_id.get(m["keep"]), by_id.get(m["retire"])
            if not (a and b):
                continue  # a loop was merged or archived; nothing to cool down
            _record(judgments, _pair_key(a.id, b.id), "expired",
                    _fingerprint(a, b), None)
            try:
                import digest as G
                G.stage("merge-expired", {
                    "keep": m["keep"], "retire": m["retire"],
                    "detail": (f"merge proposal {m['keep']}/{m['retire']} "
                               f"expired unreviewed after {weeks * 2} weeks; "
                               f"it will not be re-proposed for {weeks} weeks "
                               f"unless either loop gains a new occurrence")})
            except Exception as exc:  # digest staging must never fail refresh
                log(f"WARN could not stage expiry event: {exc}", job="merge")
        atomic_write_json(_judgments_path(), judgments)

    fresh = detect(judge=judge)
    combined = kept + fresh
    atomic_write_json(_path(), combined)
    return {"active": len(combined), "new": len(fresh), "expired": expired,
            "judge_errors": getattr(detect, "last_judge_errors", 0)}


def confirm(keep_id: str, retire_id: str) -> dict:
    loops = {l.id: l for l in V.load_loops()}
    if keep_id not in loops or retire_id not in loops:
        raise SystemExit(f"unknown loop(s): {keep_id}, {retire_id}")
    merged = V.merge_loops(loops[keep_id], loops[retire_id])
    remaining = [m for m in _load()
                 if {m["keep"], m["retire"]} != {keep_id, retire_id}]
    atomic_write_json(_path(), remaining)
    # The retired id can never appear in a candidate pair again, so every
    # cached verdict naming it is dead weight.
    judgments = _load_judgments()
    live = {k: v for k, v in judgments.items()
            if retire_id not in k.split("|")}
    if len(live) != len(judgments):
        atomic_write_json(_judgments_path(), live)
    V.regenerate_catalog()
    return {"merged_into": merged.id, "recurrence_count": merged.recurrence_count,
            "first_seen": str(merged.first_seen)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["refresh", "list", "confirm"])
    ap.add_argument("--keep")
    ap.add_argument("--retire")
    ap.add_argument("--judge", choices=["llm", "lexical"],
                    default="llm",
                    help="lexical is the cheap dry-run path")
    args = ap.parse_args()
    if args.command == "refresh":
        print(json.dumps(refresh(judge=args.judge), indent=2))
    elif args.command == "list":
        print(json.dumps(_load(), indent=2))
    else:
        if not args.keep or not args.retire:
            raise SystemExit("confirm requires --keep and --retire")
        print(json.dumps(confirm(args.keep, args.retire), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
