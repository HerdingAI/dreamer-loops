#!/usr/bin/env python3
"""Knowledge-frontier map builder.

Builds the map of what the owner knows from the owner's own words, one batch
at a time, and seeds it with Dreamer's open loops (the known unknowns).

One batch (`run_batch`):
  1. select   unmapped or grown transcripts up to frontier.map_batch_chars of owner
              text, read from the map ledger (vault/.vault-meta/frontier-map-ledger.json)
  2. extract  a sealed model call returns the learnable concepts, each with a quote
  3. verify   every quote must be found verbatim in a Human turn (owner_text); a
              quote in an assistant turn or in pasted material is logged and dropped
  4. retrieve neighbours from the `frontier` qmd collection; a degraded lookup
              defers the batch (it never means "no neighbours")
  5. match    a sealed judge answers attach / new / batch_ref, new on doubt (rule 7)
  6. apply    under wiki.lock only: create or attach items, add evidence and goal
              relations, move the quadrant with cause `chat <transcript>`, and mark
              each transcript in the ledger once its concepts are applied

Model calls and retrieval run WITHOUT the vault lock. Apply is
idempotent per transcript: evidence is keyed by transcript + normalised quote,
so a batch that crashed half way and is resumed (even with different model
answers) attaches to what it already wrote instead of duplicating it.

The caller (bin/frontier-backfill.sh) holds frontier.lock for the whole run.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, append_event_deduped, as_date, atomic_write_json, log, p, read_json,
    read_page, state_lock,
)
import frontier as F  # noqa: E402
import frontier_llm as L  # noqa: E402
import goals as G  # noqa: E402
import make_batch as MB  # noqa: E402
import owner_text as OT  # noqa: E402
import questions as Q  # noqa: E402
import vault as V  # noqa: E402

JOB = "frontier-map"
COLLECTION = "frontier"
_ITEM_ID = re.compile(r"K\d{4,}")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _cfg(key: str):
    return CFG["frontier"][key]


def _now(at: str | None = None) -> str:
    return at or _dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Ledger: {"version": 1, "done": {rel: {...}}, "deferred": {rel: {...}}, "skipped": {rel: {...}}}
#   done      message_count + turns at the moment the transcript was mapped; a chat
#             that grows past it is mapped again from turn `turns` onward
#   deferred  reason "parse" and an attempt count
#   skipped   given up after frontier.max_parse_attempts
# --------------------------------------------------------------------------

def ledger_path() -> Path:
    return p("meta") / _cfg("map_ledger_file")


def load_ledger() -> dict:
    data = read_json(ledger_path(), default=None) or {}
    return {"version": 1, "done": dict(data.get("done") or {}),
            "deferred": dict(data.get("deferred") or {}),
            "skipped": dict(data.get("skipped") or {})}


def save_ledger(ledger: dict) -> None:
    atomic_write_json(ledger_path(), ledger)


@dataclass
class Candidate:
    rel: str
    path: Path
    date: str
    from_turn: int
    n_turns: int
    message_count: int
    text: str
    attempts: int = 0


@dataclass
class Selection:
    batch: list[Candidate]
    empties: list[Candidate]   # nothing of the owner's to map: marked without a model call


def mark_done(cand: Candidate, concepts: int, at: str | None = None,
              decisions: list[dict] | None = None) -> None:
    """`decisions`: each concept's match decision and its one-line justification,
    kept so they can be sampled (constitution rule 7)."""
    ledger = load_ledger()
    ledger["done"][cand.rel] = {"message_count": cand.message_count, "turns": cand.n_turns,
                                "concepts": concepts, "at": _now(at),
                                "decisions": decisions or []}
    ledger["deferred"].pop(cand.rel, None)
    save_ledger(ledger)


def mark_parse_failure(cands: list[Candidate], at: str | None = None) -> list[str]:
    """Count one failed extraction/match per chat; a chat at frontier.max_parse_attempts
    moves to `skipped`. Returns the skipped ones."""
    ledger = load_ledger()
    limit = int(_cfg("max_parse_attempts"))
    skipped = []
    for c in cands:
        attempts = int((ledger["deferred"].get(c.rel) or {}).get("attempts", 0)) + 1
        if attempts >= limit:
            ledger["deferred"].pop(c.rel, None)
            ledger["skipped"][c.rel] = {"reason": "parse", "attempts": attempts, "at": _now(at)}
            skipped.append(c.rel)
        else:
            ledger["deferred"][c.rel] = {"reason": "parse", "attempts": attempts, "at": _now(at)}
    save_ledger(ledger)
    return skipped


def _event(kind: str, detail: str) -> None:
    """A run-state event, deduped. Never fatal: a busy vault only costs the event."""
    path = p("meta") / "run-state.json"
    event = {"at": _now(), "job": JOB, "kind": kind, "detail": detail}
    try:
        with state_lock(path.parent / "wiki.lock", timeout_s=float(_cfg("vault_lock_wait_seconds"))):
            state = read_json(path, default={}) or {}
            append_event_deduped(state, event)
            atomic_write_json(path, state)
    except (TimeoutError, OSError, ValueError) as exc:
        log(f"WARN could not record event ({detail}): {exc}", job=JOB)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half].rstrip()}\n{OT.CUT_MARKER}\n{text[-half:].lstrip()}"


def select(limit_chars: int) -> Selection:
    """Chronological, oldest first. A chat that grew since it was mapped comes
    back from its first unmapped turn. A chat that failed to parse is tried
    alone, so one bad chat cannot keep failing a whole batch."""
    ledger = load_ledger()
    batch: list[Candidate] = []
    empties: list[Candidate] = []
    used = 0
    for f in MB.all_transcripts():
        rel = MB.rel_of(f)
        if rel in ledger["skipped"]:
            continue
        fm, _ = read_page(f)
        done = ledger["done"].get(rel)
        count = fm.get("message_count") if isinstance(fm.get("message_count"), int) else None
        if done and count is not None and int(done.get("message_count", 0)) >= count:
            continue
        turns = OT.turns(f)
        n_turns = len(turns)
        if done and count is None and int(done.get("turns", 0)) >= n_turns:
            continue
        from_turn = int(done.get("turns", 0)) if done else 0
        cand = Candidate(
            rel=rel, path=f, date=(as_date(fm.get("date")) or _dt.date.min).isoformat(),
            from_turn=from_turn, n_turns=n_turns,
            message_count=count if count is not None else n_turns,
            text=OT.owner_text(f, include_reconstructed=True, skip_turns=from_turn),
            attempts=int((ledger["deferred"].get(rel) or {}).get("attempts", 0)))
        if not cand.text.strip():
            empties.append(cand)
            continue
        if cand.attempts > 0:
            if not batch:
                cand.text = _cut(cand.text, limit_chars)
                batch.append(cand)
            break  # the next call tries it alone (or after the batch in hand)
        if batch and used + len(cand.text) > limit_chars:
            break
        cand.text = _cut(cand.text, limit_chars)
        batch.append(cand)
        used += len(cand.text)
        if used >= limit_chars:
            break
    return Selection(batch, empties)


# --------------------------------------------------------------------------
# Prompt bodies
# --------------------------------------------------------------------------

def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("|", "/")).strip()


def _extract_body(cands: list[Candidate], goals: list) -> str:
    lines = ["## Active goals", ""]
    lines += [f"- {g.id}: {_one_line(g.title)} (finish line: {_one_line(g.finish_line)})"
              for g in goals] or ["(none)"]
    lines += ["", "## Chats", ""]
    for c in cands:
        lines.append(f"=== TRANSCRIPT {c.rel} ===")
        lines.append(f"date: {c.date}")
        if c.from_turn:
            lines.append("(this chat grew: only its new turns are shown)")
        lines += ["", c.text, "=== END TRANSCRIPT ===", ""]
    return "\n".join(lines)


def _match_body(concepts: list[dict], start: int, stop: int, neighbours: dict[int, list[dict]]) -> str:
    lines = ["## Earlier concepts in this batch", ""]
    earlier = [f"- #{c['idx']} | {_one_line(c['title'])} | {_one_line(c['summary'])}"
               for c in concepts[:start]]
    lines += earlier or ["(none)"]
    lines += ["", "## Concepts to decide", ""]
    for c in concepts[start:stop]:
        lines += [f"### Concept {c['idx']}", f"- title: {_one_line(c['title'])}",
                  f"- summary: {_one_line(c['summary'])}"]
        hits = neighbours.get(c["idx"], [])
        lines += [f"  - {n['id']} | {_one_line(n['title'])} | {_one_line(n['summary'])}" for n in hits]
        if not hits:
            lines.append("  (no neighbours)")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# One batch
# --------------------------------------------------------------------------

def _result(status: str, **extra) -> dict:
    return {"status": status, "mapped": 0, "items_created": 0, "items_attached": 0,
            "dropped_quotes": 0, "deferred": 0, **extra}


def _verify(cands: list[Candidate], raw: list[dict]) -> tuple[list[dict], int]:
    """Keep concepts whose quote is verbatim in a Human turn. A quote found
    only in a reconstructed turn is recorded as `reconstructed` evidence, which
    never places a known known. At most max_items_per_transcript per chat."""
    by_rel = {c.rel: c for c in cands}
    order = {c.rel: i for i, c in enumerate(cands)}
    cap = int(_cfg("max_items_per_transcript"))
    kept: dict[str, list[dict]] = {}
    dropped = 0
    for c in sorted(raw, key=lambda c: order[c["transcript"]]):
        cand = by_rel[c["transcript"]]
        hit = OT.find_owner_quote(cand.path, c["quote"])
        if hit is None:
            dropped += 1
            log(f"dropped {c['title']!r} from {cand.rel}: quote is not in a Human turn "
                f"({c['quote'][:60]!r})", job=JOB)
            continue
        bucket = kept.setdefault(cand.rel, [])
        if len(bucket) >= cap:
            log(f"dropped {c['title']!r} from {cand.rel}: over max_items_per_transcript", job=JOB)
            continue
        bucket.append({**c, "date": cand.date,
                       "kind": "reconstructed" if hit["reconstructed"] else c["kind"]})
    out = [c for cand in cands for c in kept.get(cand.rel, [])]
    for i, c in enumerate(out):
        c["idx"] = i
    return out, dropped


def _item_pages() -> dict[str, Path]:
    d = F.items_dir()
    return {path.stem: path for path in sorted(d.glob("K*.md"))} if d.exists() else {}


def _neighbours(concepts: list[dict], qmd_factory) -> dict[int, list[dict]] | None:
    """Neighbour items per concept, best first; None when retrieval is degraded
    (the caller defers: a degraded lookup is never "no neighbours")."""
    pages = _item_pages()
    if not pages:
        return {c["idx"]: [] for c in concepts}  # nothing to match against
    out: dict[int, list[dict]] = {}
    top_k = int(_cfg("match_top_k"))
    with qmd_factory() as q:
        for c in concepts:
            hits, degraded = q.query(f"{c['title']}\n{c['summary']}", COLLECTION, limit=top_k)
            if degraded:
                return None
            seen: list[dict] = []
            for h in hits:
                m = _ITEM_ID.search(Path(h["file"]).stem)
                if not m or m.group(0) in {s["id"] for s in seen} or m.group(0) not in pages:
                    continue
                item = F.Item.from_path(pages[m.group(0)])
                seen.append({"id": item.id, "title": item.title, "summary": item.summary})
            out[c["idx"]] = seen
    return out


def _decide(concepts: list[dict], neighbours: dict[int, list[dict]]) -> dict[int, dict]:
    """Match decisions for every concept, in chunks of frontier.match_chunk_concepts."""
    if len(concepts) <= 1 and not any(neighbours.values()):
        return {c["idx"]: {"decision": "new", "item": None, "batch_ref": None,
                           "justification": "no neighbours"} for c in concepts}
    chunk = max(1, int(_cfg("match_chunk_concepts")))
    cap = int(_cfg("prompt_max_chars"))
    decisions: dict[int, dict] = {}
    for start in range(0, len(concepts), chunk):
        stop = min(start + chunk, len(concepts))
        ids = {c["idx"]: {n["id"] for n in neighbours.get(c["idx"], [])}
               for c in concepts[start:stop]}
        # Lowest-ranked neighbours go first when the prompt is over the cap.
        ranked = sorted(((pos, c["idx"], n) for c in concepts[start:stop]
                         for pos, n in enumerate(neighbours.get(c["idx"], []))),
                        key=lambda t: (t[0], t[1]))

        def body_for(kept, start=start, stop=stop):
            per: dict[int, list[dict]] = {}
            for _, idx, n in kept:
                per.setdefault(idx, []).append(n)
            return _match_body(concepts, start, stop, per)

        kept = L.trim_to_cap(lambda k: L.render_prompt("match", body_for(k)), ranked, cap)
        kept_ids = {(idx, n["id"]) for _, idx, n in kept}
        ids = {idx: {i for i in s if (idx, i) in kept_ids} for idx, s in ids.items()}
        decisions.update(L.call_json("match", body_for(kept),
                                     lambda t, ids=ids: L.parse_match(t, ids)))
    return decisions


def _evidence_index() -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for item_id, path in _item_pages().items():
        for e in F.Item.from_path(path).evidence:
            index.setdefault((e["transcript"], Q.normalize_quote(e["quote"])), item_id)
    return index


def _merge_goals(item: "F.Item", goals: list[dict]) -> None:
    for g in goals:
        have = next((x for x in item.goals if x["goal"] == g["goal"]), None)
        if have and have["relation"] == "connected":
            continue  # never downgrade a connection the owner made
        item.set_goal(g["goal"], g["relation"])


def _apply(cands: list[Candidate], concepts: list[dict], decisions: dict[int, dict],
           at: str | None, res: dict) -> None:
    """Create or attach items and mark each transcript once its concepts are in.
    The caller holds wiki.lock."""
    active = F.active_goal_ids()
    index = _evidence_index()
    pages = _item_pages()
    resolved: dict[int, str] = {}
    for cand in cands:
        cause = f"chat {cand.rel}"
        kept: list[dict] = []
        for c in (c for c in concepts if c["transcript"] == cand.rel):
            key = (c["transcript"], Q.normalize_quote(c["quote"]))
            target = index.get(key)  # already applied by a crashed run: never duplicate
            if target is None:
                d = decisions[c["idx"]]
                if d["decision"] == "attach" and d["item"] in pages:
                    target = d["item"]
                elif d["decision"] == "batch_ref":
                    target = resolved.get(d["batch_ref"])
            if target is not None and target in pages:
                item = F.Item.from_path(pages[target])
                created = False
            else:
                item = F.Item(id=F.next_item_id(), title=c["title"], summary=c["summary"],
                              origin="map")
                created = True
            added = item.add_evidence(c["kind"], c["transcript"], c["date"], c["quote"])
            _merge_goals(item, c["goals"])
            F.apply_quadrant(item, cause, active, at=at)
            item.save()
            pages[item.id] = item.path
            index[key] = item.id
            resolved[c["idx"]] = item.id
            d = decisions[c["idx"]]
            kept.append({"concept": c["title"], "decision": d["decision"], "item": item.id,
                         "justification": d["justification"]})
            if created:
                res["items_created"] += 1
            elif added:
                res["items_attached"] += 1
        mark_done(cand, len(kept), at, decisions=kept)
        res["mapped"] += 1


def _fail_parse(cands: list[Candidate], what: str, exc: "L.ParseFailure", at: str | None) -> dict:
    skipped = mark_parse_failure(cands, at)
    for rel in skipped:
        _event("degraded", f"frontier map: skipped {rel} after "
                           f"{_cfg('max_parse_attempts')} unparseable {what} replies ({exc.reason[:120]})")
    log(f"{what} reply unusable for {len(cands)} chat(s): deferred(parse)", job=JOB)
    return _result("done", deferred=len(cands), reason=f"parse: {exc.reason[:200]}")


def run_batch(qmd_factory=None, at: str | None = None) -> dict:
    """Map one batch. Status: done (more may remain), drained (nothing left),
    stopped (usage limit: ledger unmarked, stop the run), deferred (lock busy or
    retrieval degraded: ledger unmarked, stop the run)."""
    if qmd_factory is None:
        from qmd_client import QmdClient
        qmd_factory = QmdClient
    sel = select(int(_cfg("map_batch_chars")))
    for cand in sel.empties:
        mark_done(cand, 0, at)
    if not sel.batch:
        return _result("drained", mapped=len(sel.empties))
    cands = sel.batch
    goals = [g for g in G.load_goals() if g.status == "active"]
    res = _result("done")
    res["mapped"] = 0

    try:
        raw = L.call_json("extract", _extract_body(cands, goals),
                          lambda t: L.parse_extract(t, {c.rel for c in cands}, {g.id for g in goals}))
    except L.UsageLimit as exc:
        log(f"extract: {exc}; stopping with the ledger unmarked", job=JOB)
        return _result("stopped", reason=str(exc))
    except L.ParseFailure as exc:
        return _fail_parse(cands, "extract", exc, at)

    concepts, res["dropped_quotes"] = _verify(cands, raw)
    decisions: dict[int, dict] = {}
    if concepts:
        try:
            neighbours = _neighbours(concepts, qmd_factory)
        except OSError as exc:
            neighbours = None
            log(f"retrieval unavailable: {exc}", job=JOB)
        if neighbours is None:
            _event("degraded", "frontier map: qmd retrieval is degraded; the batch was deferred, nothing created")
            return _result("deferred", reason="retrieval degraded")
        try:
            decisions = _decide(concepts, neighbours)
        except L.UsageLimit as exc:
            log(f"match: {exc}; stopping with the ledger unmarked", job=JOB)
            return _result("stopped", reason=str(exc))
        except L.ParseFailure as exc:
            return _fail_parse(cands, "match", exc, at)

    try:
        with state_lock(p("meta") / "wiki.lock", timeout_s=float(_cfg("vault_lock_wait_seconds"))):
            _apply(cands, concepts, decisions, at, res)
    except TimeoutError as exc:
        log(f"apply deferred: {exc}", job=JOB)
        return _result("deferred", reason="wiki.lock busy")
    return res


# --------------------------------------------------------------------------
# Seeding the known unknowns from open loops
# --------------------------------------------------------------------------

def _theme(loop: "V.Loop") -> str:
    m = re.search(r"^## Theme\s*\n+(.+?)(?:\n\n|\Z)", loop.body or "", re.M | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:400] if m else loop.title


def seed_loops(at: str | None = None) -> dict:
    """One known-unknown item per unresolved loop, its occurrences as evidence.
    Idempotent: an existing item for a loop only gains occurrences it lacks."""
    statuses = set(_cfg("seed_loop_statuses"))
    out = {"created": 0, "existing": 0, "evidence_added": 0}
    with state_lock(p("meta") / "wiki.lock", timeout_s=float(_cfg("vault_lock_wait_seconds"))):
        by_loop: dict[str, F.Item] = {}
        for path in _item_pages().values():
            item = F.Item.from_path(path)
            for lid in item.loops:
                by_loop.setdefault(lid, item)
        active = F.active_goal_ids()
        for loop in V.load_loops():
            if loop.status not in statuses:
                continue
            item = by_loop.get(loop.id)
            created = item is None
            if created:
                item = F.Item(id=F.next_item_id(), title=loop.title.strip(),
                              summary=_theme(loop), origin="map", loops=[loop.id])
            added = 0
            for occ in loop.occurrences:
                # Some loop occurrences are repo-relative (`vault/sources/...`); evidence is
                # vault-relative, like every transcript reference the map writes.
                rel = occ.strip().strip("[]").removesuffix(".md").removeprefix("vault/")
                m = _DATE.search(rel)
                # An occurrence names the chat but carries no quote, and the loop title is
                # Dreamer's text (rule 13): the evidence quotes no one.
                if rel and item.add_evidence("asked", rel, m.group(1) if m else "", ""):
                    added += 1
            F.apply_quadrant(item, f"seed from loop {loop.id}", active, at=at)
            if created or added:
                item.save()
            out["created" if created else "existing"] += 1
            out["evidence_added"] += added
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def status() -> dict:
    ledger = load_ledger()
    sel = select(int(_cfg("map_batch_chars")))
    return {"done": len(ledger["done"]), "deferred": len(ledger["deferred"]),
            "skipped": len(ledger["skipped"]), "next_batch": len(sel.batch),
            "next_empty": len(sel.empties)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Knowledge frontier map builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-batch", help="map one batch; prints a JSON status")
    sub.add_parser("seed-loops", help="one known-unknown item per unresolved loop (idempotent)")
    sub.add_parser("status", help="ledger counts")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "run-batch":
            result = run_batch()
        elif args.cmd == "seed-loops":
            result = seed_loops()
        else:
            result = status()
    except TimeoutError as exc:
        print(f"frontier_map: {exc} (wiki.lock)", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
