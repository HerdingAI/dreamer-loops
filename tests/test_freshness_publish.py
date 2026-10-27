#!/usr/bin/env python3
"""Freshness: the Postgres publisher, against a fake connection."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import freshness as F  # noqa: E402
import freshness_publish as P  # noqa: E402
import goals as G  # noqa: E402
import heartbeat as HB  # noqa: E402
from test_freshness import NOW, FreshCase, ago  # noqa: E402

MARK = "ZZPRIVATEZZ"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError(f"boom {MARK}")
        self.conn.calls.append((" ".join(sql.split()), params))
        self._rows = self.conn.rows_for(sql)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, fail_on=None, existing_db=False, schema_exists=False):
        self.calls, self.fail_on, self.existing_db = [], fail_on, existing_db
        self.schema_exists = schema_exists
        self.committed = self.rolled_back = self.closed = False
        self.autocommit = False

    def rows_for(self, sql):
        if "pg_database" in sql:
            return [(1,)] if self.existing_db else []
        if "to_regclass" in sql:
            return [("health.v_status",)] if self.schema_exists else [(None,)]
        return []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def sql(self, needle):
        return [(s, p) for s, p in self.calls if needle in s]


class PublishCase(FreshCase):
    def setUp(self) -> None:
        super().setUp()
        self.envfile = self.tmp / "postgres.env"
        self._orig_env = os.environ.get("DREAMER_PG_ENV")
        os.environ["DREAMER_PG_ENV"] = str(self.envfile)

    def tearDown(self) -> None:
        if self._orig_env is None:
            os.environ.pop("DREAMER_PG_ENV", None)
        else:
            os.environ["DREAMER_PG_ENV"] = self._orig_env
        super().tearDown()

    def configure(self, password="pw") -> None:
        self.envfile.write_text(
            "# dreamer freshness\nPGHOST=db.example.invalid\nPGPORT=5432\n"
            f"PGDATABASE=dreamer\nPGUSER=dreamer_user\nPGPASSWORD={password}\n", encoding="utf-8")

    def health(self) -> dict:
        return {"checked_at": ago(1), "assertions": [
            {"name": "index-fresh", "ok": True, "severity": "blocking", "detail": f"fine {MARK}"},
            {"name": "freshness-jobs", "ok": False, "severity": "degraded", "detail": f"bad {MARK}"}]}


class EnvTest(PublishCase):
    def test_missing_file_is_none(self) -> None:
        self.assertIsNone(P.load_env(P.env_path()))

    def test_password_with_special_characters_survives(self) -> None:
        for raw, want in (("p@ss#w0rd!x", "p@ss#w0rd!x"), ("'p@ss #w0rd'", "p@ss #w0rd"),
                          ('"a=b@c#d"', "a=b@c#d")):
            self.configure(password=raw)
            kw = P.conn_kwargs(P.load_env(P.env_path()))
            self.assertEqual(kw["password"], want)

    def test_connection_uses_separate_keywords_never_a_dsn(self) -> None:
        self.configure(password="p@ss#w0rd")
        kw = P.conn_kwargs(P.load_env(P.env_path()))
        self.assertEqual(set(kw), {"host", "port", "dbname", "user", "password", "connect_timeout",
                                   "options", "client_encoding"})
        # Never the server's default: on a SQL_ASCII database psycopg would
        # encode queries as ASCII and fail on the first non-ASCII character.
        self.assertEqual(kw["client_encoding"], "UTF8")
        self.assertEqual((kw["host"], kw["port"], kw["dbname"], kw["user"]),
                         ("db.example.invalid", 5432, "dreamer", "dreamer_user"))
        self.assertEqual(kw["connect_timeout"], int(F.cfg("freshness", "publish_timeout_seconds")))
        self.assertNotIn("dsn", kw)


class PublishTest(PublishCase):
    def publish(self, conn=None) -> FakeConn:
        conn = conn or FakeConn()
        P.publish(conn, F.collect(NOW), self.health(), NOW)
        return conn

    def test_ddl_is_idempotent(self) -> None:
        for stmt in P.ddl():
            self.assertTrue("IF NOT EXISTS" in stmt or "CREATE OR REPLACE VIEW" in stmt, stmt)
        self.assertTrue(any("health.v_status" in s for s in P.ddl()))

    def test_view_carries_the_configured_reachable_window(self) -> None:
        hours = int(F.cfg("freshness", "reachable_max_age_hours"))
        view = [s for s in P.ddl() if "health.v_status" in s][0]
        self.assertIn(f"interval '{hours} hours'", view)
        self.assertIn("laptop_reachable", view)
        self.assertIn("snapshot_age_seconds", view)

    def test_one_snapshot_row_is_upserted(self) -> None:
        conn = self.publish()
        rows = conn.sql("INSERT INTO health.snapshot")
        self.assertEqual(len(rows), 1)
        self.assertIn("ON CONFLICT (id) DO UPDATE", rows[0][0])
        self.assertTrue(conn.committed)

    def test_every_axis_is_upserted_and_removed_axes_are_pruned(self) -> None:
        conn = self.publish()
        ups = conn.sql("INSERT INTO health.axis")
        self.assertEqual(len(ups), len(F.AXES))
        self.assertIn("ON CONFLICT (axis) DO UPDATE", ups[0][0])
        prune = conn.sql("DELETE FROM health.axis")
        self.assertEqual(len(prune), 1)
        self.assertEqual(sorted(prune[0][1][0]), sorted(a["axis"] for a in F.AXES))

    def test_runs_are_inserted_idempotently(self) -> None:
        HB.record("nightly-extract", ago(2), "ok", finished=ago(1), meta=self.meta)
        conn = self.publish()
        runs = conn.sql("INSERT INTO health.job_run")
        self.assertEqual(len(runs), 1)
        self.assertIn("ON CONFLICT DO NOTHING", runs[0][0])

    def test_assertions_are_replaced_without_detail(self) -> None:
        conn = self.publish()
        self.assertEqual(len(conn.sql("DELETE FROM health.assertion")), 1)
        ins = conn.sql("INSERT INTO health.assertion")
        self.assertEqual(len(ins), 2)
        self.assertEqual(len(ins[0][1]), 4)  # name, ok, severity, checked_at

    def test_timestamps_reach_postgres_timezone_aware(self) -> None:
        conn = self.publish()
        published_at = conn.sql("INSERT INTO health.snapshot")[0][1][0]
        self.assertIsNotNone(published_at.tzinfo)

    def test_failure_rolls_back_and_raises(self) -> None:
        conn = FakeConn(fail_on="INSERT INTO health.axis")
        with self.assertRaises(RuntimeError):
            self.publish(conn)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)


class RunTest(PublishCase):
    def test_unconfigured_is_off_and_never_connects(self) -> None:
        connect = mock.Mock()
        self.assertEqual(P.run(connect=connect), "unconfigured")
        connect.assert_not_called()
        self.assertEqual(HB.load(self.meta)["publish"], {"configured": False})

    def test_no_env_file_exits_zero_and_the_axis_reads_disabled(self) -> None:
        self.assertFalse(self.envfile.exists())
        self.assertEqual(P.main(["--meta", str(self.meta)]), 0)
        a = self.axis("publish.postgres")
        self.assertEqual((a["status"], a["reason"]), ("info", "disabled"))

    def test_ok_publish_records_last_ok_and_closes(self) -> None:
        self.configure()
        conn = FakeConn()
        self.assertEqual(P.run(connect=lambda **kw: conn), "ok")
        self.assertTrue(conn.closed)
        self.assertEqual(HB.load(self.meta)["publish"]["last_ok"], NOW.isoformat())

    def test_connection_error_is_recorded_by_class_name_only(self) -> None:
        self.configure()

        def refuse(**kw):
            raise ConnectionError(f"could not reach db.example.invalid {MARK}")

        self.assertEqual(P.run(connect=refuse), "failed")
        pub = HB.load(self.meta)["publish"]
        self.assertEqual(pub["last_error"], "ConnectionError")
        self.assertNotIn(MARK, json.dumps(pub))

    def test_main_exits_zero_on_every_publish_path(self) -> None:
        self.assertEqual(P.main([]), 0)
        self.configure()
        with mock.patch.object(P, "_connect", side_effect=ConnectionError("down")):
            self.assertEqual(P.main([]), 0)


class EnsureDatabaseTest(PublishCase):
    def test_creates_when_absent_via_the_maintenance_database(self) -> None:
        self.configure()
        conn, seen = FakeConn(existing_db=False), {}

        def connect(**kw):
            seen.update(kw)
            return conn

        self.assertEqual(P.ensure_database(connect=connect), "created")
        self.assertEqual(seen["dbname"], "postgres")
        self.assertTrue(conn.autocommit)
        # Explicit encoding from template0: a bare CREATE DATABASE inherits
        # template1's, which on some servers is SQL_ASCII.
        self.assertEqual(
            len(conn.sql("""CREATE DATABASE "dreamer" ENCODING 'UTF8' TEMPLATE template0""")), 1)

    def test_leaves_an_existing_database_alone(self) -> None:
        self.configure()
        conn = FakeConn(existing_db=True)
        self.assertEqual(P.ensure_database(connect=lambda **kw: conn), "exists")
        self.assertEqual(conn.sql("CREATE DATABASE"), [])

    def test_refuses_a_database_name_that_is_not_a_plain_identifier(self) -> None:
        self.envfile.write_text("PGHOST=h\nPGDATABASE=dreamer; DROP\nPGUSER=u\nPGPASSWORD=p\n",
                                encoding="utf-8")
        with self.assertRaises(ValueError):
            P.ensure_database(connect=lambda **kw: FakeConn())


class PrivacyTest(PublishCase):
    def test_no_private_text_reaches_postgres(self) -> None:
        G.commit_goal(f"Goal {MARK}", f"Finish {MARK}", None, f"Why {MARK}",
                      [f"Step {MARK}"], f"yes make {MARK} a goal", "s1")
        self.transcript(f"2026-10-01--{MARK}.md", waiting_hours=72)
        (Path(P.p("conclusions")) / f"{MARK}.md").write_text("x", encoding="utf-8")
        self.write("owner-writes.json", [{"at": ago(1), "owner_quote": MARK,
                                          "confirmation": MARK, "target_id": "G0001"}])
        self.write("fold-pending.json", [{"enqueued_at": ago(1), "loop": MARK}])
        conn = FakeConn()
        P.publish(conn, F.collect(NOW), self.health(), NOW)
        self.assertNotIn(MARK, json.dumps(conn.calls, default=str))


class BoundedPublishTest(PublishCase):
    """A display holding a lock, or a dropped network, must never hold a job
    in its exit trap (freshness invariant: the exit trap never fails a job)."""

    def test_every_statement_and_lock_wait_is_bounded(self) -> None:
        self.configure()
        ms = int(F.cfg("freshness", "publish_statement_timeout_seconds")) * 1000
        options = P.conn_kwargs(P.load_env(P.env_path()))["options"]
        self.assertIn(f"-c statement_timeout={ms}", options)
        self.assertIn(f"-c lock_timeout={ms}", options)

    def test_main_arms_a_hard_deadline_before_publishing(self) -> None:
        with mock.patch.object(P.signal, "alarm") as alarm:
            P.main([])
        self.assertEqual(alarm.call_args_list[0],
                         mock.call(int(F.cfg("freshness", "publish_deadline_seconds"))))

    def test_no_ddl_when_the_schema_already_exists(self) -> None:
        conn = FakeConn(schema_exists=True)
        P.publish(conn, F.collect(NOW), self.health(), NOW)
        self.assertEqual(conn.sql("CREATE "), [])
        self.assertEqual(len(conn.sql("INSERT INTO health.snapshot")), 1)

    def test_ddl_runs_when_the_schema_is_missing_or_init_is_asked(self) -> None:
        missing = FakeConn(schema_exists=False)
        P.publish(missing, F.collect(NOW), self.health(), NOW)
        self.assertEqual(len(missing.sql("CREATE ")), len(P.ddl()))
        forced = FakeConn(schema_exists=True)
        P.publish(forced, F.collect(NOW), self.health(), NOW, init=True)
        self.assertEqual(len(forced.sql("CREATE ")), len(P.ddl()))


class CurrentPublishAxisTest(PublishCase):
    def test_the_snapshot_reports_this_publish_not_the_previous_one(self) -> None:
        self.configure()
        HB.record_publish(True, True, at=ago(5), meta=self.meta)  # previous publish was 5h ago (host was off)
        conn = FakeConn()
        self.assertEqual(P.run(connect=lambda **kw: conn), "ok")
        row = [p for s, p in conn.sql("INSERT INTO health.axis") if p[0] == "publish.postgres"][0]
        self.assertEqual(row[3], "ok")
        self.assertEqual(conn.sql("INSERT INTO health.snapshot")[0][1][3], "ok")  # overall


if __name__ == "__main__":
    unittest.main()
