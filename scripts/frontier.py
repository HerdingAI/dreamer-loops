#!/usr/bin/env python3
"""Knowledge frontier store and quadrant rule.

One module owns the frontier pages (`vault/frontier/items/K<NNNN>.md`,
`vault/frontier/lessons/K<NNNN>-<n>.md`) and the rule that places an item in a
quadrant, so every writer moves items the same way. The map builder,
pre-mortem, lesson writer, tap sync and MCP tools call the API below; none of
them re-implements the rule.

The quadrant rule, first match wins:
  1. a `got_it` tap in effect (not tombstoned)            -> known_known
  2. an aired lesson pins the quadrant it aired in; a
     `dig_deeper` tap keeps the pin                        -> that quadrant
  3. owner evidence (explained / decided / applied)        -> unknown_known if any
     ACTIVE goal relation is unconnected, else known_known
  4. asked / reconstructed evidence, or a linked loop      -> known_unknown
  5. otherwise (pre-mortem origin, or only assistant
     `exposure` evidence)                                  -> unknown_unknown
Only the owner's words reach a "known" quadrant (rule 13): `exposure` evidence
is recorded but never counts.

Locking: `next_item_id` and every writer here assume the caller holds
wiki.lock (as `vault.next_loop_id` does). Only the CLI takes it itself.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, p, read_page, state_lock, write_page  # noqa: E402
import questions as Q  # noqa: E402

QUADRANTS = ("known_known", "known_unknown", "unknown_known", "unknown_unknown")
ORIGINS = ("map", "premortem")
EVIDENCE_KINDS = ("explained", "decided", "applied", "asked", "reconstructed", "exposure")
OWNER_KNOWN_KINDS = {"explained", "decided", "applied"}
OWNER_UNRESOLVED_KINDS = {"asked", "reconstructed"}
RELATIONS = ("connected", "unconnected")
LESSON_KINDS = ("new", "follow_up")
LESSON_STATUSES = ("ready", "held", "dropped", "rejected", "released", "failed",
                   "aired", "got_it", "dig_deeper")
TAP_KINDS = ("got_it", "dig_deeper")
ACTORS = ("owner", "operator")

LESSON_BODY = ("## Lesson\n\n## Claims\n\n## Sources\n\n"
               "## Web sources (untrusted)\n\n## Egress record\n")

_ITEM_ID_RE = re.compile(r"^K(\d{4,})$")
_LESSON_ID_RE = re.compile(r"^K\d{4,}-\d+$")
_LESSON_FILE_RE = re.compile(r"^K(\d{4,})-\d+\.md$")


class FrontierError(ValueError):
    """A refused frontier write (illegal value, missing cause, nothing to undo)."""


# --------------------------------------------------------------------------
# Paths and small helpers
# --------------------------------------------------------------------------

def frontier_dir() -> Path:
    return p("frontier")


def items_dir() -> Path:
    return frontier_dir() / "items"


def lessons_dir() -> Path:
    return frontier_dir() / "lessons"


def item_path(item_id: str) -> Path:
    return items_dir() / f"{item_id}.md"


def lesson_path(lesson_id: str) -> Path:
    # A lesson id reaches here from the show and from Claude: refuse anything that is not
    # exactly one lesson file's name, so "../items/K2001" cannot leave the lessons directory.
    if not _LESSON_ID_RE.match(str(lesson_id)):
        raise FrontierError(f"not a lesson id: {str(lesson_id)[:60]!r}")
    return lessons_dir() / f"{lesson_id}.md"


def _cfg(key: str):
    return (CFG.get("frontier") or {})[key]


def _now(at: str | None) -> str:
    return at or _dt.datetime.now().isoformat(timespec="seconds")


def _s(value) -> str:
    """Dates a hand edit left unquoted come back from YAML as date objects."""
    if value is None:
        return ""
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value)


def _need_cause(cause: str) -> str:
    cause = (cause or "").strip()
    if len(cause) < int(_cfg("cause_min_chars")):
        raise FrontierError(
            f"a cause of at least frontier.cause_min_chars characters is required, got {cause!r}")
    return cause


def active_goal_ids() -> set[str]:
    """Ids of goals whose status is `active`. A paused goal's relations must
    not create unknown known, so callers pass this set, not every goal."""
    import goals as G  # deferred: goals imports vault, which this module needs not
    return {g.id for g in G.load_goals() if g.status == "active"}


# --------------------------------------------------------------------------
# Item
# --------------------------------------------------------------------------

@dataclass
class Item:
    id: str
    title: str
    summary: str = ""
    quadrant: str = ""
    origin: str = "map"
    evidence: list[dict] = field(default_factory=list)
    goals: list[dict] = field(default_factory=list)
    loops: list[str] = field(default_factory=list)
    moves: list[dict] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None

    @classmethod
    def from_path(cls, path: Path) -> "Item":
        fm, body = read_page(path)
        return cls(
            id=str(fm.get("id", "")),
            title=str(fm.get("title", "")),
            summary=str(fm.get("summary") or ""),
            quadrant=str(fm.get("quadrant") or ""),
            origin=str(fm.get("origin") or "map"),
            evidence=[{"kind": str(e.get("kind", "")), "transcript": _s(e.get("transcript")),
                       "date": _s(e.get("date")), "quote": str(e.get("quote", ""))}
                      for e in fm.get("evidence") or []],
            goals=[{k: _s(v) for k, v in g.items()} for g in fm.get("goals") or []],
            loops=[str(x) for x in fm.get("loops") or []],
            moves=[{"at": _s(m.get("at")), "from": str(m.get("from") or ""),
                    "to": str(m.get("to") or ""), "cause": str(m.get("cause", ""))}
                   for m in fm.get("moves") or []],
            lessons=[str(x) for x in fm.get("lessons") or []],
            body=body,
            path=path,
        )

    def frontmatter(self) -> dict:
        return {
            "type": "frontier-item",
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "quadrant": self.quadrant,
            "origin": self.origin,
            "evidence": self.evidence,
            "goals": self.goals,
            "loops": self.loops,
            "moves": self.moves,
            "lessons": self.lessons,
        }

    def save(self) -> None:
        if self.quadrant and self.quadrant not in QUADRANTS:
            raise FrontierError(f"{self.id}: illegal quadrant {self.quadrant!r}")
        if self.origin not in ORIGINS:
            raise FrontierError(f"{self.id}: illegal origin {self.origin!r}")
        if self.path is None:
            self.path = item_path(self.id)
        write_page(self.path, self.frontmatter(), self.body or f"# {self.title}\n")

    def add_evidence(self, kind: str, transcript: str, date, quote: str) -> bool:
        """Append evidence unless this transcript + normalised quote is already
        on the item (a resumed batch must not double-count). Returns
        whether anything was appended. Does not save."""
        if kind not in EVIDENCE_KINDS:
            raise FrontierError(f"{self.id}: illegal evidence kind {kind!r}")
        key = (transcript, Q.normalize_quote(quote))
        for e in self.evidence:
            if (e["transcript"], Q.normalize_quote(e["quote"])) == key:
                return False
        self.evidence.append({"kind": kind, "transcript": transcript,
                              "date": _s(date), "quote": quote})
        return True

    def set_goal(self, goal: str, relation: str, cause: str = "") -> None:
        """Set (or replace) this item's relation to one goal. Does not save."""
        if relation not in RELATIONS:
            raise FrontierError(f"{self.id}: illegal goal relation {relation!r}")
        entry = {"goal": goal, "relation": relation}
        if cause:
            entry["cause"] = cause
        self.goals = [g for g in self.goals if g["goal"] != goal] + [entry]


# --------------------------------------------------------------------------
# Lesson
# --------------------------------------------------------------------------

@dataclass
class Lesson:
    id: str
    item: str
    goal: str = ""
    kind: str = "new"
    origin_tap: str = ""
    cause: str = ""
    cause_rank: int = 0
    status: str = "ready"
    released_at: str = ""
    body_hash: str = ""
    aired_on: str = ""
    # The quadrant the item held when this lesson aired: the pin of rule 2.
    aired_quadrant: str = ""
    taps: list[dict] = field(default_factory=list)
    tombstones: list[dict] = field(default_factory=list)
    parse_attempts: int = 0
    check: dict = field(default_factory=dict)
    body: str = ""
    path: Path | None = None

    @classmethod
    def from_path(cls, path: Path) -> "Lesson":
        fm, body = read_page(path)
        return cls(
            id=str(fm.get("id", "")),
            item=str(fm.get("item", "")),
            goal=str(fm.get("goal") or ""),
            kind=str(fm.get("kind") or "new"),
            origin_tap=str(fm.get("origin_tap") or ""),
            cause=str(fm.get("cause") or ""),
            cause_rank=int(fm.get("cause_rank") or 0),
            status=str(fm.get("status") or "ready"),
            released_at=_s(fm.get("released_at")),
            body_hash=str(fm.get("body_hash") or ""),
            aired_on=_s(fm.get("aired_on")),
            aired_quadrant=str(fm.get("aired_quadrant") or ""),
            taps=[{"id": str(t.get("id", "")), "kind": str(t.get("kind", "")),
                   "at": _s(t.get("at"))} for t in fm.get("taps") or []],
            tombstones=[{**t, "at": _s(t.get("at"))} for t in fm.get("tombstones") or []],
            parse_attempts=int(fm.get("parse_attempts") or 0),
            check=dict(fm.get("check") or {}),
            body=body,
            path=path,
        )

    def frontmatter(self) -> dict:
        return {
            "type": "frontier-lesson",
            "id": self.id,
            "item": self.item,
            "goal": self.goal,
            "kind": self.kind,
            "origin_tap": self.origin_tap,
            "cause": self.cause,
            "cause_rank": self.cause_rank,
            "status": self.status,
            "released_at": self.released_at,
            "body_hash": self.body_hash,
            "aired_on": self.aired_on,
            "aired_quadrant": self.aired_quadrant,
            "taps": self.taps,
            "tombstones": self.tombstones,
            "parse_attempts": self.parse_attempts,
            "check": self.check,
        }

    def save(self) -> None:
        if self.status not in LESSON_STATUSES:
            raise FrontierError(f"{self.id}: illegal lesson status {self.status!r}")
        if self.kind not in LESSON_KINDS:
            raise FrontierError(f"{self.id}: illegal lesson kind {self.kind!r}")
        if self.path is None:
            self.path = lesson_path(self.id)
        write_page(self.path, self.frontmatter(), self.body or LESSON_BODY)

    # -- taps ---------------------------------------------------------------
    def live_taps(self, kind: str) -> list[dict]:
        """Taps of `kind` not yet tombstoned (each tombstone names its tap)."""
        dead = {t.get("tap") for t in self.tombstones if t.get("kind") == kind}
        return [t for t in self.taps if t["kind"] == kind and t["id"] not in dead]

    def tap_in_effect(self, kind: str) -> bool:
        return bool(self.live_taps(kind))

    def refresh_status(self) -> None:
        """aired <-> got_it / dig_deeper follows the taps in effect. Statuses
        before airing (ready, released, failed, ...) are not touched."""
        if self.status not in ("aired", "got_it", "dig_deeper"):
            return
        if self.tap_in_effect("got_it"):
            self.status = "got_it"
        elif self.tap_in_effect("dig_deeper"):
            self.status = "dig_deeper"
        else:
            self.status = "aired"


# --------------------------------------------------------------------------
# Ids
# --------------------------------------------------------------------------

def next_item_id() -> str:
    """Vault-wide monotonic K<NNNN>, like next_loop_id. Lesson pages count too,
    so an id whose item page is gone is still never handed out again."""
    highest = 0
    for d, rx in ((items_dir(), _ITEM_ID_RE), (lessons_dir(), _LESSON_FILE_RE)):
        if not d.exists():
            continue
        for path in d.glob("K*.md"):
            m = rx.match(path.stem if rx is _ITEM_ID_RE else path.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return f"K{highest + 1:04d}"


def next_lesson_id(item_id: str) -> str:
    """K<NNNN>-<n>, n counting up per item."""
    highest = 0
    if lessons_dir().exists():
        for path in lessons_dir().glob(f"{item_id}-*.md"):
            tail = path.stem.rsplit("-", 1)[1]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return f"{item_id}-{highest + 1}"


# --------------------------------------------------------------------------
# Quadrant rule
# --------------------------------------------------------------------------

def _item_lessons(item: Item) -> list[Lesson]:
    out = []
    for lid in item.lessons:
        path = lesson_path(lid)
        if path.exists():
            out.append(Lesson.from_path(path))
    return out


def derive_quadrant(item: Item, active_goals: Iterable[str],
                    lessons: list[Lesson] | None = None) -> str:
    """The quadrant `item` belongs in now. `active_goals` is the ids of ACTIVE
    goals only. `lessons` defaults to the item's lesson pages on disk."""
    active = set(active_goals)
    lessons = _item_lessons(item) if lessons is None else lessons
    if any(l.tap_in_effect("got_it") for l in lessons):
        return "known_known"
    aired = sorted((l for l in lessons if l.aired_on and l.aired_quadrant),
                   key=lambda l: l.id)
    if aired:
        return aired[0].aired_quadrant
    kinds = {e["kind"] for e in item.evidence}
    if kinds & OWNER_KNOWN_KINDS:
        if any(g["relation"] == "unconnected" and g["goal"] in active for g in item.goals):
            return "unknown_known"
        return "known_known"
    if kinds & OWNER_UNRESOLVED_KINDS or item.loops:
        return "known_unknown"
    return "unknown_unknown"


def apply_quadrant(item: Item, cause: str, active_goals: Iterable[str] | None = None,
                   at: str | None = None) -> dict | None:
    """Move `item` to its derived quadrant, recording the move and why.
    Returns the move, or None when nothing changed. Mutates `item`; the caller
    saves."""
    cause = (cause or "").strip()
    if not cause:
        raise FrontierError(f"{item.id}: a quadrant move needs a cause")
    if active_goals is None:
        active_goals = active_goal_ids()
    new = derive_quadrant(item, active_goals)
    if new == item.quadrant:
        return None
    move = {"at": _now(at), "from": item.quadrant, "to": new, "cause": cause}
    item.moves.append(move)
    item.quadrant = new
    return move


def create_item(title: str, summary: str, origin: str, cause: str,
                at: str | None = None) -> Item:
    """Allocate an id and write a new item with its first move (from "")."""
    item = Item(id=next_item_id(), title=title.strip(), summary=summary.strip(), origin=origin)
    apply_quadrant(item, cause, active_goals=(), at=at)
    item.save()
    return item


def create_lesson(item: Item, goal: str, kind: str, cause: str, cause_rank: int,
                  origin_tap: str = "", status: str = "ready", body: str = "",
                  check: dict | None = None) -> Lesson:
    """Allocate a lesson id under `item`, write the lesson, then link it. The lesson
    is written ONCE with its final `status`, page `body` and `check` record: the
    lesson writer builds everything in memory first, so a `ready` page that has not
    passed every gate never exists."""
    lesson = Lesson(id=next_lesson_id(item.id), item=item.id, goal=goal, kind=kind,
                    origin_tap=origin_tap, cause=cause, cause_rank=cause_rank,
                    status=status, body=body, check=dict(check or {}))
    lesson.save()
    item.lessons.append(lesson.id)
    item.save()
    return lesson


# --------------------------------------------------------------------------
# Lesson lifecycle touching the quadrant: aired, taps, undo
# --------------------------------------------------------------------------

# A lesson in one of these states has reached the show, so a tap on it may exist
# that Dreamer has not pulled yet: an undo then writes a pre-emptive tombstone.
DELIVERED_STATUSES = ("released", "aired", "got_it", "dig_deeper")


def _at_key(value: str) -> tuple[int, float | str]:
    """Sortable form of a timestamp: (0, epoch) when it parses (a naive time is local),
    else (1, text). Compare only keys of the same shape."""
    try:
        return 0, _dt.datetime.fromisoformat(_s(value)).timestamp()
    except ValueError:
        return 1, _s(value)


def at_or_before(a: str, b: str) -> bool:
    """True when time `a` is at or before time `b`."""
    ka, kb = _at_key(a), _at_key(b)
    return ka <= kb if ka[0] == kb[0] else _s(a) <= _s(b)

def mark_aired(lesson_id: str, aired_on: str, at: str | None = None) -> Lesson:
    """Record that the lesson aired and pin the item's current quadrant.
    The first airing's pin wins; the item does not move."""
    lesson = Lesson.from_path(lesson_path(lesson_id))
    item = Item.from_path(item_path(lesson.item))
    lesson.aired_on = _s(aired_on)
    if not lesson.aired_quadrant:
        lesson.aired_quadrant = item.quadrant
    lesson.status = "aired"
    lesson.refresh_status()
    lesson.save()
    return lesson


def _sync_item(lesson: Lesson, cause: str, at: str | None) -> dict | None:
    item = Item.from_path(item_path(lesson.item))
    move = apply_quadrant(item, cause, at=at)
    if move:
        item.save()
    return move


def add_tap(lesson_id: str, kind: str, tap_id: str, at: str | None = None) -> dict | None:
    """Record an owner tap (idempotent on tap_id), then move the item; a replay
    of a recorded tap re-syncs the item. Returns the move, if any."""
    if kind not in TAP_KINDS:
        raise FrontierError(f"{lesson_id}: illegal tap kind {kind!r}")
    lesson = Lesson.from_path(lesson_path(lesson_id))
    cause = f"tap {kind} {tap_id} on {lesson.id}"
    if not any(t["id"] == tap_id for t in lesson.taps):
        # A tap made at or before an undo of its kind was undone by that undo,
        # whether the undo came after the tap was pulled or before.
        stamp = _now(at)
        if any(t.get("kind") == kind and at_or_before(stamp, t["at"]) for t in lesson.tombstones):
            return None
        lesson.taps.append({"id": tap_id, "kind": kind, "at": stamp})
        lesson.refresh_status()
        lesson.save()
    # A replay still syncs the item: a crash between the two writes must heal.
    # apply_quadrant writes no move when nothing changed, so a clean replay is a no-op.
    return _sync_item(lesson, cause, at)


MCP_TAP_PREFIX = "mcp-"


def _next_mcp_tap_id(lesson: Lesson) -> str:
    """`mcp-<lesson id>-<n>`, n one past the highest on this lesson. The show's tap ids are
    plain integers, so a prefixed id cannot collide with one. The lesson id is in the tap id
    because build_queue dedupes follow-ups by origin_tap across the whole item: a per-lesson
    "mcp-1" on the follow-up would collide with the first lesson's and be skipped silently."""
    head = f"{MCP_TAP_PREFIX}{lesson.id}-"
    used = [int(t["id"][len(head):]) for t in lesson.taps
            if t["id"].startswith(head) and t["id"][len(head):].isdigit()]
    return f"{head}{max(used, default=0) + 1}"


UNDELIVERED = ("ready", "held")


def set_lesson_status(lesson: Lesson, status: str, cause: str, at: str) -> None:
    """Move a lesson and record why on its `check` record (the Lesson schema is unchanged)."""
    lesson.status = status
    lesson.check = {**lesson.check, "status_cause": cause, "status_at": at}
    lesson.save()


def cancel_follow_ups(lesson: Lesson, tap_id: str, at: str) -> list[str]:
    """A Got it cancels the item's unreleased follow-up lessons, whichever way the tap
    arrived (the consumer or Claude). Returns the ids dropped."""
    item_file = item_path(lesson.item)
    if not item_file.exists():
        return []
    dropped = []
    for lid in Item.from_path(item_file).lessons:
        path = lesson_path(lid)
        other = Lesson.from_path(path) if path.exists() else None
        if other and other.kind == "follow_up" and other.status in UNDELIVERED:
            set_lesson_status(other, "dropped", f"got_it tap {tap_id} on {lesson.id}", at)
            dropped.append(lid)
    return dropped


def tap_lesson(lesson_id: str, kind: str, owner_quote: str, session_id: str,
               at: str | None = None) -> dict:
    """The owner's tap made through Claude: the same effect as a tap in the consumer
    (`add_tap`), recorded with actor owner in the owner-writes ledger (rule 16).
    Refused without the owner's quote and session, on a lesson that has not been
    delivered, and when a tap of that kind is already in effect (one of each per
    lesson). Returns {"tap": id, "move": move-or-None}."""
    if kind not in TAP_KINDS:
        raise FrontierError(f"{lesson_id}: illegal tap kind {kind!r}")
    min_chars = int((CFG.get("mcp") or {}).get("owner_write_quote_min_chars", 8))
    if len((owner_quote or "").strip()) < min_chars or not (session_id or "").strip():
        raise FrontierError(f"an owner tap needs owner_quote (>= {min_chars} chars) and session_id")
    path = lesson_path(lesson_id)
    if not path.exists():
        raise FrontierError(f"no such lesson: {lesson_id}")
    lesson = Lesson.from_path(path)
    if lesson.status not in DELIVERED_STATUSES:
        raise FrontierError(f"{lesson_id}: not delivered yet (status {lesson.status}), nothing to tap")
    if lesson.tap_in_effect(kind):
        raise FrontierError(f"{lesson_id}: a {kind} tap is already in effect")
    tap_id = _next_mcp_tap_id(lesson)
    # Microseconds: an undo stamps whole seconds, and a tap made in the same second
    # as that undo must read as after it (add_tap drops a tap at or before a tombstone).
    stamp = at or _dt.datetime.now().isoformat(timespec="microseconds")
    Q.record_owner_write(
        tool="tap_lesson", target_type="lesson", target_id=lesson_id,
        owner_quote=owner_quote, session_id=session_id,
        params={"kind": kind, "tap": tap_id}, confirmation=f"tapped {kind} on {lesson_id}",
    )
    move = add_tap(lesson_id, kind, tap_id, at=stamp)
    dropped: list[str] = []
    if kind == "got_it" and any(t["id"] == tap_id for t in Lesson.from_path(path).taps):
        dropped = cancel_follow_ups(lesson, tap_id, stamp)  # not when the tap was undone first
    return {"tap": tap_id, "move": move, "follow_ups_cancelled": dropped}


def undo_tap(lesson_id: str, kind: str, actor: str, cause: str,
             owner_quote: str = "", session_id: str = "", at: str | None = None) -> dict | None:
    """The one undo path. Tombstones the latest tap of `kind` in effect and
    re-derives the item's quadrant, both with the cause recorded. On a delivered
    lesson with no tap in effect it writes a pre-emptive tombstone (tap "") that
    add_tap honours: a tap made before the undo but pulled after it is ignored.

    actor "owner" (the MCP tool) must carry the owner's quote and session and
    is written to the owner-writes ledger (rule 16). actor "operator" (the CLI,
    for operator test taps) claims no owner consent and writes no ledger entry."""
    if actor not in ACTORS:
        raise FrontierError(f"illegal actor {actor!r}")
    if kind not in TAP_KINDS:
        raise FrontierError(f"{lesson_id}: illegal tap kind {kind!r}")
    cause = _need_cause(cause)
    lesson = Lesson.from_path(lesson_path(lesson_id))
    live = lesson.live_taps(kind)
    # With no tap in effect the undo is still legal on a delivered lesson: the tap it
    # answers may sit on the show, not yet pulled, and the tombstone must outlive it.
    if not live and lesson.status not in DELIVERED_STATUSES:
        raise FrontierError(f"{lesson_id}: no {kind} tap in effect to undo")
    owner_write = None
    if actor == "owner":
        min_chars = int((CFG.get("mcp") or {}).get("owner_write_quote_min_chars", 8))
        if len((owner_quote or "").strip()) < min_chars or not (session_id or "").strip():
            raise FrontierError(
                f"an owner undo needs owner_quote (>= {min_chars} chars) and session_id")
        owner_write = Q.record_owner_write(
            tool="undo_tap", target_type="lesson", target_id=lesson_id,
            owner_quote=owner_quote, session_id=session_id,
            params={"kind": kind, "cause": cause}, confirmation=f"undid {kind} on {lesson_id}",
        )["id"]
    stamp = _now(at)
    lesson.tombstones.append({"kind": kind, "at": stamp, "actor": actor,
                              "owner_write": owner_write, "cause": cause,
                              "tap": live[-1]["id"] if live else ""})
    lesson.refresh_status()
    lesson.save()
    return _sync_item(lesson, f"undo {kind} on {lesson_id} by {actor}: {cause}", stamp)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Knowledge frontier store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("undo-tap", help="undo a tap as the operator (operator test taps)")
    u.add_argument("lesson")
    u.add_argument("kind", choices=TAP_KINDS)
    u.add_argument("--cause", required=True, help="why; recorded on the tombstone and the move")
    args = ap.parse_args(argv)

    lockfile = p("meta") / "wiki.lock"
    try:
        with state_lock(lockfile, timeout_s=float(_cfg("vault_lock_wait_seconds"))):
            move = undo_tap(args.lesson, args.kind, "operator", args.cause)
    except TimeoutError as exc:
        print(f"frontier: {exc} (wiki.lock)", file=sys.stderr)
        return 2
    except (FrontierError, OSError) as exc:
        print(f"frontier: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"lesson": args.lesson, "kind": args.kind, "move": move}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
