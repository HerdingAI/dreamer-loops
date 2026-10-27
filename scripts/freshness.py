#!/usr/bin/env python3
"""Freshness verdicts.

For every axis: when it last ran, last produced, last received input, and
whether that is too long ago. Pure — reads heartbeats and the existing
ledgers, never calls a model, never writes. The healthcheck, the dashboard
and the Postgres publisher all read collect(); none recomputes a verdict.

Everything returned here may leave the machine. Reasons are built from numbers
only; nothing in this module reads a title, a quote or a path into its output.

Usage:
    freshness.py --json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, p, read_json  # noqa: E402
import apply_assessment as A  # noqa: E402
import heartbeat as HB  # noqa: E402
import make_batch as MB  # noqa: E402

FAILING = ("stale", "never")
HOURS_PER_DAY = 24.0
_DIGEST_RE = re.compile(r"^\d{4}-\d{2}\.md$")


def cfg(section: str, key: str):
    """Required config, loud on absence — a soft default would judge
    staleness against a number nobody chose."""
    block = CFG.get(section)
    if not isinstance(block, dict) or key not in block:
        raise SystemExit(f"config.yaml is missing required key {section}.{key} — "
                         f"refusing to guess a default")
    return block[key]


# id, kind, label, rule, then rule-specific fields. `limit` is
# (config section, config key, hours per unit).
AXES = [
    {"axis": "source.claude-export", "kind": "source", "label": "claude.ai export",
     "rule": "source", "ledger": "claude-export",
     "limit": ("freshness", "stale_inbox_days", HOURS_PER_DAY)},
    {"axis": "source.cc-sessions", "kind": "source", "label": "Claude Code sessions",
     "rule": "source", "ledger": "cc-sessions",
     "limit": ("freshness", "cc_sessions_quiet_days", HOURS_PER_DAY)},
    {"axis": "source.owner-writes", "kind": "source", "label": "Owner writes through chat",
     "rule": "owner-writes"},
    {"axis": "job.ingest-cc", "kind": "job", "label": "Claude Code ingest",
     "rule": "job", "job": "ingest-cc", "limit": ("freshness", "job_max_age_hours", 1.0)},
    {"axis": "job.nightly-extract", "kind": "job", "label": "Nightly extraction",
     "rule": "job", "job": "nightly-extract", "limit": ("freshness", "job_max_age_hours", 1.0)},
    {"axis": "job.nightly-assess", "kind": "job", "label": "Nightly goal assessment",
     "rule": "job", "job": "nightly-assess", "limit": ("freshness", "job_max_age_hours", 1.0)},
    {"axis": "job.night-cycle", "kind": "job", "label": "Night cycle",
     "rule": "job", "job": "night-cycle", "limit": ("freshness", "job_max_age_hours", 1.0)},
    # The night cycle swallows its legs' failures by design (a deferring
    # backfill must not cost the dream its turn), so the wrapper alone can
    # never read `failed`. The research leg is judged in its own right.
    {"axis": "job.weekly-dream", "kind": "job", "label": "Research (dream) leg",
     "rule": "job", "job": "weekly-dream", "limit": ("freshness", "job_max_age_hours", 1.0)},
    {"axis": "job.decay-archive", "kind": "job", "label": "Weekly decay",
     "rule": "job", "job": "decay-archive",
     "limit": ("freshness", "weekly_job_max_age_days", HOURS_PER_DAY)},
    {"axis": "job.healthcheck", "kind": "job", "label": "Healthcheck",
     "rule": "healthcheck", "limit": ("health", "checked_max_age_hours", 1.0)},
    {"axis": "converge.extraction", "kind": "convergence", "label": "Transcripts awaiting extraction",
     "rule": "extraction", "limit": ("freshness", "extract_lag_days", HOURS_PER_DAY)},
    {"axis": "converge.assessment", "kind": "convergence", "label": "Transcripts awaiting assessment",
     "rule": "assessment", "limit": ("freshness", "assess_lag_days", HOURS_PER_DAY)},
    {"axis": "converge.folds", "kind": "convergence", "label": "Thread folds queued",
     "rule": "folds", "limit": ("thread", "fold_queue_max_age_days", HOURS_PER_DAY)},
    {"axis": "converge.search-index", "kind": "convergence", "label": "Search index",
     "rule": "search-index", "limit": ("health", "index_stale_hours", 1.0)},
    {"axis": "output.conclusion", "kind": "output", "label": "Newest conclusion",
     "rule": "output", "dir": "conclusions"},
    {"axis": "output.digest", "kind": "output", "label": "Newest digest",
     "rule": "output", "dir": "digests"},
    {"axis": "publish.postgres", "kind": "publish", "label": "Postgres publish",
     "rule": "publish", "limit": ("freshness", "publish_max_age_hours", 1.0),
     "off_reason": "disabled"},
]


# --------------------------------------------------------------------------
# Helpers

def _parse(ts) -> _dt.datetime | None:
    """ISO string -> naive LOCAL datetime. Ledger stamps ending in Z are UTC
    and are converted; heartbeats are already naive local."""
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _iso(dt: _dt.datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _age_h(now: _dt.datetime, dt: _dt.datetime | None) -> float | None:
    if dt is None:
        return None
    # A stamp ahead of the clock (clock change, DREAMER_TODAY) is "just now".
    return max((now - dt).total_seconds() / 3600.0, 0.0)


def _limit_h(spec: dict) -> float:
    section, key, per_unit = spec["limit"]
    return float(cfg(section, key)) * per_unit


def _judge(age_h: float | None, limit_h: float, what: str) -> tuple[str, str]:
    if age_h is None:
        return "never", f"no {what} recorded"
    verdict = "stale" if age_h > limit_h else "ok"
    return verdict, f"{what} {age_h:.1f}h ago (max {limit_h:g}h)"


def _axis(spec: dict, **fields) -> dict:
    out = {"axis": spec["axis"], "kind": spec["kind"], "label": spec["label"],
           "status": "info", "last_ran": None, "last_produced": None,
           "last_arrival": None, "last_outcome": None, "threshold_hours": None,
           "backlog": None, "reason": ""}
    out.update(fields)
    return out


def _newest(stamps) -> _dt.datetime | None:
    parsed = [d for d in (_parse(s) for s in stamps) if d is not None]
    return max(parsed) if parsed else None


def _mtime(f: Path) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(f.stat().st_mtime).replace(microsecond=0)


# --------------------------------------------------------------------------
# Rules

def _rule_job(spec, hb, state, now):
    j = hb["jobs"].get(spec["job"])
    j = j if isinstance(j, dict) else {}
    limit = _limit_h(spec)
    last_ok = _parse(j.get("last_ok"))
    status, reason = _judge(_age_h(now, last_ok), limit, "last ok run")
    if j.get("last_outcome") == "failed":
        status, reason = "stale", f"last run failed; {reason}"
    return _axis(spec, status=status, reason=reason, last_ran=_iso(last_ok),
                 last_produced=_iso(_parse(j.get("last_produced"))),
                 last_outcome=j.get("last_outcome") if j.get("last_ok") or j.get("last_outcome") == "failed" else None,
                 threshold_hours=limit)


def _rule_healthcheck(spec, hb, state, now):
    limit = _limit_h(spec)
    checked = _parse((state.get("health") or {}).get("checked_at"))
    status, reason = _judge(_age_h(now, checked), limit, "last check")
    return _axis(spec, status=status, reason=reason, last_ran=_iso(checked),
                 threshold_hours=limit)


def _source_arrival(ledger: str) -> tuple[_dt.datetime | None, bool]:
    """(newest arrival, ledger present)."""
    if ledger == "claude-export":
        led = read_json(p("meta") / "ingested.json", default=None)
        if not isinstance(led, dict):
            return None, False
        return _newest(e.get("updated_at") for e in led.values()
                       if isinstance(e, dict)), True
    led = read_json(p("meta") / "cc-ingested.json", default=None)
    if not isinstance(led, dict):
        return None, False
    mtimes = [e["mtime"] for e in led.values()
              if isinstance(e, dict) and e.get("status") == "ingested"
              and isinstance(e.get("mtime"), (int, float))]
    if not mtimes:
        return None, True
    return _dt.datetime.fromtimestamp(max(mtimes)).replace(microsecond=0), True


def _rule_source(spec, hb, state, now):
    limit = _limit_h(spec)
    arrival, present = _source_arrival(spec["ledger"])
    if not present:
        return _axis(spec, status="never", reason="ledger missing", threshold_hours=limit)
    status, reason = _judge(_age_h(now, arrival), limit, "last arrival")
    return _axis(spec, status=status, reason=reason, last_arrival=_iso(arrival),
                 threshold_hours=limit)


def _rule_owner_writes(spec, hb, state, now):
    writes = read_json(p("meta") / "owner-writes.json", default=[]) or []
    newest = _newest(w.get("at") for w in writes if isinstance(w, dict))
    age = _age_h(now, newest)
    reason = "no owner write recorded" if age is None else f"last write {age:.1f}h ago"
    return _axis(spec, reason=reason, last_arrival=_iso(newest))


def _backlog_axis(spec, waiting: list[_dt.datetime], now, empty_reason="backlog empty"):
    limit = _limit_h(spec)
    if not waiting:
        return _axis(spec, status="ok", reason=empty_reason, backlog=0,
                     threshold_hours=limit)
    oldest = min(waiting)
    status, reason = _judge(_age_h(now, oldest), limit, "oldest waiting input")
    return _axis(spec, status=status, reason=f"{len(waiting)} waiting; {reason}",
                 backlog=len(waiting), last_arrival=_iso(oldest), threshold_hours=limit)


def _rule_extraction(spec, hb, state, now):
    done = MB.extracted_set()
    waiting = [_mtime(f) for f in MB.all_transcripts() if MB.rel_of(f) not in done]
    return _backlog_axis(spec, waiting, now)


def _rule_assessment(spec, hb, state, now):
    if not any(g.status == "active" for g in A.G.load_goals()):
        return _backlog_axis(spec, [], now, empty_reason="no active goal")
    waiting = [_mtime(f) for f in A.select_transcripts(limit=sys.maxsize)]
    return _backlog_axis(spec, waiting, now)


def _rule_folds(spec, hb, state, now):
    entries = read_json(p("meta") / "fold-pending.json", default=[]) or []
    stamps = [_parse(e.get("enqueued_at")) for e in entries if isinstance(e, dict)]
    waiting = [s for s in stamps if s is not None]
    axis = _backlog_axis(spec, waiting, now)
    axis["backlog"] = len(entries)
    return axis


def _rule_search_index(spec, hb, state, now):
    limit = _limit_h(spec)
    at = _parse((state.get("last_reindex") or {}).get("at"))
    status, reason = _judge(_age_h(now, at), limit, "last reindex")
    return _axis(spec, status=status, reason=reason, last_ran=_iso(at),
                 threshold_hours=limit)


def _rule_output(spec, hb, state, now):
    base = p(spec["dir"])
    files = [f for f in base.glob("*.md")] if base.exists() else []
    if spec["dir"] == "digests":
        files = [f for f in files if _DIGEST_RE.match(f.name)]
    newest = max((_mtime(f) for f in files), default=None)
    age = _age_h(now, newest)
    reason = "none yet" if age is None else f"newest {age:.1f}h ago"
    return _axis(spec, reason=reason, last_produced=_iso(newest))


def _rule_publish(spec, hb, state, now):
    pub = hb.get("publish") or {}
    if not pub.get("configured"):
        return _axis(spec, reason=spec.get("off_reason", "not configured"))
    limit = _limit_h(spec)
    last_ok = _parse(pub.get("last_ok"))
    status, reason = _judge(_age_h(now, last_ok), limit, "last publish")
    return _axis(spec, status=status, reason=reason, last_ran=_iso(last_ok),
                 last_outcome="failed" if pub.get("last_error") else "ok",
                 threshold_hours=limit)


RULES = {"job": _rule_job, "healthcheck": _rule_healthcheck, "source": _rule_source,
         "owner-writes": _rule_owner_writes, "extraction": _rule_extraction,
         "assessment": _rule_assessment, "folds": _rule_folds,
         "search-index": _rule_search_index, "output": _rule_output,
         "publish": _rule_publish}


# --------------------------------------------------------------------------

def collect(now: _dt.datetime | None = None) -> dict:
    now = now or HB.clock()
    hb = HB.load()
    state = read_json(p("meta") / "run-state.json", default={}) or {}
    if not isinstance(state, dict):
        state = {}
    axes = []
    for spec in AXES:
        try:
            axes.append(RULES[spec["rule"]](spec, hb, state, now))
        except SystemExit:
            raise  # missing config is loud, by contract
        except Exception as exc:  # noqa: BLE001 — one unreadable axis must not
            # take the others (or the healthcheck) down with it. Class name
            # only: a message could carry a path.
            status = "info" if "limit" not in spec else "never"
            axes.append(_axis(spec, status=status,
                              reason=f"unreadable: {type(exc).__name__}"))
    stale = sum(1 for a in axes if a["status"] in FAILING)
    return {"generated_at": HB.stamp(now), "overall": "stale" if stale else "ok",
            "stale_count": stale, "axis_count": len(axes), "axes": axes,
            "runs": list(hb["runs"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="print the snapshot")
    ap.parse_args(argv)
    print(json.dumps(collect(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
