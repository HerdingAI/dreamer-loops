#!/usr/bin/env python3
"""Weekly digest assembly (§6.9 Job 2) — the delivery surface.

Ordering is decision-first (G5): the two or three items that most need an owner
decision sit at the top, because the owner's budget is ~10 minutes and §8's
named failure mode is content that gets skimmed.

The digest is also the feedback channel: ✓/✗ marks written into the "Matching
decisions sample" section are read back by the next run.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import random
import re
from collections import Counter
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, as_date, atomic_write, atomic_write_json, hours_since, log, p,
    read_json, today, update_run_state,
)
import vault as V  # noqa: E402

MARK_RE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX✓✗])\]\s*`(?P<key>[^`]+)`", re.M)


# --------------------------------------------------------------------------
# Freshness (§6.9) — an empty inbox is not self-evidently fine
# --------------------------------------------------------------------------

def newest_transcript_date() -> _dt.date | None:
    ledger = read_json(p("meta") / "ingested.json", default={}) or {}
    dates = [as_date(v.get("date")) for v in ledger.values() if isinstance(v, dict)]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def freshness_banner(ref: _dt.date) -> str | None:
    newest = newest_transcript_date()
    if newest is None:
        return ("**No transcripts have ever been ingested.** Drop a Claude export "
                "ZIP in `vault/inbox/` to start.")
    age = (ref - newest).days
    limit = int(CFG["freshness"]["stale_inbox_days"])
    if age > limit:
        return (f"**No new transcripts since {newest.isoformat()} ({age} days) — "
                f"export may be overdue.** Settings → Privacy → Export data, then "
                f"drop the ZIP in `vault/inbox/`.")
    return None


# --------------------------------------------------------------------------
# Spot-check marks (§6.6) — coverage is reported, never silently absent
# --------------------------------------------------------------------------

def ingest_marks(digest_path: Path) -> dict:
    """Read ✓/✗ marks the owner wrote into the previous digest."""
    if not digest_path or not digest_path.exists():
        return {"read": 0}
    text = digest_path.read_text(encoding="utf-8")
    feedback_path = p("meta") / "matching-feedback.json"
    feedback = read_json(feedback_path, default={}) or {}
    read = 0
    for m in MARK_RE.finditer(text):
        mark = m.group("mark").strip()
        if not mark:
            continue  # unmarked checkbox = no signal, by design
        key = m.group("key")
        feedback[key] = {"mark": "correct" if mark in "xX✓" else "wrong",
                         "from": digest_path.name}
        read += 1
    atomic_write_json(feedback_path, feedback)
    return {"read": read}


def rolling_precision() -> tuple[float | None, int, int]:
    """(precision, marked, window). None when there is not enough signal —
    which is reported as its own state, not as silence."""
    feedback = read_json(p("meta") / "matching-feedback.json", default={}) or {}
    window = int(CFG["matching"]["precision_window"])
    items = list(feedback.values())[-window:]
    if not items:
        return None, 0, window
    correct = sum(1 for v in items if v.get("mark") == "correct")
    return (correct / len(items)), len(items), window


def sample_decisions(n: int = 10) -> list[dict]:
    decisions = read_json(p("meta") / "matching-decisions.json", default=[]) or []
    feedback = read_json(p("meta") / "matching-feedback.json", default={}) or {}
    unmarked = [d for d in decisions if _key(d) not in feedback]
    rng = random.Random(today().toordinal())  # stable within a day, varies weekly
    return rng.sample(unmarked, min(n, len(unmarked)))


def _key(d: dict) -> str:
    return f"{d.get('loop_id','?')}|{d.get('date','?')}|{d.get('decision','?')}"


# --------------------------------------------------------------------------
# Digest rendering
# --------------------------------------------------------------------------

class _Sections:
    """Collects sections and drops the empty ones.

    The spec's G5 target is a <=10-minute, decision-first read. Rendering
    'Decisions awaiting you' as an empty heading at the top defeats exactly
    that: the reader's first content is a non-event. Empty sections are omitted
    and named once in a footer, so nothing is silently missing.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.empty: list[str] = []

    def add(self, heading: str, lines: list[str]) -> None:
        if lines:
            self.parts.append(f"## {heading}")
            self.parts.append("")
            self.parts.extend(lines)
            self.parts.append("")
        else:
            self.empty.append(heading)

    def render(self) -> list[str]:
        out = list(self.parts)
        if self.empty:
            out.append("---")
            out.append("")
            out.append("_Nothing to report under: " + ", ".join(self.empty) + "._")
            out.append("")
        return out


def read_events() -> list[dict]:
    """Run-level events recorded by the job wrappers (deferrals, recoveries)."""
    state = read_json(p("meta") / "run-state.json", default={}) or {}
    return list(state.get("events") or [])


def clear_events() -> None:
    # Locked read-merge-replace (dreamer_common.update_run_state): an unlocked
    # write here could clobber a cost/health record landing concurrently.
    path = p("meta") / "run-state.json"

    def mutate(state: dict) -> None:
        if state.get("events"):
            state["events"] = []

    update_run_state(path, mutate)


def health_lines() -> list[str]:
    """One-line health summary from run-state's `health` key (healthcheck.py).

    A missing record renders as "never checked" rather than nothing or a
    crash — an unmonitored system must not look like a healthy one. A record
    older than health.checked_max_age_hours gets an explicit staleness
    warning: the checker's own liveness is part of what is being reported.
    """
    state = read_json(p("meta") / "run-state.json", default={}) or {}
    health = state.get("health") or {}
    checked_at = health.get("checked_at")
    if not checked_at:
        return ["> [!warning] Health",
                "> health: never checked — `scripts/healthcheck.py` has not "
                "recorded a run."]

    results = health.get("assertions") or []
    ok_n = sum(1 for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok")]
    deg = sum(1 for r in failed if r.get("severity") == "degraded")
    blk = sum(1 for r in failed if r.get("severity") == "blocking")
    blocked = health.get("blocked_legs") or []

    summary = (f"> health: {ok_n} ok, {deg} degraded, {blk} blocking of "
               f"{len(results)} assertion(s), checked {checked_at}")
    if blocked:
        summary += " — blocked legs: " + ", ".join(blocked)

    max_age = float(CFG["health"]["checked_max_age_hours"])
    stale = None
    age_h = hours_since(checked_at)
    if age_h is None:
        stale = (f"> **health not checked since a parseable time** "
                 f"(checked_at={checked_at!r}) — treat the record as stale.")
    elif age_h > max_age:
        stale = (f"> **health not checked since {checked_at}** "
                 f"({age_h:.0f}h ago, window {max_age:g}h) — the checker "
                 f"itself may be down.")

    kind = "warning" if (stale or blocked or deg or blk) else "info"
    lines = [f"> [!{kind}] Health", summary]
    if stale:
        lines.append(stale)
    return lines


def build(ref: _dt.date | None = None, *, run_stats: dict | None = None,
          quiet_reason: str | None = None) -> Path:
    ref = ref or today()
    run_stats = run_stats or {}
    year, week, _ = ref.isocalendar()
    dest = p("digests") / f"{year}-{week:02d}.md"

    loops = V.load_loops()
    pending = _read_pending()

    out: list[str] = []
    out.append(f"# Dreamer digest — {year} week {week:02d}")
    out.append("")
    out.append(f"_Generated {ref.isoformat()}. Opening this file in Obsidian does "
               f"not count as reading it (owner decision Q16) — mark something, or "
               f"fetch it via `get_latest_digest`._")
    out.append("")

    banner = freshness_banner(ref)
    if banner:
        out += ["> [!warning] Freshness", f"> {banner}", ""]

    # Health sits with freshness: both answer "can this digest be trusted to
    # describe a system that is actually running?".
    out += health_lines() + [""]

    if quiet_reason:
        out += ["> [!info] Quiet week", f"> {quiet_reason}", ""]

    # Run-level events: a deferred night or a recovered loop means the system
    # did not do what a quiet digest would otherwise imply. This sits directly
    # under freshness because it is the same class of information.
    events = read_events()
    if events:
        out += ["> [!warning] What happened this week"]
        for e in events:
            out.append(f"> - **{e.get('kind', 'event')}** "
                       f"({e.get('at', '')[:16]}): {e.get('detail', '')}")
        out.append("")

    S = _Sections()

    # ---- decision-first ------------------------------------------------
    lines = []
    for l in [x for x in loops if x.status == "decision-only"]:
        lines.append(f"- **{l.title}** — `{l.id}`, seen {l.recurrence_count}× "
                     f"(last {l.last_seen}). No research will resolve this.")
        note = _decision_note(l)
        if note:
            lines.append(f"  - {note}")
    S.add("Decisions awaiting you", lines)

    merges = _read_merge_proposals()
    lines = []
    if merges:
        lines.append("The matcher split these conservatively. Confirm to merge; "
                     "unconfirmed proposals expire and are re-proposed once.")
        lines.append("")
        for m in merges:
            lines.append(f"- [ ] `merge:{m['keep']}+{m['retire']}` — "
                         f"**{m['keep_title']}** vs **{m['retire_title']}**")
            if m.get("reason"):
                lines.append(f"  - {m['reason']}")
    S.add("Merge proposals", lines)

    # ---- what the system did -------------------------------------------
    # Quality scores, keyed by loop, so a conclusion carries its own grade
    # rather than making the owner open it to find out whether it decided
    # anything. Structural score only — it says "worth your ten minutes",
    # never "correct".
    quality = {q.get("loop"): q for q in pending.get("quality", [])
               if isinstance(q, dict)}
    lines = []
    for c in pending.get("conclusions", []):
        q = quality.get(c.get("loop"))
        mark = ""
        if q:
            score = float(q.get("score") or 0)
            mark = f", quality: {score:.0%}" + ("  ⚠️" if score < 0.7 else "")
        lines.append(f"- [[{c['path']}]] — **{c['title']}** "
                     f"(route: `{c.get('route','?')}`, "
                     f"confidence: {c.get('confidence','?')}{mark})")
        if q and float(q.get("score") or 0) < 0.7 and q.get("failed"):
            lines.append(f"  - weak on: {q['failed']} — read before trusting")
    S.add("New conclusions", lines)

    # Rule 14: a concluded loop that resurfaced but was judged settled is
    # SERVED, not re-researched. Rendering the serve keeps that decision
    # auditable — a quiet gate is indistinguishable from a broken one.
    lines = []
    for sv in pending.get("served", []):
        lines.append(f"- `{sv.get('loop','?')}` **{sv.get('title','?')}** — "
                     f"resurfaced (recurrence {sv.get('recurrence','?')}), "
                     f"existing conclusion served: [[{sv.get('conclusion','?')}]]")
        if sv.get("reason"):
            lines.append(f"  - _{sv['reason']}_")
    S.add("Conclusions served (no re-research)", lines)

    # Rule 13: derived citations survive only quarantined and downgraded, but
    # their presence still means the model reached for its own prior output —
    # worth a glance, and a rising count is the echo chamber reasserting itself.
    lines = []
    for dc in pending.get("derived_citations", []):
        lines.append(f"- [[{dc.get('path','?')}]] (`{dc.get('loop','?')}`) — "
                     f"{dc.get('count','?')} derived citation(s), quarantined "
                     f"and capped at contested")
    S.add("Derived-citation report", lines)

    growing = sorted([l for l in loops if l.status == "open"
                      and l.recurrence_count >= 2],
                     key=lambda l: l.recency_weighted_score(ref), reverse=True)[:10]
    lines = []
    if growing:
        lines.append("| loop | title | count | weighted | last seen |")
        lines.append("|---|---|---|---|---|")
        for l in growing:
            lines.append(f"| `{l.id}` | {l.title.replace('|', chr(92) + '|')} | "
                         f"{l.recurrence_count} | "
                         f"{l.recency_weighted_score(ref):.2f} | {l.last_seen} |")
    S.add("Growing loops", lines)

    soon = []
    for l in loops:
        deadline = l.decay_deadline()
        if deadline and 0 <= (deadline - ref).days <= 14:
            soon.append((deadline, l))
    lines = []
    if soon:
        lines.append("Say the word to revive any of these.")
        lines.append("")
        for deadline, l in sorted(soon):
            lines.append(f"- `{l.id}` **{l.title}** — archives "
                         f"{deadline.isoformat()} ({(deadline - ref).days}d)")
    S.add("Archiving soon", lines)

    archived = pending.get("archived", [])
    lines = []
    if archived:
        lines.append("Say the word to revive any of these.")
        lines.append("")
        lines += [f"- `{a['id']}` {a['title']}" for a in archived]
    S.add("Archived this week", lines)

    # ---- feedback loop --------------------------------------------------
    precision, marked, window = rolling_precision()
    lines = []
    if precision is None:
        lines.append(f"**Matching precision: insufficient data — 0 of {window} "
                     f"decisions marked.** No mark is no signal, which is fine; "
                     f"this line exists so an empty feedback loop is visible "
                     f"rather than silent.")
    else:
        flag = "" if precision >= float(CFG["matching"]["precision_floor"]) else \
            "  ⚠️ **below floor — tuning flag raised**"
        lines.append(f"**Matching precision: {precision:.0%}** over the last "
                     f"{marked} of {window} marked decisions.{flag}")
    sample = sample_decisions(10)
    if sample:
        lines.append("")
        lines.append("Tick the box if the decision was right; leave it blank to "
                     "say nothing. Change `[ ]` to `[x]` for correct, `[✗]` for "
                     "wrong.")
        lines.append("")
        for d in sample:
            verb = ("created new loop" if d["decision"] == "new"
                    else f"matched to {d['loop_id']}")
            lines.append(f"- [ ] `{_key(d)}` — {verb}: **{d['title']}**")
            if d.get("justification"):
                lines.append(f"  - _{d['justification']}_")
    S.add("Matching decisions sample", lines)

    S.add("Proposed tags",
          [f"- `{t}`" for t in pending.get("proposed_tags", [])])

    queries = pending.get("web_queries", [])
    lines = []
    if queries:
        lines.append("Everything that left the machine this week.")
        lines.append("")
        lines += [f"- `{q}`" for q in queries]
    S.add("Web queries sent", lines)

    stats = {
        "active loops": len(loops),
        "open": sum(1 for l in loops if l.status == "open"),
        "paused": sum(1 for l in loops if l.status == "paused"),
        "decision-only": sum(1 for l in loops if l.status == "decision-only"),
        "archived (total)": len(list(p("archive").glob("*.md"))),
    }
    # Per-route counts (§6.5): the guard against `decision-only` becoming a
    # low-effort escape hatch, and against `wisdom` producing nothing.
    routes = Counter(l.route for l in loops if l.route)
    if routes:
        stats["routes"] = ", ".join(f"{k}={v}" for k, v in sorted(routes.items()))
    stats.update(run_stats)
    S.add("Run stats", [f"- {k}: {v}" for k, v in stats.items()])

    out += S.render()

    atomic_write(dest, "\n".join(out) + "\n")
    _clear_pending()
    clear_events()
    return dest


def _decision_note(loop: V.Loop) -> str:
    m = re.search(r"##\s*Decision framing\s*\n+(.+?)(\n##|\Z)", loop.body, re.DOTALL)
    return m.group(1).strip().splitlines()[0] if m else ""


# --------------------------------------------------------------------------
# pending.md — staging shared by Job 1 and Job 3 (§6.9)
# --------------------------------------------------------------------------

def _pending_path() -> Path:
    return p("digests") / "pending.json"


def stage(kind: str, item) -> None:
    path = _pending_path()
    data = read_json(path, default={}) or {}
    data.setdefault(kind, []).append(item)
    atomic_write_json(path, data)


def _read_pending() -> dict:
    return read_json(_pending_path(), default={}) or {}


def _clear_pending() -> None:
    path = _pending_path()
    if path.exists():
        path.unlink()


def _read_merge_proposals() -> list[dict]:
    return read_json(p("meta") / "merge-proposals.json", default=[]) or []


def latest_digest() -> Path | None:
    files = sorted(f for f in p("digests").glob("*.md")
                   if re.match(r"^\d{4}-\d{2}\.md$", f.name))
    return files[-1] if files else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["build", "ingest-marks", "precision"])
    ap.add_argument("--quiet-reason", default=None)
    args = ap.parse_args()
    if args.command == "build":
        print(build(quiet_reason=args.quiet_reason))
    elif args.command == "ingest-marks":
        print(json.dumps(ingest_marks(latest_digest())))
    else:
        prec, marked, window = rolling_precision()
        print(json.dumps({"precision": prec, "marked": marked, "window": window}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
