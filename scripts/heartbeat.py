#!/usr/bin/env python3
"""Job heartbeats (freshness ledger).

One record per job in vault/.vault-meta/heartbeats.json: when it last ran,
when it last succeeded, when it last changed something. Written by the job
wrapper's EXIT trap (bin/_common.sh) and by the Postgres publisher — never by
the healthcheck, which only reads (CLAUDE.md rule 15).

Usage:
    heartbeat.py now
    heartbeat.py record --job J --started TS --outcome ok|failed|skipped
                        [--produced] [--meta DIR]
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, atomic_write_json, p, read_json  # noqa: E402

FILE = "heartbeats.json"
OUTCOMES = ("ok", "failed", "skipped")


def clock() -> _dt.datetime:
    """The freshness clock. DREAMER_NOW pins it exactly (tests); DREAMER_TODAY
    moves only the date, so the simulated week ages heartbeats day by day."""
    forced = os.environ.get("DREAMER_NOW")
    if forced:
        return _dt.datetime.fromisoformat(forced)
    now = _dt.datetime.now().replace(microsecond=0)
    day = os.environ.get("DREAMER_TODAY")
    if day:
        return _dt.datetime.combine(_dt.date.fromisoformat(day), now.time())
    return now


def stamp(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec="seconds")


def meta_dir(meta=None) -> Path:
    return Path(meta) if meta else p("meta")


def load(meta=None) -> dict:
    """Never raises: a missing, truncated or wrong-shaped file reads as empty.
    Heartbeats are regenerable — the next run of each job rewrites its own."""
    data = read_json(meta_dir(meta) / FILE, default={})
    if not isinstance(data, dict):
        data = {}
    out = {"jobs": data.get("jobs"), "runs": data.get("runs"),
           "publish": data.get("publish")}
    if not isinstance(out["jobs"], dict):
        out["jobs"] = {}
    if not isinstance(out["runs"], list):
        out["runs"] = []
    if not isinstance(out["publish"], dict):
        out["publish"] = {}
    return out


@contextlib.contextmanager
def _locked(meta=None):
    """Its own lock, not wiki.lock: the trap fires while the job may still
    hold wiki.lock, and jobs that take no vault lock (ingest-cc, night-cycle)
    must not wait behind an hours-long research leg to say they finished."""
    d = meta_dir(meta)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "heartbeats.lock", "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def record(job: str, started: str, outcome: str, produced: bool = False,
           finished: str | None = None, meta=None) -> dict:
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown heartbeat outcome {outcome!r}")
    finished = finished or stamp(clock())
    keep = int(CFG["freshness"]["run_history_keep"])
    with _locked(meta):
        data = load(meta)
        j = data["jobs"].setdefault(job, {})
        if outcome == "skipped":
            # Neither a run nor a failure: no clock moves.
            j["last_skipped"] = finished
        else:
            j["last_started"] = started
            j["last_finished"] = finished
            j["last_outcome"] = outcome
            if outcome == "ok":
                j["last_ok"] = finished
            if produced:
                j["last_produced"] = finished
        data["runs"].append({"job": job, "started": started, "finished": finished,
                             "outcome": outcome, "produced": bool(produced)})
        data["runs"] = data["runs"][-keep:]
        atomic_write_json(meta_dir(meta) / FILE, data)
    return data


def record_publish(configured: bool, ok: bool, error: str | None = None,
                   at: str | None = None, meta=None) -> dict:
    """`error` is an exception CLASS NAME only — a message could carry a
    hostname or a credential fragment into the heartbeat file."""
    at = at or stamp(clock())
    with _locked(meta):
        data = load(meta)
        before = dict(data["publish"])
        if not configured:
            data["publish"] = {"configured": False}
        else:
            pub = {"configured": True}
            for k in ("last_ok", "last_error", "last_error_at"):
                if k in before:
                    pub[k] = before[k]
            if ok:
                pub["last_ok"] = at
                pub.pop("last_error", None)
                pub.pop("last_error_at", None)
            else:
                pub["last_error"] = error or "Error"
                pub["last_error_at"] = at
            data["publish"] = pub
        # The hourly unconfigured publish must not dirty the tree every hour.
        if data["publish"] != before or not (meta_dir(meta) / FILE).exists():
            atomic_write_json(meta_dir(meta) / FILE, data)
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now")
    rec = sub.add_parser("record")
    rec.add_argument("--job", required=True)
    rec.add_argument("--started", required=True)
    rec.add_argument("--outcome", required=True, choices=OUTCOMES)
    rec.add_argument("--produced", action="store_true")
    rec.add_argument("--meta", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "now":
        print(stamp(clock()))
        return 0
    record(args.job, args.started, args.outcome, produced=args.produced,
           meta=args.meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
