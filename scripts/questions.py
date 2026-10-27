#!/usr/bin/env python3
"""The open-questions queue CLI and management (§5.6, §6.3, §6.4).

Single place the friend is allowed to have unfinished business with the owner.
Everything requiring owner input lands here: silence pings, drift questions,
execution diagnoses, wellbeing observations, failed-verification writes, fold
quarantines.

Properties:
- Every quarantine has an owner-facing exit.
- Nothing leaves the queue without an answer.
- Nothing ages out on its own.
- goals.max_open_questions throttles what is asked asynchronously, never what is retained.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, as_date, atomic_write_json, p, read_json_strict, state_lock, today,
)


def queue_path() -> Path:
    return p("meta") / "open-questions.json"


def owner_writes_path() -> Path:
    return p("meta") / "owner-writes.json"


def extract_human_turns(text: str) -> str:
    """Extract only human turn content from transcript text for quote verification."""
    if not text:
        return ""
    lines = text.splitlines()
    human_lines = []
    in_human = False
    saw_role = False

    for line in lines:
        stripped = line.strip()
        if re.match(r"^##\s+Human(\s+\(.*\))?$", stripped, re.IGNORECASE) or re.match(r"^Human:\s*", stripped, re.IGNORECASE):
            in_human = True
            saw_role = True
            if ":" in stripped and not stripped.startswith("#"):
                human_lines.append(stripped.split(":", 1)[1].strip())
            continue
        elif re.match(r"^##\s+(Assistant|System|Meta|Context|Tools|Session)(\s+.*)?$", stripped, re.IGNORECASE) or re.match(r"^(Assistant|System):\s*", stripped, re.IGNORECASE):
            in_human = False
            saw_role = True
            continue

        if in_human:
            human_lines.append(line)

    if human_lines:
        return "\n".join(human_lines)
    # Role markers but no Human turn: there are no owner words here. Only a
    # file with no markers at all (a bare note) is taken whole.
    return "" if saw_role else text


_QUOTE_FOLD = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def normalize_quote(text: str) -> str:
    """Fold case, whitespace and curly quotes so a quote survives line
    wrapping and typography between the session and its transcript."""
    return " ".join((text or "").translate(_QUOTE_FOLD).lower().split())


def quote_in(quote: str, normalized_corpus: str) -> bool:
    needle = normalize_quote(quote)
    return bool(needle) and needle in normalized_corpus


def human_text_of(path: Path) -> str:
    """One transcript's Human turns, normalized for quote matching."""
    return normalize_quote(extract_human_turns(path.read_text(encoding="utf-8")))


def human_corpus() -> str:
    """Every ingested Human turn, normalized — what an owner quote must appear in."""
    sources_dir = p("sources")
    texts = []
    if sources_dir.exists():
        for f in sorted(sources_dir.rglob("*.md")):
            try:
                texts.append(human_text_of(f))
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(texts)


def load_questions() -> list[dict]:
    return read_json_strict(queue_path(), default=[]) or []


def save_questions(questions: list[dict]) -> None:
    atomic_write_json(queue_path(), questions)


def load_owner_writes() -> list[dict]:
    return read_json_strict(owner_writes_path(), default=[]) or []


def save_owner_writes(writes: list[dict]) -> None:
    atomic_write_json(owner_writes_path(), writes)


def record_owner_write(
    tool: str,
    target_type: str,
    target_id: str,
    owner_quote: str,
    session_id: str,
    params: dict,
    confirmation: str,
    at: _dt.datetime | None = None,
) -> dict:
    """Record an authorized owner write to owner-writes.json."""
    with state_lock(p("meta") / "wiki.lock"):
        writes = load_owner_writes()
        at_val = at.isoformat(timespec="seconds") if at else _dt.datetime.now().isoformat(timespec="seconds")
        write_rec = {
            "id": f"W-{uuid.uuid4().hex[:8]}",
            "at": at_val,
            "tool": tool,
            "target_type": target_type,
            "target_id": target_id,
            "owner_quote": owner_quote.strip(),
            "session_id": session_id.strip(),
            "params": params,
            "confirmation": confirmation.strip(),
            "verified": None,
            "verified_at": None,
        }
        writes.append(write_rec)
        save_owner_writes(writes)
        return write_rec


def reconcile_owner_writes(now: _dt.datetime | None = None) -> dict:
    """Settle the verification state of every owner write (spec §5.4).

    A quote found in an ingested Human turn verifies the write. One still
    missing after mcp.owner_write_verify_lag_days is quarantined
    (verified=False) and enqueued as an `unverified-write` question, so it
    comes back to the owner instead of lingering in a ledger. The write itself
    is never reverted: undoing real work on a matching failure would be worse
    than asking. If the session is ingested late, the quarantine clears.
    """
    now = now or _dt.datetime.now()
    lag_days = float((CFG.get("mcp") or {}).get("owner_write_verify_lag_days", 3))
    corpus = human_corpus()
    updates: dict[str, dict] = {}
    cleared: list[str] = []

    for w in load_owner_writes():
        if w.get("verified") is True:
            continue
        quote = w.get("owner_quote") or ""
        if quote_in(quote, corpus):
            updates[w["id"]] = {"verified": True,
                                "verified_at": now.isoformat(timespec="seconds")}
            if w.get("question_id"):
                cleared.append(w["question_id"])
            continue
        if w.get("verified") is False:
            continue  # already quarantined and asked about
        try:
            age_days = (now - _dt.datetime.fromisoformat(str(w.get("at")))).total_seconds() / 86400
        except ValueError:
            age_days = lag_days + 1  # undatable: treat as overdue, never as fresh
        if age_days <= lag_days:
            continue
        target = w.get("target_id") or "-"
        q = enqueue_question(
            kind="unverified-write",
            text=(f"Write {w['id']} ({w.get('tool')} on {target}) was authorized with the "
                  f"quote \"{quote.strip()}\", but no ingested conversation contains you "
                  "saying that. Did you make this change?"),
            goal_id=target if w.get("target_type") == "goal" else None,
            loop_id=target if w.get("target_type") == "loop" else None,
            ref_id=w["id"],
            context={"write_id": w["id"], "tool": w.get("tool"), "at": w.get("at")},
        )
        updates[w["id"]] = {"verified": False, "question_id": q["id"]}

    if updates:
        with state_lock(p("meta") / "wiki.lock"):
            writes = load_owner_writes()
            for w in writes:
                w.update(updates.get(w.get("id"), {}))
            save_owner_writes(writes)
    pending_ids = {q["id"] for q in load_questions() if q.get("status") != "answered"}
    for qid in cleared:
        if qid in pending_ids:
            answer_question(qid, "Verified: the quote appeared in a later-ingested conversation.")

    return {
        "verified": sum(1 for u in updates.values() if u["verified"] is True),
        "quarantined": sum(1 for u in updates.values() if u["verified"] is False),
    }


def enqueue_question(
    kind: str,
    text: str,
    goal_id: str | None = None,
    loop_id: str | None = None,
    context: dict | None = None,
    ref_id: str | None = None,
) -> dict:
    """Enqueue an open question. Idempotent on (kind, goal_id, loop_id, ref_id)
    while pending or active deferred. `ref_id` tells apart questions that
    share a target — two quarantined writes on one goal are two questions."""
    with state_lock(p("meta") / "wiki.lock"):
        questions = load_questions()

        def same(q: dict) -> bool:
            return (q.get("kind") == kind and q.get("goal_id") == goal_id
                    and q.get("loop_id") == loop_id and q.get("ref_id") == ref_id)

        for q in questions:
            q_status = q.get("status")
            if q_status in ("pending", "asked"):
                if same(q):
                    return q
            elif q_status == "deferred":
                defer_until = q.get("defer_until")
                if defer_until and defer_until > today().isoformat() and same(q):
                    return q

        now_iso = _dt.datetime.now().isoformat(timespec="seconds")
        item = {
            "id": f"Q-{uuid.uuid4().hex[:8]}",
            "kind": kind,
            "goal_id": goal_id,
            "loop_id": loop_id,
            "ref_id": ref_id,
            "text": text.strip(),
            "context": context or {},
            "created_at": now_iso,
            "status": "pending",
            "asked_at": None,
            "asked_surface": None,
            "answer": None,
            "answered_at": None,
            "defer_until": None,
            "backoff_count": 0,
            "last_response": None,
        }
        questions.append(item)
        save_questions(questions)
        return item


def list_questions(
    status: str | None = "unresolved",
    surface: str = "all",
    limit: int | None = None,
    ref: _dt.date | None = None,
) -> list[dict]:
    ref = ref or today()
    ref_iso = ref.isoformat()
    questions = load_questions()

    eligible = []
    for q in questions:
        q_status = q.get("status")
        defer_until = q.get("defer_until")
        if status == "unresolved":
            if q_status in ("pending", "asked"):
                pass
            elif q_status == "deferred" and defer_until and defer_until <= ref_iso:
                pass
            else:
                continue
        elif status is not None and status != "all":
            if q_status != status:
                continue

        if defer_until and defer_until > ref_iso:
            continue
        if surface == "async":
            # §6.2: Only silence interruptions asynchronously
            if q.get("kind") != "silence":
                continue
            # Once asked async, never re-asked async (§6.3)
            if q.get("asked_surface") == "async":
                continue
        elif surface == "review":
            # In review, include all kinds of questions
            pass
        eligible.append(q)

    # Priority by age (oldest first)
    eligible.sort(key=lambda q: q.get("created_at") or "")

    if surface == "async" and limit is None:
        cfg_g = CFG.get("goals") or {}
        limit = int(cfg_g.get("max_open_questions", 2))

    if limit is not None:
        return eligible[:limit]
    return eligible


def mark_asked(question_ids: list[str], surface: str) -> None:
    with state_lock(p("meta") / "wiki.lock"):
        now_iso = _dt.datetime.now().isoformat(timespec="seconds")
        questions = load_questions()
        for q in questions:
            if q.get("id") in question_ids and q.get("status") == "pending":
                q["status"] = "asked"
                q["asked_at"] = now_iso
                q["asked_surface"] = surface
        save_questions(questions)


def answer_question(
    question_id: str,
    answer_text: str,
    owner_quote: str = "",
    session_id: str = "",
) -> dict:
    with state_lock(p("meta") / "wiki.lock"):
        questions = load_questions()
        for q in questions:
            if q.get("id") == question_id:
                now_iso = _dt.datetime.now().isoformat(timespec="seconds")
                q["status"] = "answered"
                q["answer"] = answer_text.strip()
                q["answered_at"] = now_iso
                if owner_quote:
                    q["last_response"] = owner_quote.strip()
                save_questions(questions)
                return q
        raise ValueError(f"Question {question_id} not found in queue")


def defer_question(
    question_id: str,
    days: int | None = None,
    owner_quote: str = "",
    session_id: str = "",
    ref: _dt.date | None = None,
) -> dict:
    """Back off on request (§6.4). Snoozes question for back_off_days, doubles window on subsequent backoffs."""
    ref = ref or today()
    with state_lock(p("meta") / "wiki.lock"):
        questions = load_questions()
        cfg_g = CFG.get("goals") or {}
        base_days = int(cfg_g.get("back_off_days", 14))

        for q in questions:
            if q.get("id") == question_id:
                count = int(q.get("backoff_count") or 0) + 1
                q["backoff_count"] = count
                snooze_days = days if days is not None else base_days * (2 ** (count - 1))
                defer_date = ref + _dt.timedelta(days=snooze_days)
                q["defer_until"] = defer_date.isoformat()
                q["status"] = "deferred"
                if owner_quote:
                    q["last_response"] = owner_quote.strip()
                save_questions(questions)
                return q
        raise ValueError(f"Question {question_id} not found in queue")


def backoff_goal(
    goal_id: str,
    owner_quote: str,
    session_id: str,
    ref: _dt.date | None = None,
) -> list[dict]:
    """'Leave it' on a goal (§6.4) snoozes every pending question for that goal."""
    ref = ref or today()
    questions = load_questions()
    deferred = []
    for q in questions:
        if q.get("goal_id") == goal_id and q.get("status") in ("pending", "asked"):
            deferred.append(defer_question(q["id"], owner_quote=owner_quote, session_id=session_id, ref=ref))
    return deferred


# --------------------------------------------------------------------------
# Quarantine exits (§5.6, §12.4) — nothing is quarantined and forgotten
# --------------------------------------------------------------------------

def _fold_ref(entry: dict) -> str:
    return f"{entry.get('loop_id')}|{entry.get('occurrence')}"


def sync_fold_quarantine() -> dict:
    """Mirror fold-quarantine.json onto the queue: one question per
    quarantined fold, and a question settled once its entry is repaired."""
    entries = read_json_strict(p("meta") / "fold-quarantine.json", default=[]) or []
    live = {}
    for e in entries:
        if isinstance(e, dict) and e.get("loop_id") and e.get("occurrence"):
            live[_fold_ref(e)] = e

    enqueued = 0
    known = {q.get("ref_id") for q in load_questions() if q.get("kind") == "fold-quarantine"
             and q.get("status") != "answered"}
    for ref, e in live.items():
        if ref in known:
            continue
        enqueue_question(
            kind="fold-quarantine",
            text=(f"The thread update for {e['loop_id']} from {e['occurrence']} failed "
                  f"{e.get('attempts', 'several')} times and is quarantined, so that "
                  "conversation is missing from the loop's thread. Retry it "
                  "(`python3 scripts/fold_pending.py requeue-quarantined`) or leave it out?"),
            loop_id=e["loop_id"], ref_id=ref,
            context={"occurrence": e["occurrence"], "reason": e.get("reason")},
        )
        enqueued += 1

    settled = 0
    for q in load_questions():
        if (q.get("kind") == "fold-quarantine" and q.get("status") != "answered"
                and q.get("ref_id") not in live):
            answer_question(q["id"], "Repaired: the fold was re-enqueued.")
            settled += 1
    return {"enqueued": enqueued, "settled": settled}


def note_derived_claims(loop_id: str, conclusion: str, count: int) -> dict:
    """A conclusion that leaned on Dreamer's own prior output (rule 13) had
    those claims capped and quarantined. Ask the owner about it once."""
    return enqueue_question(
        kind="derived-claims",
        text=(f"The conclusion for {loop_id} ({conclusion}) had {count} claim(s) that cited "
              "Dreamer's own earlier writing instead of a primary source; they were capped "
              "at contested and set apart. Is that conclusion still good enough, or should "
              "the loop be researched again?"),
        loop_id=loop_id, ref_id=conclusion,
        context={"conclusion": conclusion, "count": count},
    )


def render_for_owner(questions: list[dict]) -> str:
    """The async surfaces' shared wording (§6.1): what the assistant in the
    session is told when the Friend has a question for the owner."""
    if not questions:
        return ""
    lines = [
        "Dreamer (the owner's goal tracker) has a question waiting for the owner.",
        "Ask it in your own words, one at a time, before moving on. Record the",
        "reply with the dreamer `answer_question` tool, quoting the owner's words",
        "verbatim; if they say to leave it, call it with defer=true and do not ask again.",
        "",
    ]
    for q in questions:
        target = q.get("goal_id") or q.get("loop_id") or "-"
        lines.append(f"- [{q['id']}] ({q.get('kind')}, {target}) {q['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Weekly-review offer (§6.7) — one offer per digest, never one per session
# --------------------------------------------------------------------------

def review_offer_path() -> Path:
    return p("meta") / "review-offer.json"


def last_review_offer() -> str | None:
    state = read_json_strict(review_offer_path(), default={}) or {}
    return state.get("digest")


def mark_review_offered(digest_name: str) -> None:
    with state_lock(p("meta") / "wiki.lock"):
        atomic_write_json(review_offer_path(), {
            "digest": digest_name,
            "offered_at": _dt.datetime.now().isoformat(timespec="seconds"),
        })


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list")
    lp.add_argument("--all", action="store_true", help="include answered/deferred")
    lp.add_argument("--surface", choices=["all", "async", "review"], default="all")
    lp.add_argument("--limit", type=int, default=None)

    ans = sub.add_parser("answer")
    ans.add_argument("--id", required=True)
    ans.add_argument("--answer", required=True)
    ans.add_argument("--quote", default="")
    ans.add_argument("--session", default="")

    df = sub.add_parser("defer")
    df.add_argument("--id", required=True)
    df.add_argument("--days", type=int, default=None)
    df.add_argument("--quote", default="")
    df.add_argument("--session", default="")

    args = ap.parse_args()

    if args.cmd == "list":
        status_filter = "all" if args.all else "unresolved"
        items = list_questions(status=status_filter, surface=args.surface, limit=args.limit)
        print(json.dumps(items, indent=2))
    elif args.cmd == "answer":
        res = answer_question(args.id, args.answer, owner_quote=args.quote, session_id=args.session)
        print(json.dumps(res, indent=2))
    elif args.cmd == "defer":
        res = defer_question(args.id, days=args.days, owner_quote=args.quote, session_id=args.session)
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
