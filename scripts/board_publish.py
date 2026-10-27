#!/usr/bin/env python3
"""Board publisher.

Copies Dreamer's CURRENT problem -> solution pairs to schema `board` in the
Postgres database named in postgres.env, for a display to list and open.
A read-only projection: it never writes to the vault.

Off unless DREAMER_PUBLISH_BOARD=1 is set in ~/.config/dreamer/postgres.env
(override: DREAMER_PG_ENV). The `health` schema stays metadata only; content
leaves the laptop only through here, and only when the owner opts in.

Only an allow-list of conclusion sections is published. Web excerpts and the
egress record never leave the laptop (CLAUDE.md rule 10).

Usage:
    board_publish.py             # publish (always exits 0)
    board_publish.py --init      # (re)create schema objects, publish
    board_publish.py --show      # read board.v_pairs back
    board_publish.py --dry-run   # count and hash; connects to nothing
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

from dreamer_common import as_date, log, p, read_page  # noqa: E402
import freshness as F  # noqa: E402
import freshness_publish as P  # noqa: E402
import heartbeat as HB  # noqa: E402
import vault as V  # noqa: E402

FLAG = "DREAMER_PUBLISH_BOARD"
TARGET = "publish_board"
JOB = "board-publish"

# Heading -> column. A heading not listed here is never published; a new
# heading in a conclusion page stays private until someone adds it.
SECTIONS = {"The loop, restated": "restated", "Synthesis": "synthesis",
            "Open sub-questions": "open_questions"}
LEDGER = "Evidence ledger"
# Loop statuses whose answer leaves the board. `resolved` is the owner saying
# the problem is done with ("no longer relevant"), so showing its solution is
# noise. `archived` stays: a loop that went quiet on its own still has an
# answer worth seeing. A removal is deliberate, so it is not counted as a skip.
HIDDEN_LOOP_STATUSES = ("resolved",)
GRADES = {"accepted": "ev_accepted", "provisional": "ev_provisional",
          "contested": "ev_contested", "unsupported": "ev_unsupported"}
_GRADE_RE = re.compile(r"\b(accepted|provisional|contested|unsupported):\s*(\d+)")
TRUNCATED = "\n\n[truncated for the board]"
# A fence line: three or more backticks or tildes, then the rest of the line.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")

# The order apply_conclusion.render writes a page's sections in. A page whose
# known headings are repeated or out of this order was not written by it —
# or carries a heading planted by text it embedded — and is not published.
WEB = "Web sources (untrusted)"
ORDER = ("The loop, restated", "What the library says",
         "What you previously concluded", WEB,
         "Prior conclusions (derived — hypothesis, not evidence)", LEDGER,
         "Synthesis", "Open sub-questions", "Decision framing", "Egress record")

COLUMNS = ("id", "loop_id", "problem", "answer", "concluded_on", "confidence",
           "route", "raised", "loop_status", "tags", "ev_accepted",
           "ev_provisional", "ev_contested", "ev_unsupported", "restated",
           "synthesis", "open_questions")


def _iter_sections(body: str):
    """(heading, text) for each `## Heading`, in page order. A heading inside
    a fenced block is content, not a section. Fences follow CommonMark: a
    closing fence uses the opening character, is at least as long, and has
    nothing after it — so a shorter or annotated fence line inside a block
    cannot end it early. An unclosed fence swallows the rest of the page."""
    name, buf, fence = None, [], None
    for line in body.splitlines():
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
            elif line.startswith("## "):
                if name is not None:
                    yield name, "\n".join(buf).strip()
                name, buf = line[3:].strip(), []
                continue
        elif (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
              and not m.group(2).strip()):
            fence = None
        if name is not None:
            buf.append(line)
    if name is not None:
        yield name, "\n".join(buf).strip()


def split_sections(body: str) -> dict[str, str]:
    """`## Heading` -> its text; a repeated heading keeps its first body.
    Says nothing about whether the page can be trusted: see page_sections."""
    out: dict[str, str] = {}
    for name, text in _iter_sections(body):
        out.setdefault(name, text)
    return out


def page_sections(body: str) -> dict[str, str] | None:
    """The page's sections, or None when its structure cannot be trusted.

    Real pages do not fence their web section (apply_conclusion.render writes
    web claims as plain bullets), so a claim that carried a newline and a
    `## Synthesis` line would plant a section the publisher then sent as the
    answer. Rather than guess which occurrence is real, refuse the page when:
    a known heading appears twice; known headings are not in the order the
    writer uses; or there is a web section with no evidence ledger after it
    (the writer always emits a ledger when it emits claims, so a forged
    section cannot hide in a page that lacks one)."""
    pairs = list(_iter_sections(body))
    known = [ORDER.index(n) for n, _ in pairs if n in ORDER]
    if len(set(known)) != len(known) or known != sorted(known):
        return None
    names = {n for n, _ in pairs}
    if WEB in names and LEDGER not in names:
        return None
    return dict(pairs)


def parse_ledger(text: str | None) -> dict[str, int | None]:
    """Counts from the ledger's FIRST non-empty line. A grade absent from that
    line is 0; no ledger, or no count on that line, is all NULL (unknown)."""
    empty = {col: None for col in GRADES.values()}
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    found = _GRADE_RE.findall(first)
    if not found:
        return empty
    out = {col: 0 for col in GRADES.values()}
    for grade, n in found:
        out[GRADES[grade]] = int(n)
    return out


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + TRUNCATED


def _row(stem: str, fm: dict, body: str, loop, limit: int) -> dict | None:
    problem, answer = (loop.title or "").strip(), str(fm.get("title") or "").strip()
    if not problem or not answer:
        return None
    sections = page_sections(body)
    # No trusted structure, or no answer text: nothing a display should show.
    if sections is None or not sections.get("Synthesis"):
        return None
    row = {"id": stem, "loop_id": loop.id, "problem": problem, "answer": answer,
           "concluded_on": as_date(fm.get("created")),
           "confidence": str(fm["confidence"]) if fm.get("confidence") else None,
           "route": str(fm["route"]) if fm.get("route") else None,
           "raised": int(loop.recurrence_count or 0),
           "loop_status": loop.status or None,
           "tags": [str(t) for t in (loop.tags or [])]}
    row.update(parse_ledger(sections.get(LEDGER)))
    for heading, column in SECTIONS.items():
        row[column] = _clip(sections.get(heading), limit)
    return row


def collect() -> tuple[list[dict], int]:
    """(rows sorted by id, skipped count). Reads the vault; writes nothing.
    Skips are logged by file stem only — never a title."""
    limit = int(F.cfg("board", "section_max_chars"))
    loops = {l.id: l for l in V.load_loops(include_archived=True)}
    base = p("conclusions")
    best: dict[str, dict] = {}
    skipped = 0
    for f in sorted(base.glob("*.md")) if base.exists() else []:
        try:
            fm, body = read_page(f)
        except Exception as exc:  # noqa: BLE001 — one bad page never aborts
            skipped += 1
            log(f"skip {f.stem}: unreadable ({type(exc).__name__})", job=JOB)
            continue
        if fm.get("superseded_by"):
            continue
        loop = loops.get(str(fm.get("loop") or ""))
        if loop is not None and loop.status in HIDDEN_LOOP_STATUSES:
            continue
        row = _row(f.stem, fm, body, loop, limit) if loop is not None else None
        if row is None:
            skipped += 1
            log(f"skip {f.stem}: no loop page, no title, no synthesis, or "
                f"untrusted page structure", job=JOB)
            continue
        prev = best.get(loop.id)
        if prev is not None:
            log(f"loop {loop.id} has two current conclusions; keeping the later", job=JOB)
        if prev is None or _order(row) > _order(prev):
            best[loop.id] = row
    return sorted(best.values(), key=lambda r: r["id"]), skipped


def _order(row: dict) -> tuple:
    return (row["concluded_on"] is not None, row["concluded_on"], row["id"])


def content_hash(rows: list[dict]) -> str:
    blob = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ddl() -> list[str]:
    return [
        "CREATE SCHEMA IF NOT EXISTS board",
        """CREATE TABLE IF NOT EXISTS board.snapshot (
             id smallint PRIMARY KEY CHECK (id = 1),
             published_at timestamptz NOT NULL,
             content_hash text NOT NULL,
             total integer NOT NULL,
             skipped integer NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS board.pair (
             id text PRIMARY KEY,
             loop_id text NOT NULL,
             problem text NOT NULL,
             answer text NOT NULL,
             concluded_on date,
             confidence text,
             route text,
             raised integer NOT NULL,
             loop_status text,
             tags text[] NOT NULL DEFAULT '{}',
             ev_accepted integer,
             ev_provisional integer,
             ev_contested integer,
             ev_unsupported integer,
             restated text,
             synthesis text,
             open_questions text)""",
        # The list query: no body column, so a display's poll stays small.
        """CREATE OR REPLACE VIEW board.v_pairs AS
             SELECT p.id, p.loop_id, p.problem, p.answer, p.concluded_on,
                    p.confidence, p.route, p.raised, p.loop_status, p.tags,
                    p.ev_accepted, p.ev_provisional, p.ev_contested, p.ev_unsupported,
                    s.published_at, s.total,
                    EXTRACT(EPOCH FROM (now() - s.published_at))::bigint
                      AS snapshot_age_seconds
               FROM board.pair p CROSS JOIN board.snapshot s""",
    ]


_INSERT_PAIR = (f"INSERT INTO board.pair ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join(['%s'] * len(COLUMNS))})")


def publish(conn, rows: list[dict], skipped: int, init: bool = False) -> str:
    """One transaction: a reader sees the previous complete set or this one.
    Unchanged content moves only the timestamp, so a quiet hour still proves
    the laptop is alive without rewriting every row. `published_at` is
    Postgres's now(), the clock a display compares it against."""
    digest = content_hash(rows)
    cur = conn.cursor()
    try:
        if init:
            for stmt in ddl():
                cur.execute(stmt)
        cur.execute("SELECT content_hash FROM board.snapshot WHERE id = 1")
        row = cur.fetchone()
        if row and row[0] == digest:
            cur.execute("UPDATE board.snapshot SET published_at = now(), skipped = %s "
                        "WHERE id = 1", (skipped,))
            conn.commit()
            return "unchanged"
        cur.execute("DELETE FROM board.pair")
        for r in rows:
            cur.execute(_INSERT_PAIR, tuple(r[c] for c in COLUMNS))
        cur.execute(
            """INSERT INTO board.snapshot (id, published_at, content_hash, total, skipped)
               VALUES (1, now(), %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                 published_at = EXCLUDED.published_at,
                 content_hash = EXCLUDED.content_hash,
                 total = EXCLUDED.total, skipped = EXCLUDED.skipped""",
            (digest, len(rows), skipped))
        conn.commit()
        return "published"
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


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
        rows, skipped = collect()
        conn = (connect or P._connect)(**P.conn_kwargs(env))
        try:
            result = publish(conn, rows, skipped, init=init)
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
        cur.execute("SELECT id, concluded_on, confidence, raised, left(answer, 60), "
                    "total, snapshot_age_seconds FROM board.v_pairs "
                    "ORDER BY concluded_on DESC NULLS LAST, id DESC")
        return cur.fetchall()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="count and hash the pairs; connect to nothing")
    ap.add_argument("--init", action="store_true",
                    help="(re)create the schema objects, then publish")
    ap.add_argument("--meta", default=None, help="heartbeat directory override (tests)")
    args = ap.parse_args(argv)
    when = HB.stamp(HB.clock())
    if args.dry_run:
        rows, skipped = collect()
        print(f"[{when}] board: dry run, {len(rows)} pairs, {skipped} skipped, "
              f"hash {content_hash(rows)[:12]}")
        return 0
    if args.show:
        try:
            for row in show():
                print("  ".join("" if v is None else str(v) for v in row))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[{when}] board show: failed ({type(exc).__name__})")
            return 1
    # Hard deadline: whatever the network or the server does, this process
    # ends. The transaction rolls back and the previous set stays.
    signal.alarm(int(F.cfg("board", "publish_deadline_seconds")))
    result = run(meta=args.meta, init=args.init)
    signal.alarm(0)
    print(f"[{when}] board: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
