#!/usr/bin/env python3
"""Frontier publisher (contract: docs/frontier-schema.md).

Copies Dreamer's knowledge map (goals, items, lessons, moves) to schema
`frontier` in the Postgres database named in postgres.env, for any display that
reads it. A read-only projection: it never writes to the vault.

Off unless DREAMER_PUBLISH_FRONTIER=1 is set in ~/.config/dreamer/postgres.env
(override: DREAMER_PG_ENV). The schema carries titles, summaries and lesson text
only. Evidence quotes, passages, web excerpts, the egress record, causes, check
reasons and counts never leave the laptop (CLAUDE.md rules 10, 13): the lesson
body is the `## Lesson` section alone, and evidence goes out by chat, never by quote.

The vault is read under wiki.lock with a short wait. When a job holds it the hour
is skipped without an event; `publish.frontier` freshness catches a long gap.

Usage:
    frontier_publish.py             # publish (always exits 0)
    frontier_publish.py --init      # (re)create schema objects, publish
    frontier_publish.py --show      # read frontier.v_snapshot and v_movement back
    frontier_publish.py --dry-run   # hash only; connects to nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, as_date, log, p, read_page, state_lock  # noqa: E402
import freshness_publish as P  # noqa: E402
import frontier as F  # noqa: E402
import frontier_lessons as FL  # noqa: E402
import goals as G  # noqa: E402
import heartbeat as HB  # noqa: E402

FLAG = "DREAMER_PUBLISH_FRONTIER"
TARGET = "publish_frontier"
JOB = "frontier-publish"

# A lesson in one of these states is not published, and never becomes an item's lesson.
HIDDEN_LESSON_STATUSES = ("dropped", "rejected")
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}--")

# Table -> columns, in insert order. The contract fixes these; do not add one.
TABLES = {
    "goal": ("id", "title", "status"),
    "item": ("id", "title", "summary", "quadrant", "last_evidence_on", "lesson_id",
             "lesson_status"),
    "item_goal": ("item_id", "goal_id", "relation"),
    "evidence": ("item_id", "position", "kind", "chat_title", "on_date"),
    "move": ("item_id", "at", "from_quadrant", "to_quadrant"),
    "lesson": ("id", "item_id", "goal_id", "title", "kind", "status", "body", "sources",
               "released_at", "aired_on", "got_it_at", "dig_deeper_at"),
}


def _cfg(key: str):
    return (CFG.get("frontier") or {})[key]


# --------------------------------------------------------------------------
# Collecting
# --------------------------------------------------------------------------

def _lesson_no(lesson_id: str) -> int:
    tail = lesson_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _chat(rel: str, fallback_date: str) -> tuple[str, str | None]:
    """(chat title, date) of a transcript: its frontmatter `title`, else the file
    name without date prefix and extension; its `date`, else the evidence's own."""
    stem = _DATE_PREFIX.sub("", rel.rsplit("/", 1)[-1])
    title, date = stem, None
    try:
        fm, _ = read_page(p("vault") / f"{rel}.md")
        title = str(fm.get("title") or "").strip() or stem
        date = as_date(fm.get("date"))
    except Exception as exc:  # noqa: BLE001 — a missing or bad transcript never aborts
        log(f"evidence chat unreadable ({type(exc).__name__})", job=JOB)
    return title, (date.isoformat() if date else (fallback_date or None))


def _evidence_rows(item: "F.Item") -> list[dict]:
    """One row per (chat, kind), newest chat first. The quote is never read here."""
    seen, found = set(), []
    for e in item.evidence:
        key = (e["transcript"], e["kind"])
        if key in seen or not e["transcript"]:
            continue
        seen.add(key)
        title, date = _chat(e["transcript"], e["date"])
        found.append((date or "", title, e["kind"]))
    found.sort(key=lambda t: t[0], reverse=True)   # stable: ties keep page order
    return [{"item_id": item.id, "position": i, "kind": kind, "chat_title": title,
             "on_date": date or None}
            for i, (date, title, kind) in enumerate(found, 1)]


def _tap_at(lesson: "F.Lesson", kind: str) -> str | None:
    live = lesson.live_taps(kind)
    return live[-1]["at"] if live else None


def _lesson_row(lesson: "F.Lesson", item: "F.Item") -> dict:
    # The `## Lesson` section only. Its headline is what the show presents, so a
    # display's "Got it" row names the lesson the owner heard; the item's title stands in
    # for a lesson that never got one.
    title, body = FL.lesson_parts(lesson)
    return {"id": lesson.id, "item_id": item.id, "goal_id": lesson.goal,
            "title": title or item.title,
            "kind": lesson.kind, "status": lesson.status, "body": body,
            "sources": FL.lesson_sources(lesson),
            "released_at": lesson.released_at or None, "aired_on": lesson.aired_on or None,
            "got_it_at": _tap_at(lesson, "got_it"),
            "dig_deeper_at": _tap_at(lesson, "dig_deeper")}


def collect() -> dict[str, list[dict]]:
    """{table: rows}, each sorted for a stable hash. Reads the vault; writes nothing."""
    out = {t: [] for t in TABLES}
    for g in G.load_goals():
        out["goal"].append({"id": g.id, "title": g.title, "status": g.status})
    lessons: dict[str, list["F.Lesson"]] = {}
    ldir = F.lessons_dir()
    for path in sorted(ldir.glob("K*.md")) if ldir.exists() else []:
        try:
            lesson = F.Lesson.from_path(path)
        except Exception as exc:  # noqa: BLE001 — one bad page never aborts
            log(f"skip {path.stem}: unreadable ({type(exc).__name__})", job=JOB)
            continue
        if lesson.status not in HIDDEN_LESSON_STATUSES:
            lessons.setdefault(lesson.item, []).append(lesson)
    idir = F.items_dir()
    for path in sorted(idir.glob("K*.md")) if idir.exists() else []:
        try:
            item = F.Item.from_path(path)
        except Exception as exc:  # noqa: BLE001
            log(f"skip {path.stem}: unreadable ({type(exc).__name__})", job=JOB)
            continue
        mine = sorted(lessons.get(item.id, []), key=lambda l: _lesson_no(l.id))
        newest = mine[-1] if mine else None
        dates = [e["date"] for e in item.evidence if e["date"]]
        out["item"].append({
            "id": item.id, "title": item.title, "summary": item.summary,
            "quadrant": item.quadrant, "last_evidence_on": max(dates) if dates else None,
            "lesson_id": newest.id if newest else None,
            "lesson_status": newest.status if newest else None})
        out["item_goal"] += [{"item_id": item.id, "goal_id": g["goal"], "relation": g["relation"]}
                             for g in item.goals]
        out["evidence"] += _evidence_rows(item)
        out["move"] += [{"item_id": item.id, "at": m["at"], "from_quadrant": m["from"],
                         "to_quadrant": m["to"]} for m in item.moves]
        out["lesson"] += [_lesson_row(l, item) for l in mine]
    return out


def content_hash(content: dict[str, list[dict]]) -> str:
    blob = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

def ddl() -> list[str]:
    """Idempotent and additive: nothing here drops or alters a live object."""
    return [
        "CREATE SCHEMA IF NOT EXISTS frontier",
        """CREATE TABLE IF NOT EXISTS frontier.snapshot (
             published_at timestamptz NOT NULL,
             content_hash text NOT NULL)""",
        # Exactly one row, without adding a column the contract does not have.
        "CREATE UNIQUE INDEX IF NOT EXISTS snapshot_one_row ON frontier.snapshot ((true))",
        """CREATE TABLE IF NOT EXISTS frontier.goal (
             id text PRIMARY KEY,
             title text NOT NULL,
             status text NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS frontier.item (
             id text PRIMARY KEY,
             title text NOT NULL,
             summary text NOT NULL,
             quadrant text NOT NULL,
             last_evidence_on date,
             lesson_id text,
             lesson_status text)""",
        """CREATE TABLE IF NOT EXISTS frontier.item_goal (
             item_id text NOT NULL,
             goal_id text NOT NULL,
             relation text NOT NULL,
             PRIMARY KEY (item_id, goal_id))""",
        """CREATE TABLE IF NOT EXISTS frontier.evidence (
             item_id text NOT NULL,
             position integer NOT NULL,
             kind text NOT NULL,
             chat_title text NOT NULL,
             on_date date)""",
        """CREATE TABLE IF NOT EXISTS frontier.move (
             item_id text NOT NULL,
             at timestamptz NOT NULL,
             from_quadrant text NOT NULL,
             to_quadrant text NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS frontier.lesson (
             id text PRIMARY KEY,
             item_id text NOT NULL,
             goal_id text NOT NULL,
             title text NOT NULL,
             kind text NOT NULL,
             status text NOT NULL,
             body text NOT NULL,
             sources jsonb NOT NULL DEFAULT '[]',
             released_at timestamptz,
             aired_on date,
             got_it_at timestamptz,
             dig_deeper_at timestamptz)""",
        """CREATE OR REPLACE VIEW frontier.v_snapshot AS
             SELECT published_at,
                    EXTRACT(EPOCH FROM (now() - published_at))::bigint AS snapshot_age_seconds
               FROM frontier.snapshot""",
        # One row per event. An undone tap is NULL in frontier.lesson, so it is absent here.
        """CREATE OR REPLACE VIEW frontier.v_movement AS
             SELECT 'released' AS kind, id AS lesson_id, title, released_at AS at
               FROM frontier.lesson WHERE released_at IS NOT NULL
             UNION ALL
             SELECT 'aired', id, title, aired_on::timestamp AT TIME ZONE 'UTC'
               FROM frontier.lesson WHERE aired_on IS NOT NULL
             UNION ALL
             SELECT 'got_it', id, title, got_it_at
               FROM frontier.lesson WHERE got_it_at IS NOT NULL
             UNION ALL
             SELECT 'dig_deeper', id, title, dig_deeper_at
               FROM frontier.lesson WHERE dig_deeper_at IS NOT NULL""",
        """CREATE OR REPLACE VIEW frontier.v_goal_items AS
             SELECT g.id AS goal_id, g.title AS goal_title, g.status AS goal_status,
                    i.id AS item_id, i.title AS item_title, i.summary, i.quadrant,
                    ig.relation, i.last_evidence_on, i.lesson_id, i.lesson_status
               FROM frontier.item_goal ig
               JOIN frontier.goal g ON g.id = ig.goal_id
               JOIN frontier.item i ON i.id = ig.item_id""",
    ]


def _insert(table: str) -> str:
    cols = TABLES[table]
    marks = ["%s::jsonb" if c == "sources" else "%s" for c in cols]
    return f"INSERT INTO frontier.{table} ({', '.join(cols)}) VALUES ({', '.join(marks)})"


# timestamptz columns. The vault keeps local wall-clock times without an offset;
# sent as text, Postgres would read them in the server's zone and shift them.
_LOCAL_TIMES = ("at", "released_at", "got_it_at", "dig_deeper_at")


def _param(column: str, value):
    if column == "sources":
        return json.dumps(value, ensure_ascii=False)
    if column in _LOCAL_TIMES:
        return P._aware(value)
    return value


def publish(conn, content: dict[str, list[dict]], init: bool = False) -> str:
    """One transaction: a reader sees the previous complete set or this one.
    Unchanged content moves only the timestamp, so a quiet hour still proves
    the laptop is alive without rewriting every row. `published_at` is
    Postgres's now(), the clock a display compares it against."""
    digest = content_hash(content)
    cur = conn.cursor()
    try:
        if init:
            for stmt in ddl():
                cur.execute(stmt)
        cur.execute("SELECT content_hash FROM frontier.snapshot")
        row = cur.fetchone()
        if row and row[0] == digest:
            cur.execute("UPDATE frontier.snapshot SET published_at = now()")
            conn.commit()
            return "unchanged"
        cur.execute("TRUNCATE " + ", ".join(f"frontier.{t}" for t in TABLES))
        for table, cols in TABLES.items():
            for r in content[table]:
                cur.execute(_insert(table), tuple(_param(c, r[c]) for c in cols))
        cur.execute("DELETE FROM frontier.snapshot")
        cur.execute("INSERT INTO frontier.snapshot (published_at, content_hash) "
                    "VALUES (now(), %s)", (digest,))
        conn.commit()
        return "published"
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def run(connect=None, meta=None, init: bool = False) -> str:
    """Publish once. Never raises; a failure is a class name, never a message
    (a message can quote row content or a hostname)."""
    env = P.load_env(P.env_path())
    if env is None:
        HB.record_publish(False, False, meta=meta, target=TARGET)
        return "not configured"
    if env.get(FLAG) != "1":
        HB.record_publish(False, False, meta=meta, target=TARGET)
        return "disabled"
    at = HB.stamp(HB.clock())
    try:
        try:
            with state_lock(p("meta") / "wiki.lock",
                            timeout_s=float(_cfg("publish_lock_wait_seconds"))):
                content = collect()
        except TimeoutError:
            # A job holds the vault: skip this hour. Not a failure, so no event and no
            # heartbeat; a long gap shows as stale on publish.frontier.
            return "skipped (busy)"
        conn = (connect or P._connect)(**P.conn_kwargs(env))
        try:
            result = publish(conn, content, init=init)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — a publish failure never fails a job
        HB.record_publish(True, False, error=type(exc).__name__, at=at,
                          meta=meta, target=TARGET)
        return f"failed ({type(exc).__name__})"
    HB.record_publish(True, True, at=at, meta=meta, target=TARGET)
    return result


def show(connect=None) -> list[tuple]:
    env = P.load_env(P.env_path())
    if env is None:
        raise FileNotFoundError(f"{P.env_path()} does not exist")
    conn = (connect or P._connect)(**P.conn_kwargs(env))
    try:
        cur = conn.cursor()
        cur.execute("SELECT 'snapshot', '', '', published_at, snapshot_age_seconds "
                    "FROM frontier.v_snapshot")
        rows = cur.fetchall()
        cur.execute("SELECT kind, lesson_id, left(title, 40), at, NULL "
                    "FROM frontier.v_movement ORDER BY at DESC LIMIT 10")
        return rows + cur.fetchall()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="hash the map; connect to nothing")
    ap.add_argument("--init", action="store_true",
                    help="(re)create the schema objects, then publish")
    ap.add_argument("--meta", default=None, help="heartbeat directory override (tests)")
    args = ap.parse_args(argv)
    when = HB.stamp(HB.clock())
    if args.dry_run:
        print(f"[{when}] frontier: dry run, hash {content_hash(collect())[:12]}")
        return 0
    if args.show:
        try:
            for row in show():
                print("  ".join("" if v is None else str(v) for v in row))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[{when}] frontier show: failed ({type(exc).__name__})")
            return 1
    # Hard deadline: whatever the network or the server does, this process
    # ends. The transaction rolls back and the previous set stays.
    signal.alarm(int(_cfg("publish_deadline_seconds")))
    result = run(meta=args.meta, init=args.init)
    signal.alarm(0)
    print(f"[{when}] frontier: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
