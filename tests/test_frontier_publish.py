#!/usr/bin/env python3
"""Frontier publisher (docs/frontier-schema.md): collecting the map from the vault,
then publishing it against a fake connection. Fixtures are synthetic temp dirs."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import dreamer_common as dc  # noqa: E402
import frontier as F  # noqa: E402
import frontier_publish as FP  # noqa: E402
import heartbeat as HB  # noqa: E402
from test_freshness_publish import MARK, FakeConn, PublishCase  # noqa: E402

AT = "2030-01-15T19:00:00"
CHAT = "sources/transcripts/2029/12/2029-12-21--test-tiles"
CHAT_NO_TITLE = "sources/transcripts/2029/12/2029-12-22--untitled-chat"

LESSON_PAGE = """## Lesson

The title line

A test tile asks for a sample [1].

## Claims

- c1 (fact): CLAIM-{m} [1]

Rejected draft (never published):

DRAFT-{m}

## Sources

[1] A sample is not a load — https://example.com/samples
[2] Owner note — owner:2029-12-21

## Web sources (untrusted)

### [1] Page — https://example.com/samples

PASSAGE-{m}

## Egress record

- fetch allowed: https://example.com/EGRESS-{m}
""".format(m=MARK)

REJECTED_PAGE = """## Lesson

## Claims

Rejected draft (never published):

REJDRAFT-{m}

## Sources

## Web sources (untrusted)

## Egress record
""".format(m=MARK)


class FrontierCase(PublishCase):
    def setUp(self) -> None:
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "frontier").mkdir(parents=True, exist_ok=True)
        dc.CFG["paths"]["frontier"] = str(self.vault / "frontier")
        self._orig_frontier = dict(dc.CFG.get("frontier") or {})
        dc.CFG["frontier"] = {"vault_lock_wait_seconds": 1, "cause_min_chars": 8,
                              "publish_lock_wait_seconds": 0.3, "publish_deadline_seconds": 30}
        os.environ.pop("DREAMER_LOCK_HELD", None)

    def tearDown(self) -> None:
        dc.CFG["frontier"] = self._orig_frontier
        super().tearDown()

    def enable(self, value: str = "1") -> None:
        self.configure()
        with self.envfile.open("a", encoding="utf-8") as fh:
            fh.write(f"DREAMER_PUBLISH_FRONTIER={value}\n")

    def goal(self, gid: str, title: str, status: str = "active") -> None:
        dc.write_page(self.vault / "goals" / f"{gid}.md",
                      {"type": "goal", "id": gid, "status": status, "title": title}, "body\n")

    def transcript(self, rel: str, title: str | None, date: str) -> None:
        f = self.vault / f"{rel}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        fm = {"date": date}
        if title:
            fm["title"] = title
        dc.write_page(f, fm, "**Human:** hi\n")

    def item(self, title="Test tiles", evidence=(), goals=()) -> F.Item:
        it = F.create_item(title, "a summary", "premortem", cause="created in test", at=AT)
        for kind, transcript, date, quote in evidence:
            it.add_evidence(kind, transcript, date, quote)
        for goal, relation in goals:
            it.set_goal(goal, relation, cause="because CAUSE-" + MARK)
        it.save()
        return it

    def lesson(self, it, status="ready", page=LESSON_PAGE, **kw) -> F.Lesson:
        les = F.create_lesson(it, goal="G0003", kind="new", cause="CAUSE-" + MARK, cause_rank=1,
                              status=status, body=page, check={"reason": "CHECK-" + MARK})
        for k, v in kw.items():
            setattr(les, k, v)
        les.save()
        return les


class CollectTest(FrontierCase):
    def test_every_table_shape(self) -> None:
        self.goal("G0003", "Ship it", "paused")
        self.transcript(CHAT, "Test tiles chat", "2029-12-21")
        it = self.item(evidence=[("explained", CHAT, "2029-12-21", "QUOTE-" + MARK)],
                       goals=[("G0003", "connected")])
        self.lesson(it, released_at="2030-01-15T20:00:00", aired_on="2030-01-16")
        c = FP.collect()
        self.assertEqual(c["goal"], [{"id": "G0003", "title": "Ship it", "status": "paused"}])
        item = c["item"][0]
        self.assertEqual((item["id"], item["quadrant"], item["last_evidence_on"]),
                         (it.id, "unknown_unknown", "2029-12-21"))
        self.assertEqual(c["item_goal"], [{"item_id": it.id, "goal_id": "G0003",
                                           "relation": "connected"}])
        self.assertEqual(c["evidence"], [{"item_id": it.id, "position": 1, "kind": "explained",
                                          "chat_title": "Test tiles chat", "on_date": "2029-12-21"}])
        self.assertEqual(c["move"][0]["from_quadrant"], "")
        les = c["lesson"][0]
        self.assertEqual((les["id"], les["title"], les["kind"], les["status"]),
                         (f"{it.id}-1", "The title line", "new", "ready"))
        self.assertEqual(les["body"], "A test tile asks for a sample [1].")
        self.assertEqual(les["sources"], [
            {"n": 1, "title": "A sample is not a load", "url": "https://example.com/samples"},
            {"n": 2, "title": "Owner note", "ref": "owner:2029-12-21"}])
        self.assertEqual(les["aired_on"], "2030-01-16")
        self.assertIsNone(les["got_it_at"])

    def test_a_lesson_without_a_lesson_section_is_titled_by_its_item(self) -> None:
        it = self.item()
        self.lesson(it, status="failed", page=REJECTED_PAGE)
        les = FP.collect()["lesson"][0]
        self.assertEqual((les["title"], les["body"]), ("Test tiles", ""))

    def test_item_lesson_skips_dropped_and_rejected(self) -> None:
        it = self.item()
        self.lesson(it)                                 # -1 ready
        self.lesson(it, status="rejected", page=REJECTED_PAGE)   # -2
        self.lesson(it, status="dropped")               # -3
        c = FP.collect()
        item = c["item"][0]
        self.assertEqual((item["lesson_id"], item["lesson_status"]), (f"{it.id}-1", "ready"))
        self.assertEqual([l["id"] for l in c["lesson"]], [f"{it.id}-1"])

    def test_newest_lesson_is_numeric_not_lexical(self) -> None:
        it = self.item()
        for _ in range(10):
            self.lesson(it, status="held")
        self.assertEqual(FP.collect()["item"][0]["lesson_id"], f"{it.id}-10")

    def test_item_with_only_dropped_lessons_has_null_lesson(self) -> None:
        it = self.item()
        self.lesson(it, status="dropped")
        item = FP.collect()["item"][0]
        self.assertEqual((item["lesson_id"], item["lesson_status"]), (None, None))

    def test_chat_title_falls_back_to_file_name_without_date_or_extension(self) -> None:
        self.transcript(CHAT_NO_TITLE, None, "2029-12-22")
        self.item(evidence=[("asked", CHAT_NO_TITLE, "2029-12-22", "q")])
        self.assertEqual(FP.collect()["evidence"][0]["chat_title"], "untitled-chat")

    def test_missing_transcript_still_falls_back_to_the_name(self) -> None:
        self.item(evidence=[("asked", CHAT, "2029-12-21", "q")])
        ev = FP.collect()["evidence"][0]
        self.assertEqual((ev["chat_title"], ev["on_date"]), ("test-tiles", "2029-12-21"))

    def test_evidence_is_by_chat_newest_first(self) -> None:
        self.transcript(CHAT, "Old", "2029-12-21")
        self.transcript(CHAT_NO_TITLE, "New", "2029-12-22")
        self.item(evidence=[("asked", CHAT, "2029-12-21", "one"),
                            ("explained", CHAT, "2029-12-21", "two"),
                            ("asked", CHAT_NO_TITLE, "2029-12-22", "three")])
        got = [(e["position"], e["chat_title"], e["kind"]) for e in FP.collect()["evidence"]]
        self.assertEqual(got, [(1, "New", "asked"), (2, "Old", "asked"), (3, "Old", "explained")])

    def test_tap_in_effect_and_undone_tap(self) -> None:
        it = self.item()
        les = self.lesson(it, status="aired", aired_on="2030-01-15", aired_quadrant="unknown_unknown")
        F.add_tap(les.id, "got_it", "t1", at="2030-01-15T21:00:00")
        F.add_tap(les.id, "dig_deeper", "t2", at="2030-01-15T22:00:00")
        row = FP.collect()["lesson"][0]
        self.assertTrue(row["got_it_at"].startswith("2030-01-15T21:00:00"))
        self.assertTrue(row["dig_deeper_at"].startswith("2030-01-15T22:00:00"))
        F.undo_tap(les.id, "got_it", "operator", "undone in test", at="2030-01-15T23:00:00")
        row = FP.collect()["lesson"][0]
        self.assertIsNone(row["got_it_at"])
        self.assertIsNotNone(row["dig_deeper_at"])

    def test_empty_vault_is_empty(self) -> None:
        c = FP.collect()
        self.assertTrue(all(v == [] for v in c.values()))


class PrivacyTest(FrontierCase):
    def test_no_private_text_reaches_any_statement_or_parameter(self) -> None:
        self.goal("G0003", "Ship it")
        self.transcript(CHAT, "Test tiles chat", "2029-12-21")
        it = self.item(evidence=[("explained", CHAT, "2029-12-21", "QUOTE-" + MARK)],
                       goals=[("G0003", "unconnected")])
        self.lesson(it)
        rej = self.item(title="Other")
        self.lesson(rej, status="rejected", page=REJECTED_PAGE)
        conn = Conn()
        FP.publish(conn, FP.collect(), init=True)
        blob = json.dumps(conn.calls, default=str)
        for marker in ("CLAIM-", "DRAFT-", "PASSAGE-", "EGRESS-", "REJDRAFT-", "QUOTE-",
                       "CAUSE-", "CHECK-"):
            self.assertNotIn(marker + MARK, blob, marker)
        self.assertNotIn(MARK, blob)
        self.assertIn("A test tile asks for a sample", blob)   # the lesson body does go

    def test_no_table_or_view_carries_a_count_or_a_withheld_column(self) -> None:
        text = " ".join(FP.ddl()).lower()
        for word in ("quote", "passage", "excerpt", "egress", "cause", "reason", "count("):
            self.assertNotIn(word, text)


class Conn(FakeConn):
    """FakeConn that also answers the stored-hash lookup."""

    def __init__(self, stored_hash: str | None = None, **kw):
        super().__init__(**kw)
        self.stored_hash = stored_hash

    def rows_for(self, sql):
        if "content_hash FROM frontier.snapshot" in sql:
            return [(self.stored_hash,)] if self.stored_hash else []
        return super().rows_for(sql)


class PublishTest(FrontierCase):
    def seed(self) -> dict:
        self.goal("G0003", "Ship it")
        it = self.item(goals=[("G0003", "connected")])
        self.lesson(it)
        return FP.collect()

    def test_new_content_replaces_every_table_in_one_transaction(self) -> None:
        c = self.seed()
        conn = Conn()
        self.assertEqual(FP.publish(conn, c), "published")
        trunc = conn.sql("TRUNCATE")
        self.assertEqual(len(trunc), 1)
        for t in ("goal", "item", "item_goal", "evidence", "move", "lesson"):
            self.assertIn(f"frontier.{t}", trunc[0][0])
            self.assertTrue(conn.sql(f"INSERT INTO frontier.{t} ") or t == "evidence")
        snap = conn.sql("INSERT INTO frontier.snapshot")[0]
        self.assertEqual(snap[1], (FP.content_hash(c),))
        self.assertIn("now()", snap[0])
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)
        self.assertEqual(conn.sql("DROP"), [])

    def test_unchanged_hash_moves_only_the_timestamp(self) -> None:
        c = self.seed()
        conn = Conn(stored_hash=FP.content_hash(c))
        self.assertEqual(FP.publish(conn, c), "unchanged")
        self.assertEqual(conn.sql("TRUNCATE"), [])
        self.assertEqual(conn.sql("INSERT"), [])
        self.assertEqual(len(conn.sql("UPDATE frontier.snapshot SET published_at = now()")), 1)
        self.assertTrue(conn.committed)

    def test_hash_ignores_nothing_in_the_content_and_changes_with_it(self) -> None:
        c = self.seed()
        first = FP.content_hash(c)
        self.assertEqual(first, FP.content_hash(FP.collect()))
        c["lesson"][0]["body"] += " more"
        self.assertNotEqual(first, FP.content_hash(c))

    def test_ddl_is_idempotent_and_never_drops(self) -> None:
        for stmt in FP.ddl():
            self.assertNotIn("DROP", stmt.upper())
            self.assertTrue("IF NOT EXISTS" in stmt or "OR REPLACE" in stmt, stmt)
        conn = Conn()
        FP.publish(conn, self.seed(), init=True)
        FP.publish(conn, self.seed(), init=True)
        self.assertEqual(conn.sql("DROP"), [])

    def test_a_routine_publish_runs_no_ddl_and_no_drop(self) -> None:
        conn = Conn()
        FP.publish(conn, self.seed())
        self.assertEqual(conn.sql("CREATE"), [])
        self.assertEqual(conn.sql("DROP"), [])

    def test_init_creates_the_schema_first(self) -> None:
        conn = Conn()
        FP.publish(conn, self.seed(), init=True)
        created = [s for s, _ in conn.calls if s.startswith("CREATE")]
        self.assertEqual(len(created), len(FP.ddl()))
        self.assertIn("CREATE SCHEMA IF NOT EXISTS frontier", created[0])

    def test_a_failure_mid_insert_rolls_back_and_raises(self) -> None:
        conn = Conn(fail_on="INSERT INTO frontier.item ")
        with self.assertRaises(RuntimeError):
            FP.publish(conn, self.seed())
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    def test_lesson_sources_are_sent_as_json(self) -> None:
        conn = Conn()
        FP.publish(conn, self.seed())
        sql, params = conn.sql("INSERT INTO frontier.lesson ")[0]
        self.assertIn("%s::jsonb", sql)
        self.assertEqual(json.loads(params[7])[0]["url"], "https://example.com/samples")

    def test_naive_local_times_go_out_with_the_local_offset(self) -> None:
        # The vault stores local wall-clock times without an offset; Postgres would
        # read them in the server's zone and shift every move and tap.
        self.goal("G0003", "Ship it")
        it = self.item(goals=[("G0003", "connected")])
        self.lesson(it, released_at=AT, aired_on="2030-01-16")
        conn = Conn()
        FP.publish(conn, FP.collect())
        _, move = conn.sql("INSERT INTO frontier.move ")[0]
        self.assertIsNotNone(move[1].tzinfo)
        self.assertEqual(move[1].replace(tzinfo=None).isoformat(timespec="seconds"), AT)
        _, les = conn.sql("INSERT INTO frontier.lesson ")[0]
        self.assertIsNotNone(les[8].tzinfo)          # released_at
        self.assertEqual(les[9], "2030-01-16")       # aired_on is a date; left as is


class ViewTest(FrontierCase):
    def view(self, name: str) -> str:
        return [s for s in FP.ddl() if f"VIEW frontier.{name} " in s][0]

    def test_movement_has_the_four_kinds_with_the_contract_times(self) -> None:
        v = " ".join(self.view("v_movement").split())
        for kind, source in (("released", "released_at"), ("aired", "aired_on"),
                             ("got_it", "got_it_at"), ("dig_deeper", "dig_deeper_at")):
            self.assertIn(f"'{kind}'", v)
            self.assertIn(f"{source} IS NOT NULL", v)
        self.assertIn("aired_on::timestamp AT TIME ZONE 'UTC'", v)
        self.assertEqual(v.count("UNION ALL"), 3)
        for col in ("lesson_id", "title", "at"):
            self.assertIn(col, v)

    def test_movement_reads_only_the_lesson_table_so_an_undone_tap_is_absent(self) -> None:
        # The view derives got_it/dig_deeper from lesson.*_at, which collect() sets
        # NULL for an undone tap (CollectTest.test_tap_in_effect_and_undone_tap).
        v = self.view("v_movement")
        self.assertEqual(v.count("FROM frontier.lesson"), 4)
        self.assertNotIn("frontier.move", v)

    def test_snapshot_view_has_the_age(self) -> None:
        v = " ".join(self.view("v_snapshot").split())
        self.assertIn("snapshot_age_seconds", v)
        self.assertIn("published_at", v)

    def test_goal_items_view_has_the_contract_columns(self) -> None:
        v = " ".join(self.view("v_goal_items").split())
        for col in ("goal_id", "goal_title", "goal_status", "item_id", "item_title", "summary",
                    "quadrant", "relation", "last_evidence_on", "lesson_id", "lesson_status"):
            self.assertIn(col, v)

    def test_movement_rows_for_the_view_follow_lesson_state(self) -> None:
        it = FrontierCase.item(self)
        les = self.lesson(it, status="released", released_at="2030-01-15T20:00:00")
        row = FP.collect()["lesson"][0]
        self.assertEqual((row["got_it_at"], row["dig_deeper_at"], row["aired_on"]),
                         (None, None, None))
        self.assertIsNotNone(row["released_at"])
        self.assertEqual(les.id, row["id"])


class RunTest(FrontierCase):
    def connector(self, conn):
        self.connects = []

        def connect(**kw):
            self.connects.append(kw)
            return conn
        return connect

    def test_no_env_file_is_not_configured(self) -> None:
        connect = self.connector(Conn())
        self.assertEqual(FP.run(connect=connect, meta=self.meta), "not configured")
        self.assertEqual(self.connects, [])
        self.assertEqual(HB.load(self.meta)["publish_frontier"], {"configured": False})

    def test_flag_off_publishes_nothing(self) -> None:
        self.configure()
        connect = self.connector(Conn())
        self.assertEqual(FP.run(connect=connect, meta=self.meta), "disabled")
        self.assertEqual(self.connects, [])

    def test_only_the_literal_1_enables(self) -> None:
        for value in ("true", "yes", "0", "", " 1x"):
            self.enable(value)
            connect = self.connector(Conn())
            self.assertEqual(FP.run(connect=connect, meta=self.meta), "disabled", value)
            self.assertEqual(self.connects, [], value)

    def test_the_board_flag_does_not_enable_it(self) -> None:
        self.configure()
        with self.envfile.open("a", encoding="utf-8") as fh:
            fh.write("DREAMER_PUBLISH_BOARD=1\n")
        self.assertEqual(FP.run(connect=self.connector(Conn()), meta=self.meta), "disabled")

    def test_enabled_publishes_and_records_ok(self) -> None:
        self.enable()
        conn = Conn()
        self.assertEqual(FP.run(connect=self.connector(conn), meta=self.meta), "published")
        self.assertEqual(self.connects[0]["dbname"], "dreamer")
        self.assertTrue(conn.closed)
        rec = HB.load(self.meta)["publish_frontier"]
        self.assertEqual((rec["configured"], "last_ok" in rec, "last_error" in rec),
                         (True, True, False))

    def test_a_failure_is_reported_by_class_name_only(self) -> None:
        self.enable()
        self.item()
        conn = Conn(fail_on="INSERT INTO frontier.item ")
        result = FP.run(connect=self.connector(conn), meta=self.meta)
        self.assertEqual(result, "failed (RuntimeError)")
        self.assertNotIn(MARK, result)
        rec = HB.load(self.meta)["publish_frontier"]
        self.assertEqual(rec["last_error"], "RuntimeError")
        self.assertNotIn(MARK, repr(rec))
        self.assertTrue(conn.closed)

    def test_a_connect_failure_never_raises(self) -> None:
        self.enable()

        def connect(**kw):
            raise OSError(f"no route {MARK}")
        self.assertEqual(FP.run(connect=connect, meta=self.meta), "failed (OSError)")

    def test_lock_held_by_another_process_publishes_nothing_and_records_nothing(self) -> None:
        self.enable()
        lockfile = Path(dc.CFG["paths"]["meta"]) / "wiki.lock"
        fh = open(lockfile, "a")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            connect = self.connector(Conn())
            self.assertEqual(FP.run(connect=connect, meta=self.meta), "skipped (busy)")
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
        self.assertEqual(self.connects, [])
        rec = HB.load(self.meta)["publish_frontier"]
        self.assertNotIn("last_error", rec)
        self.assertNotIn("last_ok", rec)
        state = Path(dc.CFG["paths"]["meta"]) / "run-state.json"
        events = (json.loads(state.read_text(encoding="utf-8")).get("events") or []) \
            if state.exists() else []
        self.assertEqual([e for e in events if "frontier" in json.dumps(e)], [])


class CliTest(FrontierCase):
    def run_main(self, *argv) -> tuple[int, str]:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = FP.main([*argv, "--meta", str(self.meta)])
        return rc, buf.getvalue()

    def test_a_disabled_publish_says_so_and_exits_0(self) -> None:
        self.configure()
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("frontier: disabled", out)

    def test_dry_run_connects_to_nothing(self) -> None:
        self.item()
        rc, out = self.run_main("--dry-run")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"frontier: dry run, hash [0-9a-f]{12}")
        self.assertEqual(HB.load(self.meta)["publish_frontier"], {})


if __name__ == "__main__":
    unittest.main()
