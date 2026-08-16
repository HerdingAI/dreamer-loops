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

The U3 launch set (registered at the bottom of this file): embeddings-current,
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
# U3 — qmd status (read-only) and its parsers
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
    """The one subprocess call — read-only, isolated so tests can stub it."""
    out = subprocess.run(["qmd", "status"], capture_output=True, text=True,
                         timeout=QMD_TIMEOUT_S)
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
# U3 — the launch assertions (pure reads; the spine observes, never mutates)
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
    # A configured collection qmd stops reporting must not silently drop out
    # of the staleness check — absence of evidence of freshness blocks.
    want = list((CFG.get("qmd") or {}).get("collections") or [])
    missing = [c for c in want if c not in ages]
    if missing:
        return False, (f"qmd status reports no age for configured "
                       f"collection(s): {', '.join(missing)} — cannot judge "
                       f"freshness")
    # qmd's per-collection age tracks content CHANGE, not last scan (observed
    # live: the static wisdom corpus sits at "updated 14d ago" while being
    # rescanned nightly). A collection is fresh if qmd's age is inside the
    # window OR a recent successful reindex covered it.
    rescued = _recently_reindexed(window)
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


register("embeddings-current", "degraded", (), assert_embeddings_current)
register("index-fresh", "blocking", ("research",), assert_index_fresh)
register("collections-covered", "degraded", (), assert_collections_covered)
register("vocabulary-applied", "degraded", (), assert_vocabulary_applied)
register("cost-records-sane", "degraded", (), assert_cost_records_sane)
register("fold-queue-current", "degraded", (), assert_fold_queue_current)
register("gates-current", "info", (), assert_gates_current)


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
