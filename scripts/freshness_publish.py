#!/usr/bin/env python3
"""Publish the freshness snapshot to Postgres (opt-in).

A display on another machine reads schema `health` in the database named by
PGDATABASE. The machine running Dreamer may sleep; Postgres keeps the
last-known state readable while it is off, and v_status.laptop_reachable says
which of the two is happening.

Credentials: ~/.config/dreamer/postgres.env (override: DREAMER_PG_ENV), mode
600, keys PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD. No file means the
publisher is off. Nothing here ever fails a job: publishing always exits 0.

Only operational metadata is written. Assertion `detail` text
and exception messages are never sent.

Usage:
    freshness_publish.py                     # publish (always exits 0)
    freshness_publish.py --create-database   # one-time setup
    freshness_publish.py --init              # (re)create schema objects, publish
    freshness_publish.py --show              # read health.v_status back
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import signal
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import p, read_json  # noqa: E402
import freshness as F  # noqa: E402
import heartbeat as HB  # noqa: E402

ENV_DEFAULT = Path.home() / ".config" / "dreamer" / "postgres.env"
REQUIRED = ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
DEFAULT_PORT = 5432
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def env_path() -> Path:
    return Path(os.environ.get("DREAMER_PG_ENV") or ENV_DEFAULT)


def load_env(path: Path) -> dict | None:
    """KEY=VALUE per line. The value is everything after the FIRST '=', kept
    byte-for-byte apart from one pair of surrounding quotes — a '#' or '@'
    inside a password is data, not a comment or a separator."""
    if not path.exists():
        return None
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def conn_kwargs(env: dict, dbname: str | None = None) -> dict:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise KeyError(f"postgres.env is missing {', '.join(missing)}")
    # Separate keywords, never a DSN: '@' and '#' in the password break URL
    # parsing and can silently redirect the connection to localhost.
    return {"host": env["PGHOST"], "port": int(env.get("PGPORT") or DEFAULT_PORT),
            "dbname": dbname or env["PGDATABASE"], "user": env["PGUSER"],
            "password": env["PGPASSWORD"],
            "connect_timeout": int(F.cfg("freshness", "publish_timeout_seconds")),
            # Bound every statement and every lock wait server-side: a display
            # idling in a transaction on v_status must not hold a job in its
            # exit trap.
            "options": f"-c statement_timeout={_statement_ms()} -c lock_timeout={_statement_ms()}"}


def _statement_ms() -> int:
    return int(F.cfg("freshness", "publish_statement_timeout_seconds")) * 1000


def _connect(**kwargs):
    import psycopg2  # lazy: tests and unconfigured hosts never need it
    return psycopg2.connect(**kwargs)


def _aware(ts) -> _dt.datetime | None:
    """Naive local ISO string -> timezone-aware datetime for timestamptz."""
    if not ts:
        return None
    dt = ts if isinstance(ts, _dt.datetime) else _dt.datetime.fromisoformat(str(ts))
    return dt.astimezone()


def ddl() -> list[str]:
    reachable = int(F.cfg("freshness", "reachable_max_age_hours"))
    return [
        "CREATE SCHEMA IF NOT EXISTS health",
        """CREATE TABLE IF NOT EXISTS health.snapshot (
             id boolean PRIMARY KEY DEFAULT true CHECK (id),
             published_at timestamptz NOT NULL,
             generated_at timestamptz NOT NULL,
             host text NOT NULL,
             overall text NOT NULL,
             stale_count integer NOT NULL,
             axis_count integer NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS health.axis (
             axis text PRIMARY KEY,
             kind text NOT NULL,
             label text NOT NULL,
             status text NOT NULL,
             last_ran timestamptz,
             last_produced timestamptz,
             last_arrival timestamptz,
             last_outcome text,
             threshold_hours numeric,
             backlog integer,
             reason text NOT NULL,
             updated_at timestamptz NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS health.job_run (
             job text NOT NULL,
             started_at timestamptz,
             finished_at timestamptz NOT NULL,
             outcome text NOT NULL,
             produced boolean NOT NULL,
             PRIMARY KEY (job, finished_at))""",
        """CREATE TABLE IF NOT EXISTS health.assertion (
             name text PRIMARY KEY,
             ok boolean NOT NULL,
             severity text NOT NULL,
             checked_at timestamptz)""",
        f"""CREATE OR REPLACE VIEW health.v_status AS
             SELECT a.axis, a.kind, a.label, a.status, a.last_ran, a.last_produced,
                    a.last_arrival, a.last_outcome, a.threshold_hours, a.backlog,
                    a.reason, a.updated_at,
                    EXTRACT(EPOCH FROM (now() - CASE a.kind
                        WHEN 'source' THEN a.last_arrival
                        WHEN 'convergence' THEN COALESCE(a.last_arrival, a.last_ran)
                        WHEN 'output' THEN a.last_produced
                        ELSE a.last_ran END))::bigint AS age_seconds,
                    s.published_at, s.generated_at, s.host, s.overall, s.stale_count,
                    EXTRACT(EPOCH FROM (now() - s.published_at))::bigint AS snapshot_age_seconds,
                    (now() - s.published_at) < interval '{reachable} hours' AS laptop_reachable
               FROM health.axis a CROSS JOIN health.snapshot s""",
    ]


def publish(conn, snapshot: dict, health: dict, now: _dt.datetime,
            init: bool = False) -> None:
    """One transaction: a reader sees the previous complete snapshot or this
    one, never a mixture. DDL runs only when the schema is missing or `init`
    is asked (setup): CREATE OR REPLACE VIEW takes an exclusive lock, and a
    routine publish must not queue behind a reader for it."""
    stamp = _aware(now)
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('health.v_status')")
        row = cur.fetchone()
        if init or not row or row[0] is None:
            for stmt in ddl():
                cur.execute(stmt)
        cur.execute(
            """INSERT INTO health.snapshot
                 (id, published_at, generated_at, host, overall, stale_count, axis_count)
               VALUES (true, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                 published_at = EXCLUDED.published_at, generated_at = EXCLUDED.generated_at,
                 host = EXCLUDED.host, overall = EXCLUDED.overall,
                 stale_count = EXCLUDED.stale_count, axis_count = EXCLUDED.axis_count""",
            (stamp, _aware(snapshot["generated_at"]), socket.gethostname(),
             snapshot["overall"], snapshot["stale_count"], snapshot["axis_count"]))
        for a in snapshot["axes"]:
            cur.execute(
                """INSERT INTO health.axis
                     (axis, kind, label, status, last_ran, last_produced, last_arrival,
                      last_outcome, threshold_hours, backlog, reason, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (axis) DO UPDATE SET
                     kind = EXCLUDED.kind, label = EXCLUDED.label, status = EXCLUDED.status,
                     last_ran = EXCLUDED.last_ran, last_produced = EXCLUDED.last_produced,
                     last_arrival = EXCLUDED.last_arrival, last_outcome = EXCLUDED.last_outcome,
                     threshold_hours = EXCLUDED.threshold_hours, backlog = EXCLUDED.backlog,
                     reason = EXCLUDED.reason, updated_at = EXCLUDED.updated_at""",
                (a["axis"], a["kind"], a["label"], a["status"], _aware(a["last_ran"]),
                 _aware(a["last_produced"]), _aware(a["last_arrival"]), a["last_outcome"],
                 a["threshold_hours"], a["backlog"], a["reason"], stamp))
        cur.execute("DELETE FROM health.axis WHERE axis <> ALL(%s)",
                    ([a["axis"] for a in snapshot["axes"]],))
        cur.execute("DELETE FROM health.assertion")
        checked = _aware(health.get("checked_at"))
        for item in health.get("assertions") or []:
            # name / ok / severity only: `detail` can name a goal or a loop.
            cur.execute(
                "INSERT INTO health.assertion (name, ok, severity, checked_at) "
                "VALUES (%s, %s, %s, %s)",
                (item["name"], bool(item["ok"]), item["severity"], checked))
        for r in snapshot["runs"]:
            cur.execute(
                """INSERT INTO health.job_run (job, started_at, finished_at, outcome, produced)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (r["job"], _aware(r.get("started")), _aware(r["finished"]),
                 r["outcome"], bool(r.get("produced"))))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _with_current_publish(snapshot: dict, now: _dt.datetime) -> dict:
    """collect() reads the PREVIOUS publish from the heartbeat file. A snapshot
    being written right now is, by definition, a current publish — without
    this the first publish after the host was off would report itself stale."""
    axes = [dict(a, status="ok", reason="publishing now", last_ran=HB.stamp(now),
                 last_outcome="ok",
                 threshold_hours=float(F.cfg("freshness", "publish_max_age_hours")))
            if a["axis"] == "publish.postgres" else a for a in snapshot["axes"]]
    stale = sum(1 for a in axes if a["status"] in F.FAILING)
    return dict(snapshot, axes=axes, stale_count=stale,
                overall="stale" if stale else "ok")


def run(connect=None, meta=None, init: bool = False) -> str:
    """Publish once. Returns 'unconfigured', 'ok' or 'failed'; never raises."""
    env = load_env(env_path())
    if env is None:
        HB.record_publish(False, False, meta=meta)
        return "unconfigured"
    now = HB.clock()
    try:
        health = (read_json(p("meta") / "run-state.json", default={}) or {}).get("health") or {}
        snapshot = _with_current_publish(F.collect(now), now)
        conn = (connect or _connect)(**conn_kwargs(env))
        try:
            publish(conn, snapshot, health, now, init=init)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — a publish failure never fails a job
        HB.record_publish(True, False, error=type(exc).__name__,
                          at=HB.stamp(now), meta=meta)
        return "failed"
    HB.record_publish(True, True, at=HB.stamp(now), meta=meta)
    return "ok"


def ensure_database(connect=None) -> str:
    env = load_env(env_path())
    if env is None:
        raise FileNotFoundError(f"{env_path()} does not exist")
    name = env.get("PGDATABASE") or ""
    if not _IDENT.match(name):
        raise ValueError("PGDATABASE must be a plain lowercase identifier")
    conn = (connect or _connect)(**conn_kwargs(env, dbname="postgres"))
    try:
        conn.autocommit = True  # CREATE DATABASE cannot run in a transaction
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if cur.fetchone():
            return "exists"
        cur.execute(f'CREATE DATABASE "{name}"')
        return "created"
    finally:
        conn.close()


def show(connect=None) -> list[tuple]:
    env = load_env(env_path())
    if env is None:
        raise FileNotFoundError(f"{env_path()} does not exist")
    conn = (connect or _connect)(**conn_kwargs(env))
    try:
        cur = conn.cursor()
        cur.execute("SELECT axis, status, age_seconds, laptop_reachable, "
                    "snapshot_age_seconds, reason FROM health.v_status ORDER BY axis")
        return cur.fetchall()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--create-database", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--init", action="store_true",
                    help="(re)create the schema objects, then publish")
    ap.add_argument("--meta", default=None, help="heartbeat directory override (tests)")
    args = ap.parse_args(argv)
    when = HB.stamp(HB.clock())
    if args.create_database:
        try:
            print(f"[{when}] database: {ensure_database()}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[{when}] database: failed ({type(exc).__name__})")
            return 1
    if args.show:
        try:
            for row in show():
                print("  ".join("" if v is None else str(v) for v in row))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[{when}] show: failed ({type(exc).__name__})")
            return 1
    # Hard deadline: whatever the network or the server does, this process
    # ends. SIGALRM's default action terminates it; the next publish is a
    # complete snapshot, so nothing is lost but one update.
    signal.alarm(int(F.cfg("freshness", "publish_deadline_seconds")))
    result = run(meta=args.meta, init=args.init)
    signal.alarm(0)
    detail = "" if result != "failed" else f" ({HB.load(args.meta)['publish'].get('last_error')})"
    print(f"[{when}] publish: {'not configured' if result == 'unconfigured' else result}{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
