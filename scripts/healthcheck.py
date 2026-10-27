#!/usr/bin/env python3
"""Health spine — evaluates registered assertions, records the result.

Writes `{checked_at, blocked_legs, assertions: [{name, ok, severity, blocks,
detail}]}` under the `health` key of vault/.vault-meta/run-state.json, holding
the same advisory lock the bash jobs hold (bin/_common.sh acquire_lock) around
a read-merge-replace so a concurrently recorded cost or event is never dropped.

Severity contract:
- info      — recorded in the health record only, never events.
- degraded  — a failure also appends a `degraded` event (same channel the job
              wrappers use, so it reaches the next digest).
- blocking  — a failure additionally lands its `blocks` legs in
              `blocked_legs`; jobs consult that via `leg_blocked` in
              bin/_common.sh and defer cleanly.

Failure semantics: every assertion runs inside try/except — an assertion whose
data source is unreachable reports ok=false at its declared severity with the
error string as detail, and never suppresses the assertions after it. The
exit code is 0 unless the health WRITE itself failed; callers treat non-zero
as degraded, never as a reason to abort the cycle.

The launch set (registered at the bottom of this file): embeddings-current,
index-fresh, collections-covered, vocabulary-applied, cost-records-sane,
gates-current — plus fold-queue-current (rule-15 fold-queue hygiene, the R24
growth discipline). Every assertion is a pure function over readable state — no LLM
calls, no mutations; the spine observes, never mutates. qmd is consulted via a
single read-only `qmd status` subprocess call per evaluation.

Usage:
    python3 scripts/healthcheck.py            # evaluate + record
    python3 scripts/healthcheck.py --json     # also print the record
    python3 scripts/healthcheck.py --watchdog # staleness-only check (cron,
                                              # hourly) — runs NO assertions
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (CFG, as_date, atomic_write_json, hours_since,  # noqa: E402
                            log, p, read_json, read_json_strict, read_page,
                            state_lock, today)
import goals as G  # noqa: E402
import questions as Q  # noqa: E402
import vault as V  # noqa: E402
import freshness as FR  # noqa: E402
import frontier as FRN  # noqa: E402
import frontier_gaps as FRG  # noqa: E402
import frontier_lessons as FRL  # noqa: E402
import frontier_map as FRM  # noqa: E402
import spine as SP  # noqa: E402

SEVERITIES = ("info", "degraded", "blocking")

# Registry rows: (name, severity, blocks, callable). The callable returns
# True / False or (ok, detail). The launch set registers at the bottom of
# this file.
Assertion = tuple[str, str, tuple[str, ...], Callable]
ASSERTIONS: list[Assertion] = []


def register(name: str, severity: str, blocks: Sequence[str],
             fn: Callable) -> None:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r} for assertion {name!r}"
                         f" (want one of {SEVERITIES})")
    ASSERTIONS.append((name, severity, tuple(blocks), fn))


def health_cfg(key: str):
    """Required config, loud on absence — a soft default here would let the
    watchdog silently judge staleness against a number nobody chose."""
    block = CFG.get("health")
    if not isinstance(block, dict) or key not in block:
        raise SystemExit(f"config.yaml is missing required key health.{key} — "
                         f"refusing to guess a default")
    return block[key]


def thread_cfg(key: str):
    """Required config under `thread:`, loud on absence — same contract as
    health_cfg: a soft default would let the fold-queue assertion judge
    against numbers nobody chose."""
    block = CFG.get("thread")
    if not isinstance(block, dict) or key not in block:
        raise SystemExit(f"config.yaml is missing required key thread.{key} — "
                         f"refusing to guess a default")
    return block[key]


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Vault lock — mirror of bin/_common.sh acquire_lock
# --------------------------------------------------------------------------

@contextlib.contextmanager
def vault_lock(timeout_s: float = 6.0, poll_s: float = 0.2):
    """flock on the same wiki.lock the bash jobs hold for their whole run.

    Non-blocking with a short retry, per the jobs' own convention (delegates
    to dreamer_common.state_lock, the shared implementation the bash record_*
    helpers use). NOTE: flock is per open-file-description, so a child of a
    job that already holds wiki.lock must not re-flock it — the wrappers
    invoke healthcheck BEFORE acquire_lock, and state_lock additionally skips
    its own flock when DREAMER_LOCK_HELD=1 (exported by acquire_lock), so a
    lock-holding parent can never deadlock a child healthcheck either way.
    """
    with state_lock(p("meta") / "wiki.lock", timeout_s=timeout_s,
                    poll_s=poll_s):
        yield


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def evaluate(registry: Iterable[Assertion] | None = None) -> list[dict]:
    _QMD_CACHE.clear()  # one fresh `qmd status` per evaluation, however many
    # assertions read it.
    _STATE_CACHE.clear()  # same contract for the run-state read.
    results: list[dict] = []
    for name, severity, blocks, fn in (ASSERTIONS if registry is None
                                       else registry):
        try:
            out = fn()
            if isinstance(out, tuple):
                ok, detail = bool(out[0]), str(out[1])
            else:
                ok, detail = bool(out), ""
        except Exception as exc:  # noqa: BLE001 — one broken assertion must
            # report as failed, never take the rest of the checks down with it.
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"name": name, "ok": ok, "severity": severity,
                        "blocks": list(blocks), "detail": detail})
    return results


def build_record(results: list[dict]) -> dict:
    blocked = sorted({leg for r in results
                      if not r["ok"] and r["severity"] == "blocking"
                      for leg in r["blocks"]})
    return {"checked_at": _now(), "blocked_legs": blocked,
            "assertions": results}


def _events_for(results: list[dict]) -> list[dict]:
    job = os.environ.get("JOB", "healthcheck")
    events = []
    for r in results:
        if r["ok"] or r["severity"] == "info":
            continue
        detail = f"health: assertion '{r['name']}' failed ({r['severity']})"
        if r["detail"]:
            detail += f": {r['detail']}"
        if r["severity"] == "blocking" and r["blocks"]:
            detail += " — blocked leg(s): " + ", ".join(r["blocks"])
        events.append({"at": _now(), "job": job, "kind": "degraded",
                       "detail": detail})
    return events


def write_health(record: dict, events: Iterable[dict] = ()) -> None:
    """Locked read-merge-replace. The state is read INSIDE the locked section,
    so a cost/event recorded by a job that released the lock a moment earlier
    is re-read and preserved rather than clobbered by a stale snapshot.

    Corrupt state refuses loudly: an existing-but-unparseable run-state.json
    raises (read_json inside would default to {} and the atomic replace would
    wipe every cost/event/deferral) — main() turns that into a non-zero exit.

    Events dedup against the pending list (job+kind+detail), mirroring
    watchdog(): healthcheck runs several times a night but the digest that
    drains events is weekly, so a persistently failing assertion would
    otherwise stack dozens of identical lines into one digest."""
    path = p("meta") / "run-state.json"
    with vault_lock():
        state = read_json_strict(path, default={}) or {}
        state["health"] = record
        seen = {(e.get("job"), e.get("kind"), e.get("detail"))
                for e in state.get("events") or []}
        for ev in events:
            key = (ev.get("job"), ev.get("kind"), ev.get("detail"))
            if key in seen:
                continue
            seen.add(key)
            state.setdefault("events", []).append(ev)
        atomic_write_json(path, state)


def run(registry: Iterable[Assertion] | None = None) -> dict:
    results = evaluate(registry)
    record = build_record(results)
    write_health(record, _events_for(results))
    return record


# --------------------------------------------------------------------------
# Watchdog — independent, cheap, assertion-free
# --------------------------------------------------------------------------

def _watchdog_log(line: str) -> None:
    logdir = p("logs")
    logdir.mkdir(parents=True, exist_ok=True)
    with (logdir / "watchdog.log").open("a", encoding="utf-8") as fh:
        fh.write(f"[{_now()}] {line}\n")


def watchdog() -> int:
    """Only checks health.checked_at staleness against
    health.checked_max_age_hours. Never runs the assertion registry — the
    point is an observer that stays alive when the pipeline it watches dies."""
    max_age_h = float(health_cfg("checked_max_age_hours"))
    path = p("meta") / "run-state.json"
    state = read_json(path, default={}) or {}
    checked_at = (state.get("health") or {}).get("checked_at")

    stale_reason = None
    if not checked_at:
        stale_reason = "health has never been checked"
    else:
        age_h = hours_since(checked_at)
        if age_h is None:
            stale_reason = f"health.checked_at is unparseable: {checked_at!r}"
        elif age_h > max_age_h:
            stale_reason = (f"health last checked {checked_at} "
                            f"({age_h:.1f}h ago, window {max_age_h:g}h)")
    if stale_reason is None:
        return 0

    detail = f"watchdog: {stale_reason} — the healthcheck itself may be down"
    # Out-of-band log first: it must exist even if the run-state write fails.
    _watchdog_log(detail)
    # Event channel, deduped: the cron entry is hourly and the digest that
    # clears events is weekly, so an un-deduped watchdog would stack dozens of
    # identical lines into one digest.
    pending = state.get("events") or []
    if any(e.get("kind") == "degraded"
           and str(e.get("detail", "")).startswith("watchdog:")
           for e in pending):
        return 0
    try:
        with vault_lock():
            state = read_json(path, default={}) or {}
            state.setdefault("events", []).append(
                {"at": _now(), "job": os.environ.get("JOB", "watchdog"),
                 "kind": "degraded", "detail": detail})
            atomic_write_json(path, state)
    except Exception as exc:  # noqa: BLE001 — the log line above already fired
        _watchdog_log(f"watchdog: could not append the degraded event: {exc}")
        return 1
    return 0


# --------------------------------------------------------------------------
# qmd status (read-only) and its parsers
# --------------------------------------------------------------------------
# The parsers are pure functions over the command's text so tests can feed
# them captured output. NEVER judge index freshness from the index file's
# mtime — SQLite WAL keeps index.sqlite looking perpetually stale.

QMD_TIMEOUT_S = 20
_QMD_CACHE: dict[str, str] = {}

# Run-state snapshot, cached per evaluation like _QMD_CACHE (cleared at the
# top of evaluate()). Serves the ASSERTIONS only: write_health re-reads fresh
# under the vault lock, and the watchdog (standalone) reads fresh itself.
_STATE_CACHE: dict[str, dict] = {}


def _run_state() -> dict:
    if "state" not in _STATE_CACHE:
        _STATE_CACHE["state"] = read_json(p("meta") / "run-state.json",
                                          default={}) or {}
    return _STATE_CACHE["state"]

_QMD_NOT_UNDERSTOOD = "qmd status output not understood"

# "  Pending:  7 need embedding (run 'qmd embed')"
_PENDING_RE = re.compile(r"^\s*Pending:\s*([\d,]+)\s+need embedding", re.M)
# "  Vectors:  5000 embedded" — proves the Documents section parsed even
# when there is no backlog line to find.
_VECTORS_RE = re.compile(r"^\s*Vectors:\s*[\d,]+\s+embedded", re.M)
# "  wisdom (qmd://wisdom/)" opens a collection block …
_COLL_HEAD_RE = re.compile(r"^\s+([\w.-]+)\s+\(qmd://")
# … and "    Files:    432 (updated 14d ago)" carries its age.
_UPDATED_RE = re.compile(r"\(updated\s+([^)]+?)\s+ago\)")
# qmd prints "(updated just now)" for a very recent scan — no "ago" suffix.
_JUST_NOW_RE = re.compile(r"\(updated\s+just\s+now\)")
_AGE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(mo|s|m|h|d|w|y)$")
_AGE_HOURS = {"s": 1 / 3600, "m": 1 / 60, "h": 1.0, "d": 24.0,
              "w": 24.0 * 7, "mo": 24.0 * 30, "y": 24.0 * 365}


def _qmd_status_raw() -> str:
    """The one subprocess call — read-only, isolated so tests can stub it.

    cwd is pinned to the project root. qmd resolves its project and
    collection set from the caller's working directory, and cron callers
    invoke this script by absolute path without `cd`-ing into the project.
    An unpinned call therefore reports on whatever (usually empty) index the
    caller's directory resolves to, which surfaces as "qmd status output not
    understood (no collection ages)" rather than as a stale vault index. See
    tests/test_healthcheck.py QmdSubprocessCwdTest.
    """
    out = subprocess.run(["qmd", "status"], capture_output=True, text=True,
                         timeout=QMD_TIMEOUT_S, cwd=p("root"))
    if out.returncode != 0:
        raise RuntimeError(f"qmd status exited {out.returncode}: "
                           f"{(out.stderr or '').strip()[:200]}")
    return out.stdout


def qmd_status_text() -> str:
    """Cached per evaluation. Raises RuntimeError with a clear detail when
    qmd is absent, times out, or exits non-zero — the calling assertions turn
    that into ok=false at their declared severity."""
    if "text" not in _QMD_CACHE:
        try:
            text = _qmd_status_raw()
        except FileNotFoundError:
            raise RuntimeError("qmd not on PATH") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"qmd status timed out after {QMD_TIMEOUT_S}s") from None
        _QMD_CACHE["text"] = text
    return _QMD_CACHE["text"]


def parse_pending_embeds(text: str) -> int:
    m = _PENDING_RE.search(text)
    if m:
        return int(m.group(1).replace(",", ""))
    if _VECTORS_RE.search(text):
        # Documents section understood, no backlog line: nothing pending.
        return 0
    raise ValueError(f"{_QMD_NOT_UNDERSTOOD} (no Pending/Vectors line)")


def _age_to_hours(age: str) -> float:
    m = _AGE_RE.match(age.strip())
    if not m:
        raise ValueError(f"{_QMD_NOT_UNDERSTOOD} (unparseable age {age!r})")
    return float(m.group(1)) * _AGE_HOURS[m.group(2)]


def parse_collection_ages(text: str) -> dict[str, float]:
    """Per-collection `updated … ago` ages in hours, keyed by collection."""
    ages: dict[str, float] = {}
    current: str | None = None
    for line in text.splitlines():
        head = _COLL_HEAD_RE.match(line)
        if head:
            current = head.group(1)
            continue
        if current:
            if _JUST_NOW_RE.search(line):
                ages[current] = 0.0
                current = None
                continue
            upd = _UPDATED_RE.search(line)
            if upd:
                ages[current] = _age_to_hours(upd.group(1))
                current = None
    if not ages:
        raise ValueError(f"{_QMD_NOT_UNDERSTOOD} (no collection ages)")
    return ages


# --------------------------------------------------------------------------
# The launch assertions (pure reads; the spine observes, never mutates)
# --------------------------------------------------------------------------

def assert_embeddings_current():
    limit = int(health_cfg("embed_pending_max"))
    try:
        pending = parse_pending_embeds(qmd_status_text())
    except (RuntimeError, ValueError) as exc:
        return False, str(exc)
    if pending > limit:
        return False, (f"{pending} chunks pending embedding "
                       f"(health.embed_pending_max {limit}) — run 'qmd embed'")
    return True, f"{pending} pending (max {limit})"


def _recently_reindexed(window_h: float) -> set[str]:
    """Collections a `last_reindex` record (bin/_common.sh reindex) shows were
    successfully scanned within the window. An absent, undated, or unparseable
    record rescues nothing."""
    state = _run_state()
    rec = state.get("last_reindex")
    if not isinstance(rec, dict):
        return set()
    age_h = hours_since(str(rec.get("at")))
    if age_h is None or age_h > window_h:
        return set()
    return {str(c) for c in rec.get("collections") or []}


def assert_index_fresh():
    window = float(health_cfg("index_stale_hours"))
    try:
        ages = parse_collection_ages(qmd_status_text())
    except (RuntimeError, ValueError) as exc:
        return False, str(exc)
    # qmd's per-collection age tracks content CHANGE, not last scan (a static
    # corpus can sit at 'updated 14d ago' while being rescanned nightly). A collection
    # is fresh if qmd's age is inside the window OR a recent successful reindex covered it.
    rescued = _recently_reindexed(window)
    # A configured collection qmd stops reporting must not silently drop out
    # of the staleness check — absence of evidence of freshness blocks. An
    # empty collection ("Files: 0 (updated never)") has no age either; a
    # recent successful scan of it is that evidence. A collection qmd does not
    # know fails its `qmd update -c`, so reindex never records it as scanned.
    want = list((CFG.get("qmd") or {}).get("collections") or [])
    missing = [c for c in want if c not in ages and c not in rescued]
    if missing:
        return False, (f"qmd status reports no age for configured "
                       f"collection(s): {', '.join(missing)} — cannot judge "
                       f"freshness")
    stale = {name: h for name, h in ages.items()
             if h > window and name not in rescued}
    if stale:
        listing = ", ".join(f"{n} updated {h:.0f}h ago"
                            for n, h in sorted(stale.items()))
        return False, (f"stale collection index "
                       f"(health.index_stale_hours {window:g}): {listing}")
    via_reindex = sorted(n for n, h in ages.items()
                         if h > window and n in rescued)
    detail = f"{len(ages)} collection(s) fresh within {window:g}h"
    if via_reindex:
        detail += (" (" + ", ".join(via_reindex)
                   + " via recent last_reindex; qmd age tracks content change)")
    return True, detail


def assert_collections_covered():
    want = list((CFG.get("qmd") or {}).get("collections") or [])
    if not want:
        return False, "config.yaml is missing qmd.collections"
    state = _run_state()
    rec = state.get("last_reindex")
    if not isinstance(rec, dict) or not rec.get("collections"):
        return False, "no last_reindex record"
    covered = set(rec["collections"])
    missing = [c for c in want if c not in covered]
    if missing:
        return False, (f"last reindex ({rec.get('at', 'undated')}) missed "
                       f"collection(s): {', '.join(missing)}")
    return True, (f"all {len(want)} configured collections reindexed "
                  f"at {rec.get('at', 'undated')}")


def assert_vocabulary_applied():
    """Vocabulary exists but recent loops are 100% untagged — the tagging
    step has silently stopped. Vacuous windows must not fire: no vocabulary
    file, or zero loops created in the window, both pass."""
    if not (p("meta") / "tag-vocabulary.json").exists():
        return True, "no tag vocabulary yet — not applicable"
    window_days = int(health_cfg("untagged_window_days"))
    cutoff = today() - _dt.timedelta(days=window_days)
    recent = tagged = 0
    for page in sorted(p("loops").glob("*.md")):
        if page.name.startswith("_"):  # _catalog.md is an index, not a loop
            continue
        fm, _ = read_page(page)
        created = as_date(fm.get("created"))
        if created is None or created < cutoff:
            continue
        recent += 1
        if fm.get("tags"):
            tagged += 1
    if recent == 0:
        return True, f"no loops created in the last {window_days}d — vacuous"
    if tagged == 0:
        return False, (f"all {recent} loop(s) created in the last "
                       f"{window_days}d are untagged despite an approved "
                       f"vocabulary")
    return True, f"{tagged}/{recent} loop(s) in the {window_days}d window tagged"


def assert_cost_records_sane():
    """Windowed to health.cost_window_days: costs accumulate forever in
    run-state.json, so an unwindowed max means one historical outlier (e.g.
    a documented backfill spike) alarms on every run until someone edits the
    file. A record whose `at` cannot be parsed counts as in-window —
    conservative: a record we cannot date might be fresh."""
    ceiling = float(health_cfg("cost_max_per_run"))
    window_days = float(health_cfg("cost_window_days"))
    state = _run_state()
    recent = []
    for c in state.get("costs") or []:
        if not isinstance(c, dict):
            continue
        age_h = hours_since(str(c.get("at", "")))
        if age_h is not None and age_h > window_days * 24.0:
            continue
        recent.append(float(c.get("cost", 0.0)))
    if not recent:
        return True, f"no cost records in the last {window_days:g}d"
    worst = max(recent)
    if worst > ceiling:
        return False, (f"max run cost ${worst:.2f} in the last "
                       f"{window_days:g}d exceeds health.cost_max_per_run "
                       f"${ceiling:.2f}")
    return True, (f"max run cost ${worst:.2f} in the last {window_days:g}d "
                  f"(ceiling ${ceiling:.2f})")


def assert_fold_queue_current():
    """The fold queue (rule 15) must stay bounded, current, and
    quarantine-empty. Depth over thread.fold_queue_max means folds are
    enqueued faster than the drain retires them; an entry older than
    thread.fold_queue_max_age_days means the drain has stopped retiring it;
    a non-empty fold-quarantine.json holds folds that failed their attempt
    cap and need repair. Entries without an enqueued_at stamp (pre-existing
    queues) count as fresh — conservative; they age from their first stamped
    write onward."""
    limit = int(thread_cfg("fold_queue_max"))
    max_age_days = float(thread_cfg("fold_queue_max_age_days"))
    entries = read_json(p("meta") / "fold-pending.json", default=[]) or []
    quarantine = read_json(p("meta") / "fold-quarantine.json",
                           default=[]) or []
    depth = len(entries)
    oldest_h = 0.0
    for e in entries:
        if not isinstance(e, dict):
            continue
        age_h = hours_since(str(e.get("enqueued_at") or ""))
        if age_h is not None and age_h > oldest_h:
            oldest_h = age_h
    summary = (f"depth {depth} (max {limit}), oldest {oldest_h / 24.0:.1f}d "
               f"(max {max_age_days:g}d), quarantined {len(quarantine)}")
    issues = []
    if depth > limit:
        issues.append(f"queue depth {depth} exceeds thread.fold_queue_max "
                      f"{limit}")
    if oldest_h > max_age_days * 24.0:
        issues.append(f"oldest queued fold is {oldest_h / 24.0:.1f}d old "
                      f"(thread.fold_queue_max_age_days {max_age_days:g}) — "
                      f"the drain has stopped retiring it")
    if quarantine:
        issues.append(f"{len(quarantine)} entr(y/ies) quarantined in "
                      f"fold-quarantine.json — repair and re-enqueue")
    if issues:
        return False, "; ".join(issues) + f" — {summary}"
    return True, summary


# Standing owner gates: reminders that surface in every health record until
# their flag in vault/.vault-meta/gate-state.json flips to true (a file the
# OWNER writes — this gives the reminders a machine-readable close signal
# without automating the gates themselves). Absent file = nothing closed.

def _configured_gates() -> list[dict]:
    """Owner acceptance gates come from config, not code: each entry is
    {"flag": <key in vault/.vault-meta/gate-state.json>, "text": <reminder>}.
    An empty list means no standing gates — the assertion passes."""
    return list((CFG.get("health") or {}).get("gates") or [])


def assert_gates_current():
    gates = _configured_gates()
    if not gates:
        return True, "no standing owner gates configured"
    state = read_json(p("meta") / "gate-state.json", default={}) or {}
    open_gates = [g["text"] for g in gates if not state.get(g["flag"])]
    if open_gates:
        return False, "; ".join(open_gates)
    return True, f"all {len(gates)} configured gate(s) recorded closed"


def assert_goal_catalog_fresh():
    goals = G.load_goals()
    cat_path = G.goals_dir() / G.CATALOG
    if not cat_path.exists():
        if goals:
            return False, "goals catalog missing on disk"
        return True, "0 goals, catalog not needed yet"
    try:
        cfm, _ = read_page(cat_path)
        count = int(cfm.get("count") or 0)
        if count != len(goals):
            return False, f"catalog count {count} != goals on disk {len(goals)}"
        return True, f"catalog fresh ({count} goals)"
    except Exception as exc:  # noqa: BLE001
        return False, f"unreadable goals catalog: {exc}"


def assert_goal_cap_respected():
    active = G.active_goals_count()
    max_active = G.max_active_goals()
    if active > max_active:
        return False, f"{active} active goals exceed goals.max_active {max_active}"
    return True, f"{active}/{max_active} active goals"


def assert_goal_clocks_sane():
    problems = []
    goals = G.load_goals()
    t = today()
    for g in goals:
        if g.last_mention and g.last_movement and g.last_movement > g.last_mention:
            problems.append(f"{g.id}: last_movement ({g.last_movement}) > last_mention ({g.last_mention})")
        if g.last_mention and g.last_mention > t:
            problems.append(f"{g.id}: last_mention in future ({g.last_mention})")
        if g.last_movement and g.last_movement > t:
            problems.append(f"{g.id}: last_movement in future ({g.last_movement})")
        if g.created:
            if g.last_mention and g.last_mention < g.created:
                problems.append(f"{g.id}: last_mention predates created")
            if g.last_movement and g.last_movement < g.created:
                problems.append(f"{g.id}: last_movement predates created")
    if problems:
        return False, "; ".join(problems)
    return True, f"clocks sane across {len(goals)} goal(s)"


def assert_goal_quiet_diff():
    # Check that goals have consistent attachments and trajectory lines
    goals = G.load_goals()
    problems = []
    for g in goals:
        assessment = G.assessment_section(g.body or "")
        if assessment:
            traj = [ln for ln in assessment.splitlines() if re.match(r"^-\s*\d{4}-\d{2}-\d{2}", ln.strip())]
            for ln in traj:
                m = re.search(r"\[\[([^\]]+)\]\]", ln)
                if m:
                    wikilink = f"[[{m.group(1)}]]"
                    if wikilink not in g.attachments:
                        problems.append(f"{g.id}: trajectory cites {wikilink} not in attachments")
    if problems:
        return False, "; ".join(problems)
    return True, f"quiet diff respected across {len(goals)} goal(s)"


def assert_step_citations():
    goals = G.load_goals()
    problems = []
    for g in goals:
        plan_re = re.compile(r"^## Plan \(revision \d+\)\s*$(.*?)(?=^## |\Z)", re.M | re.S)
        pm = plan_re.search(g.body)
        if pm:
            for step_line in pm.group(1).splitlines():
                step_line = step_line.strip()
                if re.match(r"^-\s*\[[xX]\]", step_line):
                    m = G._DONE_STEP_RE.match(step_line)
                    if not m or not m.group(3):
                        problems.append(f"{g.id}: done step lacks citation: {step_line!r}")
    if problems:
        return False, "; ".join(problems)
    return True, f"step citations valid across {len(goals)} goal(s)"


def assert_owner_writes_verified():
    writes = Q.load_owner_writes()
    if not writes:
        return True, "no owner writes recorded yet"

    lag_days = float((CFG.get("mcp") or {}).get("owner_write_verify_lag_days", 3))
    unverified = []

    corpus = Q.human_corpus()
    # A quarantined write whose question the owner has answered is settled:
    # they ruled on it, so it must not alarm forever.
    answered = {q.get("id") for q in Q.load_questions() if q.get("status") == "answered"}

    verified_count = 0
    for w in writes:
        quote = (w.get("owner_quote") or "").strip()
        if not quote:
            continue
        if w.get("verified") is True or Q.quote_in(quote, corpus):
            verified_count += 1
            continue
        if w.get("verified") is False and w.get("question_id") in answered:
            continue
        age_h = hours_since(str(w.get("at") or ""))
        if age_h is not None and age_h > lag_days * 24.0:
            unverified.append(f"{w['id']} ({w.get('tool')}) quote={quote[:25]!r}")

    if unverified:
        return False, f"{len(unverified)} write(s) unverified past {lag_days:g}d lag: {', '.join(unverified)}"
    return True, f"{verified_count}/{len(writes)} owner write(s) verified"


def assert_open_questions_drained():
    questions = Q.load_questions()
    max_weeks = int((CFG.get("goals") or {}).get("silence_escalation_weeks", 3))
    limit_days = max_weeks * 7.0
    stale_questions = []

    for q in questions:
        if q.get("status") in ("pending", "asked"):
            age_h = hours_since(str(q.get("created_at") or ""))
            if age_h is not None and age_h > limit_days * 24.0:
                stale_questions.append(f"{q['id']} ({q.get('kind')}) {age_h/24.0:.1f}d")

    if stale_questions:
        return False, f"{len(stale_questions)} question(s) pending past {max_weeks}w: {', '.join(stale_questions)}"
    pending_count = sum(1 for q in questions if q.get("status") in ("pending", "asked"))
    return True, f"{pending_count} open question(s) pending (all within {max_weeks}w window)"


def assert_assessment_queue_depth():
    entries = read_json(p("meta") / "fold-pending.json", default=[]) or []
    depth = len(entries)
    limit = int(thread_cfg("fold_queue_max"))
    if depth > limit:
        return False, f"assessment/fold queue depth {depth} exceeds {limit}"
    return True, f"queue depth {depth} (max {limit})"


def assert_no_numeric_progress():
    goals = G.load_goals()
    problems = []
    for g in goals:
        raw = g.path.read_text(encoding="utf-8") if g.path and g.path.exists() else g.body
        if G._NUMERIC_PROGRESS_RE.search(raw):
            problems.append(f"{g.id} contains numeric progress key")
    if problems:
        return False, "; ".join(problems)
    return True, "no numeric progress keys in goal pages"


# --------------------------------------------------------------------------
# Freshness (see docs/freshness-schema.md).
# Verdicts are computed once per run by scripts/freshness.py and read here —
# the healthcheck judges nothing itself and, as ever, writes nothing. All
# three are `degraded` and block no leg: blocking a leg because the system is
# stale would make it staler.

_FRESH_JOBS_SKIP = ("job.healthcheck",)            # the watchdog owns it
_FRESH_CONVERGED = ("converge.extraction", "converge.assessment",
                    "converge.frontier-map", "converge.premortem")
# converge.folds and converge.search-index are already asserted by
# fold-queue-current and index-fresh.


def _freshness() -> dict:
    if "freshness" not in _STATE_CACHE:
        _STATE_CACHE["freshness"] = FR.collect()
    return _STATE_CACHE["freshness"]


def _freshness_verdict(select, what: str):
    axes = [a for a in _freshness()["axes"] if select(a)]
    bad = [a for a in axes if a["status"] in FR.FAILING]
    if bad:
        # Axis id and verdict only — no age. write_health dedups events on
        # the exact detail string, and an age that changes every run would
        # stack one digest line per healthcheck. The reason is on the
        # dashboard and in `freshness.py --json`.
        return False, "; ".join(f"{a['axis']} {a['status']}" for a in bad)
    return True, f"{len(axes)} {what} within window"


def assert_freshness_jobs():
    return _freshness_verdict(
        lambda a: a["kind"] == "job" and a["axis"] not in _FRESH_JOBS_SKIP,
        "job axis(es)")


def assert_freshness_sources():
    return _freshness_verdict(
        lambda a: a["kind"] == "source" and a["status"] != "info",
        "source axis(es)")


def assert_freshness_converged():
    return _freshness_verdict(lambda a: a["axis"] in _FRESH_CONVERGED,
                              "convergence axis(es)")


def assert_spine_complete():
    """Every current conclusion that carries a problem type has a valid type
    and a heading for each stage that type requires (rule 5b). A stage the
    writer honestly marked missing still has its heading and passes here; the
    quality grader is what scores it. Reads pages, writes nothing."""
    base = p("conclusions")
    bad, seen = [], 0
    for f in sorted(base.glob("*.md")) if base.exists() else []:
        try:
            fm, body = read_page(f)
        except Exception:  # noqa: BLE001
            continue
        ptype = str(fm.get("problem_type") or "")
        if not ptype or fm.get("superseded_by"):
            continue
        seen += 1
        if ptype not in SP.TYPES:
            bad.append(f"{f.stem}: unknown problem type")
            continue
        absent = [st for st in SP.required(ptype)
                  if not re.search(rf"^## {re.escape(SP.HEADINGS[st])}\s*$", body, re.M)]
        if absent:
            bad.append(f"{f.stem}: no {', '.join(absent)}")
    if bad:
        return False, "; ".join(bad)
    return True, f"{seen} spine conclusion(s) complete"


# --------------------------------------------------------------------------
# Knowledge frontier. Mechanical comparisons of pages already on disk: no
# model call, no write. All `degraded`, none blocking: the release step (frontier_sync)
# refuses a lesson that fails integrity whatever these say. An absent frontier passes
# (nothing built yet); each assertion is proved against a seeded defect in the tests.

def _ids(found: Iterable[str]) -> str:
    """Names at most health.detail_max_ids ids and counts the rest, so a detail stays one line."""
    shown = int(CFG["health"]["detail_max_ids"])
    found = sorted(found)
    head = ", ".join(found[:shown])
    return head + (f" (+{len(found) - shown} more)" if len(found) > shown else "")


def _frontier_items() -> list:
    d = FRN.items_dir()
    return [FRN.Item.from_path(f) for f in sorted(d.glob("K*.md"))] if d.exists() else []


def _frontier_lessons() -> list:
    d = FRN.lessons_dir()
    return [FRN.Lesson.from_path(f) for f in sorted(d.glob("K*.md"))] if d.exists() else []


def assert_frontier_released_integrity():
    lessons = [l for l in _frontier_lessons() if l.status in FRN.DELIVERED_STATUSES]
    bad = {l.id: FRL.lesson_integrity(l) for l in lessons}
    bad = {k: v for k, v in bad.items() if v}
    if bad:
        first = next(iter(sorted(bad)))
        return False, f"{len(bad)} released lesson(s) fail integrity: {_ids(bad)} ({bad[first][0]})"
    return True, f"{len(lessons)} released lesson(s) passed their check and cite listed sources"


def assert_frontier_quadrant_consistent():
    active = FRN.active_goal_ids()
    items = _frontier_items()
    by_item: dict[str, list] = {}
    for les in _frontier_lessons():
        by_item.setdefault(les.item, []).append(les)
    bad = []
    for it in items:
        lessons = [l for l in by_item.get(it.id, []) if l.id in set(it.lessons)]
        # Accepted when the stored quadrant is what the item's goals made it, whether or not a
        # goal has been paused since: placement is re-derived on the item's next write, and a
        # pause alone is not a defect.
        related = {g["goal"] for g in it.goals}
        if it.quadrant not in (FRN.derive_quadrant(it, active, lessons),
                               FRN.derive_quadrant(it, related, lessons)):
            bad.append(it.id)
    if bad:
        return False, f"{len(bad)} item(s) sit in a quadrant their evidence does not give: {_ids(bad)}"
    return True, f"{len(items)} item(s) in the quadrant their evidence and taps give"


def assert_frontier_uu_no_owner_evidence():
    # An item an aired lesson pinned keeps the quadrant the owner was told it was in
    # even if the owner's words arrived afterwards, so it is not a defect.
    pinned = {l.item for l in _frontier_lessons() if l.aired_on and l.aired_quadrant}
    bad = [it.id for it in _frontier_items() if it.quadrant == "unknown_unknown" and it.id not in pinned
           and any(e["kind"] != "exposure" for e in it.evidence)]
    if bad:
        return False, f"{len(bad)} unknown unknown item(s) hold owner evidence: {_ids(bad)}"
    return True, "no unknown unknown item holds owner evidence"


def assert_frontier_ceiling():
    """No trailing ceiling_window_days window holds more than weekly_ceiling NEW lessons
    released. Follow-ups are exempt; a failed lesson frees its slot. Counts the
    same lessons frontier_sync._plan does, so the two cannot disagree about the window."""
    ceiling = int(FRN._cfg("weekly_ceiling"))
    window = float(FRN._cfg("ceiling_window_days")) * 86400.0
    stamps, unreadable = [], []
    for les in _frontier_lessons():
        if les.kind != "new" or les.status == "failed" or not les.released_at:
            continue
        key = FRN._at_key(les.released_at)
        if key[0] != 0:
            unreadable.append(les.id)
        else:
            stamps.append((key[1], les.id))
    stamps.sort()
    over = []
    for i in range(len(stamps) - ceiling):
        if stamps[i + ceiling][0] - stamps[i][0] <= window:
            over.append(stamps[i][1])
    issues = []
    if over:
        issues.append(f"more than {ceiling} new lessons released within {FRN._cfg('ceiling_window_days'):g} days "
                      f"from {_ids(over)}")
    if unreadable:
        issues.append(f"unreadable released_at on {_ids(unreadable)}")
    if issues:
        return False, "; ".join(issues)
    return True, f"at most {ceiling} new lessons released in any {FRN._cfg('ceiling_window_days'):g}-day window"


def assert_frontier_stuck_items():
    """Work that reached frontier.max_parse_attempts and was given up: a chat the map
    skipped, a lesson key the researcher skipped, a lesson at the limit, a pre-mortem
    candidate that ran out of attempts. Each is a defect to look at, not work to retry."""
    limit = int(FRN._cfg("max_parse_attempts"))
    stuck = []
    stuck += [f"map:{k}" for k in FRM.load_ledger()["skipped"]]
    stuck += [f"lesson-run:{k}" for k in FRL.load_ledger()["skipped"]]
    stuck += [f"lesson:{l.id}" for l in _frontier_lessons() if l.parse_attempts >= limit]
    base = FRN.frontier_dir() / FRN._cfg("premortems_dir")
    for gdir in sorted(base.iterdir()) if base.exists() else []:
        rec = FRG.latest_record(gdir.name) if gdir.is_dir() else None
        for c in (rec or {}).get("candidates", []):
            if c.get("status") != "done" and int(c.get("attempts") or 0) >= limit:
                stuck.append(f"premortem:{gdir.name}")
                break
    if stuck:
        return False, f"{len(stuck)} stuck at {limit} attempts: {_ids(stuck)}"
    return True, f"nothing at {limit} attempts"


register("embeddings-current", "degraded", (), assert_embeddings_current)
register("index-fresh", "blocking", ("research",), assert_index_fresh)
register("collections-covered", "degraded", (), assert_collections_covered)
register("vocabulary-applied", "degraded", (), assert_vocabulary_applied)
register("cost-records-sane", "degraded", (), assert_cost_records_sane)
register("fold-queue-current", "degraded", (), assert_fold_queue_current)
register("gates-current", "info", (), assert_gates_current)

# The Worried Friend assertions (§8.1)
register("goal-catalog-fresh", "degraded", (), assert_goal_catalog_fresh)
register("goal-cap-respected", "blocking", ("owner-write",), assert_goal_cap_respected)
register("goal-clocks-sane", "degraded", (), assert_goal_clocks_sane)
register("goal-quiet-diff", "degraded", (), assert_goal_quiet_diff)
register("step-citations", "blocking", ("owner-write",), assert_step_citations)
register("owner-writes-verified", "degraded", (), assert_owner_writes_verified)
register("open-questions-drained", "degraded", (), assert_open_questions_drained)
register("assessment-queue-depth", "degraded", (), assert_assessment_queue_depth)
register("no-numeric-progress", "info", (), assert_no_numeric_progress)
register("freshness-jobs", "degraded", (), assert_freshness_jobs)
register("freshness-sources", "degraded", (), assert_freshness_sources)
register("freshness-converged", "degraded", (), assert_freshness_converged)
register("spine-complete", "degraded", (), assert_spine_complete)
register("frontier-released-integrity", "degraded", (), assert_frontier_released_integrity)
register("frontier-quadrant-consistent", "degraded", (), assert_frontier_quadrant_consistent)
register("frontier-uu-no-owner-evidence", "degraded", (), assert_frontier_uu_no_owner_evidence)
register("frontier-ceiling", "degraded", (), assert_frontier_ceiling)
register("frontier-stuck-items", "degraded", (), assert_frontier_stuck_items)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="print the health record to stdout")
    ap.add_argument("--watchdog", action="store_true",
                    help="staleness-only check; runs no assertions")
    args = ap.parse_args(argv)

    if args.watchdog:
        return watchdog()

    # Fail loudly on incomplete config at every run, not only when the
    # watchdog first needs the key.
    health_cfg("checked_max_age_hours")
    try:
        record = run()
    except Exception as exc:  # noqa: BLE001 — write failure is the one
        # non-zero path; callers treat it as degraded, never a cycle abort.
        log(f"FAILED to write the health record: {exc}", job="healthcheck")
        return 1
    log(f"health recorded: {len(record['assertions'])} assertion(s), "
        f"blocked legs: {', '.join(record['blocked_legs']) or 'none'}",
        job="healthcheck")
    if args.json:
        print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
