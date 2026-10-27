#!/usr/bin/env python3
"""Board publisher: collecting and parsing, then publishing against a fake
connection."""

from __future__ import annotations

import datetime as _dt
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import board_publish as B  # noqa: E402
import heartbeat as HB  # noqa: E402
from test_freshness_publish import MARK, FakeConn, PublishCase  # noqa: E402

FENCE = "`" * 3  # spelled out so this file never contains a literal fence

BODY = f"""## The loop, restated

Restated text.

## What you previously concluded

PRIOR-{MARK}

## Web sources (untrusted)

{FENCE}
WEB-{MARK}
## Synthesis
FENCED-{MARK}
{FENCE}

## Evidence ledger

✓ accepted: 2 · ~ provisional: 1 · ✗ unsupported: 1

> LEDGERNOTE-{MARK}

## Synthesis

The answer text.

## Open sub-questions

- one?

## Egress record

EGRESS-{MARK}

## Something new

NEW-{MARK}
"""


class BoardCase(PublishCase):
    def loop(self, lid: str, title: str = "Problem title", count: int = 3,
             status: str = "paused", archived: bool = False) -> None:
        d = self.tmp / "vault" / ("archive" if archived else "loops")
        (d / f"{lid}.md").write_text(
            f'---\ntype: loop\nid: {lid}\nstatus: {status}\ntitle: "{title}"\n'
            f"recurrence_count: {count}\ntags: [architecture, tooling]\n---\n\nbody\n",
            encoding="utf-8")

    def conclusion(self, stem: str, loop: str = "L0001", title: str = "Answer title",
                   created: str = "2026-09-01", confidence: str = "medium",
                   superseded_by: str | None = None, body: str = BODY) -> Path:
        fm = [f"type: conclusion", f"loop: {loop}", f'title: "{title}"',
              f"created: {created}", f"confidence: {confidence}", "route: mixed"]
        if superseded_by:
            fm.append(f"superseded_by: conclusions/{superseded_by}")
        f = self.tmp / "vault" / "conclusions" / f"{stem}.md"
        f.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8")
        return f

    def enable(self, value: str = "1") -> None:
        self.configure()
        with self.envfile.open("a", encoding="utf-8") as fh:
            fh.write(f"DREAMER_PUBLISH_BOARD={value}\n")


class SectionTest(unittest.TestCase):
    def test_sections_are_split_on_level_two_headings(self) -> None:
        got = B.split_sections("intro\n\n## A\n\none\n\n## B\ntwo\n")
        self.assertEqual(got, {"A": "one", "B": "two"})

    def test_a_heading_inside_a_fence_is_not_a_section(self) -> None:
        got = B.split_sections(BODY)
        self.assertEqual(got["Synthesis"], "The answer text.")
        self.assertIn(f"FENCED-{MARK}", got["Web sources (untrusted)"])

    def test_a_tilde_fence_is_also_respected(self) -> None:
        got = B.split_sections("## A\n~~~\n## B\nx\n~~~\n\n## C\ny\n")
        self.assertEqual(set(got), {"A", "C"})

    def test_a_longer_fence_is_not_closed_by_a_shorter_one(self) -> None:
        long, short = "`" * 4, FENCE
        got = B.split_sections(f"## A\n{long}\n{short}\n## B\nx\n{long}\n\n## C\ny\n")
        self.assertEqual(set(got), {"A", "C"})

    def test_a_fence_line_with_trailing_text_does_not_close(self) -> None:
        got = B.split_sections(f"## A\n{FENCE}\n{FENCE} not a closer\n## B\nx\n{FENCE}\n\n## C\ny\n")
        self.assertEqual(set(got), {"A", "C"})

    def test_a_repeated_heading_keeps_the_first(self) -> None:
        got = B.split_sections("## A\nfirst\n\n## A\nsecond\n")
        self.assertEqual(got["A"], "first")


def web_page(injected: str, tail: str) -> str:
    """An UNFENCED web section, as apply_conclusion.render really writes it,
    whose claim carries `injected` on its own lines."""
    return ("## The loop, restated\n\nr\n\n## Web sources (untrusted)\n\n"
            f"- **[✓ accepted]** x\n{injected}\n  - source: `https://e.example/a`\n\n" + tail)


LEDGER_AND_REAL = "## Evidence ledger\n\n✓ accepted: 1\n\n## Synthesis\n\nREAL\n"


class PageStructureTest(unittest.TestCase):
    def test_a_well_formed_page_is_trusted(self) -> None:
        self.assertEqual(B.page_sections(BODY)["Synthesis"], "The answer text.")

    def test_a_forged_synthesis_in_unfenced_web_text_is_rejected(self) -> None:
        body = web_page(f"## Synthesis\nINJECTED-{MARK}", LEDGER_AND_REAL)
        self.assertIsNone(B.page_sections(body))

    def test_a_forged_ledger_in_unfenced_web_text_is_rejected(self) -> None:
        body = web_page("## Evidence ledger\n\n✓ accepted: 99", LEDGER_AND_REAL)
        self.assertIsNone(B.page_sections(body))

    def test_a_forged_section_ahead_of_the_ledger_is_rejected(self) -> None:
        # The real page has no open sub-questions, so the forged one is not a
        # duplicate — but it sits before the ledger, which the writer never does.
        body = web_page(f"## Open sub-questions\nINJECTED-{MARK}", LEDGER_AND_REAL)
        self.assertIsNone(B.page_sections(body))

    def test_a_forged_section_after_the_egress_record_is_rejected(self) -> None:
        body = ("## The loop, restated\n\nr\n\n## Synthesis\n\nREAL\n\n## Egress record\n\n"
                f"- query: `q`\n## Open sub-questions\nINJECTED-{MARK}\n")
        self.assertIsNone(B.page_sections(body))

    def test_a_web_section_with_no_ledger_is_rejected(self) -> None:
        body = web_page("more", "## Synthesis\n\nREAL\n")
        self.assertIsNone(B.page_sections(body))

    def test_unknown_headings_do_not_affect_trust(self) -> None:
        body = "## The loop, restated\n\nr\n\n## Notes\n\nn\n\n## Synthesis\n\nREAL\n\n## Notes\n\nm\n"
        self.assertEqual(B.page_sections(body)["Synthesis"], "REAL")


class WriterTest(unittest.TestCase):
    """The page writer itself must not let a single-line field plant a heading."""

    def render(self, sections: dict, **payload) -> str:
        import apply_conclusion as AC
        import vault as V
        return AC.render(V.Loop(id="L0001", title="t"),
                         {"title": "T", "route": "web", "confidence": "low",
                          "sections": sections, **payload}, [])

    def test_a_newline_in_a_web_claim_cannot_forge_a_section(self) -> None:
        body = self.render({
            "restated": "r", "synthesis": "REAL SYNTH",
            "web_says": [{"claim": f"x\n## Synthesis\nINJECTED-{MARK}",
                          "citation": "https://e.example/a\n## Evidence ledger\n✓ accepted: 99",
                          "support": "accepted"}]})
        got = B.page_sections(body)
        self.assertIsNotNone(got)
        self.assertEqual(got["Synthesis"], "REAL SYNTH")
        self.assertEqual(B.parse_ledger(got["Evidence ledger"])["ev_accepted"], 1)

    def test_a_newline_in_a_sub_question_or_egress_entry_cannot_forge_a_section(self) -> None:
        body = self.render(
            {"restated": "r", "synthesis": "REAL SYNTH",
             "open_sub_questions": [f"q\n## Synthesis\nINJECTED-{MARK}"]},
            web_queries=[f"q\n## Open sub-questions\nINJECTED-{MARK}"],
            fetched_urls=["https://e.example/a\n## Synthesis\nz"])
        got = B.page_sections(body)
        self.assertIsNotNone(got)
        self.assertEqual(got["Synthesis"], "REAL SYNTH")
        self.assertNotIn("\n## Synthesis\nINJECTED", body)


class LedgerTest(unittest.TestCase):
    def test_each_shape_found_in_the_vault(self) -> None:
        cases = {
            "✓ accepted: 3": (3, 0, 0, 0),
            "✓ accepted: 2 · ~ provisional: 4": (2, 4, 0, 0),
            "✓ accepted: 2 · ~ provisional: 2 · ✗ unsupported: 1": (2, 2, 0, 1),
            "✓ accepted: 1 · ~ provisional: 2 · ! contested: 5": (1, 2, 5, 0),
            "✓ accepted: 7 · ✗ unsupported: 2": (7, 0, 0, 2),
        }
        for line, want in cases.items():
            got = B.parse_ledger(f"\n{line}\n\n> a note with accepted: 99\n")
            self.assertEqual((got["ev_accepted"], got["ev_provisional"],
                              got["ev_contested"], got["ev_unsupported"]), want, line)

    def test_no_ledger_section_is_all_null(self) -> None:
        self.assertEqual(set(B.parse_ledger(None).values()), {None})

    def test_a_ledger_with_no_count_line_is_all_null(self) -> None:
        self.assertEqual(set(B.parse_ledger("\n> prose only\n").values()), {None})


class CollectTest(BoardCase):
    def test_one_pair_carries_every_column(self) -> None:
        self.loop("L0001", title="Why does X recur", count=4)
        self.conclusion("2026-09-01--answer", title="Do Y instead")
        rows, skipped = B.collect()
        self.assertEqual(skipped, 0)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(set(r), set(B.COLUMNS))
        self.assertEqual(
            (r["id"], r["loop_id"], r["problem"], r["answer"], r["concluded_on"],
             r["confidence"], r["route"], r["raised"], r["loop_status"], r["tags"]),
            ("2026-09-01--answer", "L0001", "Why does X recur", "Do Y instead",
             _dt.date(2026, 9, 1), "medium", "mixed", 4, "paused",
             ["architecture", "tooling"]))
        self.assertEqual((r["ev_accepted"], r["ev_provisional"], r["ev_contested"],
                          r["ev_unsupported"]), (2, 1, 0, 1))
        self.assertEqual(r["restated"], "Restated text.")
        self.assertEqual(r["synthesis"], "The answer text.")
        self.assertEqual(r["open_questions"], "- one?")

    def test_only_allow_listed_sections_are_published(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--answer")
        rows, _ = B.collect()
        blob = repr(rows)
        self.assertNotIn(MARK, blob)

    def test_superseded_conclusions_are_excluded(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-08-01--old", superseded_by="2026-09-01--new")
        self.conclusion("2026-09-01--new")
        rows, skipped = B.collect()
        self.assertEqual([r["id"] for r in rows], ["2026-09-01--new"])
        self.assertEqual(skipped, 0)

    def test_an_archived_loop_is_included(self) -> None:
        self.loop("L0002", archived=True, status="archived")
        self.conclusion("2026-09-01--answer", loop="L0002")
        rows, _ = B.collect()
        self.assertEqual(rows[0]["loop_status"], "archived")

    def test_a_resolved_loop_is_not_published(self) -> None:
        # "Resolved" is the owner saying the problem is done with; its answer
        # leaves the board. It is a deliberate removal, not a skip.
        self.loop("L0001", status="resolved")
        self.loop("L0002", status="paused")
        self.conclusion("2026-09-01--done-with", loop="L0001")
        self.conclusion("2026-09-02--still-live", loop="L0002")
        rows, skipped = B.collect()
        self.assertEqual([r["id"] for r in rows], ["2026-09-02--still-live"])
        self.assertEqual(skipped, 0)

    def test_reopening_a_resolved_loop_publishes_it_again(self) -> None:
        self.loop("L0001", status="resolved")
        self.conclusion("2026-09-01--answer")
        self.assertEqual(B.collect()[0], [])
        self.loop("L0001", status="open")
        self.assertEqual([r["id"] for r in B.collect()[0]], ["2026-09-01--answer"])

    def test_resolving_a_loop_changes_the_content_hash(self) -> None:
        self.loop("L0001")
        self.loop("L0002")
        self.conclusion("2026-09-01--a", loop="L0001")
        self.conclusion("2026-09-02--b", loop="L0002")
        before = B.content_hash(B.collect()[0])
        self.loop("L0001", status="resolved")
        self.assertNotEqual(before, B.content_hash(B.collect()[0]))

    def test_a_missing_loop_is_skipped_and_counted(self) -> None:
        self.conclusion("2026-09-01--orphan", loop="L0999")
        self.assertEqual(B.collect(), ([], 1))

    def test_an_untitled_conclusion_is_skipped_and_counted(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--blank", title="")
        self.assertEqual(B.collect(), ([], 1))

    def test_a_page_with_untrusted_structure_is_skipped_and_counted(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--forged",
                        body=web_page(f"## Synthesis\nINJECTED-{MARK}", LEDGER_AND_REAL))
        self.assertEqual(B.collect(), ([], 1))

    def test_a_page_with_no_synthesis_is_skipped_and_counted(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--empty", body="## The loop, restated\n\nr\n")
        self.assertEqual(B.collect(), ([], 1))

    def test_an_unclosed_fence_hides_the_synthesis_and_skips_the_page(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--open",
                        body=f"## The loop, restated\n\nr\n{FENCE}\n\n## Synthesis\n\nREAL\n")
        self.assertEqual(B.collect(), ([], 1))

    def test_an_untitled_loop_is_skipped_and_counted(self) -> None:
        self.loop("L0001", title="")
        self.conclusion("2026-09-01--answer")
        self.assertEqual(B.collect(), ([], 1))

    def test_two_current_conclusions_for_one_loop_keeps_the_later(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-08-01--first", created="2026-08-01")
        self.conclusion("2026-09-01--second", created="2026-09-01")
        rows, skipped = B.collect()
        self.assertEqual([r["id"] for r in rows], ["2026-09-01--second"])
        self.assertEqual(skipped, 0)

    def test_a_page_with_no_ledger_and_no_open_questions(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--thin",
                        body="## The loop, restated\n\nr\n\n## Synthesis\n\ns\n")
        r = B.collect()[0][0]
        self.assertIsNone(r["ev_accepted"])
        self.assertIsNone(r["open_questions"])

    def test_a_long_section_is_truncated_with_a_notice(self) -> None:
        self.loop("L0001")
        limit = int(B.F.cfg("board", "section_max_chars"))
        self.conclusion("2026-09-01--long",
                        body=f"## The loop, restated\n\nr\n\n## Synthesis\n\n{'x' * (limit + 50)}\n")
        s = B.collect()[0][0]["synthesis"]
        self.assertTrue(s.endswith(B.TRUNCATED))
        self.assertEqual(len(s), limit + len(B.TRUNCATED))

    def test_an_empty_vault_collects_nothing(self) -> None:
        self.assertEqual(B.collect(), ([], 0))

    def test_collect_touches_no_vault_file(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--answer")
        vault = self.tmp / "vault"
        before = {f: f.stat().st_mtime_ns for f in vault.rglob("*") if f.is_file()}
        B.collect()
        after = {f: f.stat().st_mtime_ns for f in vault.rglob("*") if f.is_file()}
        self.assertEqual(before, after)


class HashTest(BoardCase):
    def test_same_rows_same_hash_and_a_change_moves_it(self) -> None:
        self.loop("L0001")
        f = self.conclusion("2026-09-01--answer")
        first = B.content_hash(B.collect()[0])
        self.assertEqual(first, B.content_hash(B.collect()[0]))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        f.write_text(f.read_text(encoding="utf-8").replace("The answer text.", "A new answer."),
                     encoding="utf-8")
        self.assertNotEqual(first, B.content_hash(B.collect()[0]))


class BoardConn(FakeConn):
    """FakeConn that also answers the stored-hash lookup."""

    def __init__(self, stored_hash: str | None = None, **kw):
        super().__init__(**kw)
        self.stored_hash = stored_hash

    def rows_for(self, sql):
        if "content_hash FROM board.snapshot" in sql:
            return [(self.stored_hash,)] if self.stored_hash else []
        return super().rows_for(sql)


class PublishTest(BoardCase):
    def seed(self) -> list[dict]:
        self.loop("L0001")
        self.conclusion("2026-09-01--answer")
        return B.collect()[0]

    def test_new_content_replaces_rows_in_one_transaction(self) -> None:
        rows = self.seed()
        conn = BoardConn()
        self.assertEqual(B.publish(conn, rows, 0), "published")
        self.assertEqual(len(conn.sql("DELETE FROM board.pair")), 1)
        inserts = conn.sql("INSERT INTO board.pair")
        self.assertEqual(len(inserts), 1)
        self.assertEqual(len(inserts[0][1]), len(B.COLUMNS))
        self.assertEqual(inserts[0][1][0], "2026-09-01--answer")
        snap = conn.sql("INSERT INTO board.snapshot")[0]
        self.assertEqual(snap[1], (B.content_hash(rows), 1, 0))
        self.assertIn("now()", snap[0])
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)

    def test_unchanged_content_updates_only_the_timestamp(self) -> None:
        rows = self.seed()
        conn = BoardConn(stored_hash=B.content_hash(rows))
        self.assertEqual(B.publish(conn, rows, 2), "unchanged")
        self.assertEqual(conn.sql("DELETE FROM board.pair"), [])
        self.assertEqual(conn.sql("INSERT INTO board.pair"), [])
        upd = conn.sql("UPDATE board.snapshot")
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0][1], (2,))
        self.assertTrue(conn.committed)

    def test_a_routine_publish_runs_no_ddl(self) -> None:
        conn = BoardConn()
        B.publish(conn, self.seed(), 0)
        self.assertEqual(conn.sql("CREATE"), [])

    def test_init_creates_the_schema_first(self) -> None:
        conn = BoardConn()
        B.publish(conn, self.seed(), 0, init=True)
        created = [s for s, _ in conn.calls if s.startswith("CREATE")]
        self.assertEqual(len(created), len(B.ddl()))
        self.assertIn("CREATE SCHEMA IF NOT EXISTS board", created[0])

    def test_the_list_view_selects_no_body_column(self) -> None:
        view = [s for s in B.ddl() if "VIEW board.v_pairs" in s][0]
        for col in ("restated", "synthesis", "open_questions"):
            self.assertNotIn(col, view)

    def test_a_failure_mid_insert_rolls_back_and_raises(self) -> None:
        conn = BoardConn(fail_on="INSERT INTO board.pair")
        with self.assertRaises(RuntimeError):
            B.publish(conn, self.seed(), 0)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)


class RunTest(BoardCase):
    def connector(self, conn):
        self.connects = []

        def connect(**kw):
            self.connects.append(kw)
            return conn
        return connect

    def test_no_env_file_is_not_configured_and_never_connects(self) -> None:
        connect = self.connector(BoardConn())
        self.assertEqual(B.run(connect=connect, meta=self.meta), "not configured")
        self.assertEqual(self.connects, [])
        self.assertEqual(HB.load(self.meta)["publish_board"], {"configured": False})

    def test_flag_unset_is_disabled_and_never_connects(self) -> None:
        self.configure()
        connect = self.connector(BoardConn())
        self.assertEqual(B.run(connect=connect, meta=self.meta), "disabled")
        self.assertEqual(self.connects, [])
        self.assertEqual(HB.load(self.meta)["publish_board"], {"configured": False})

    def test_only_the_literal_1_enables(self) -> None:
        for value in ("true", "yes", "0", "", " 1x"):
            self.enable(value)
            connect = self.connector(BoardConn())
            self.assertEqual(B.run(connect=connect, meta=self.meta), "disabled", value)
            self.assertEqual(self.connects, [], value)

    def test_enabled_publishes_and_records_ok(self) -> None:
        self.enable()
        self.loop("L0001")
        self.conclusion("2026-09-01--answer")
        conn = BoardConn()
        self.assertEqual(B.run(connect=self.connector(conn), meta=self.meta), "published")
        self.assertEqual(self.connects[0]["dbname"], "dreamer")
        self.assertTrue(conn.closed)
        rec = HB.load(self.meta)["publish_board"]
        self.assertEqual((rec["configured"], "last_ok" in rec, "last_error" in rec),
                         (True, True, False))

    def test_an_empty_vault_publishes_zero_rows(self) -> None:
        self.enable()
        conn = BoardConn()
        self.assertEqual(B.run(connect=self.connector(conn), meta=self.meta), "published")
        self.assertEqual(conn.sql("INSERT INTO board.pair"), [])
        self.assertEqual(conn.sql("INSERT INTO board.snapshot")[0][1][1], 0)

    def test_a_failure_is_reported_by_class_name_only(self) -> None:
        self.enable()
        self.loop("L0001")
        self.conclusion("2026-09-01--answer")
        conn = BoardConn(fail_on="DELETE FROM board.pair")
        result = B.run(connect=self.connector(conn), meta=self.meta)
        self.assertEqual(result, "failed (RuntimeError)")
        self.assertNotIn(MARK, result)
        rec = HB.load(self.meta)["publish_board"]
        self.assertEqual(rec["last_error"], "RuntimeError")
        self.assertNotIn(MARK, repr(rec))
        self.assertTrue(conn.closed)

    def test_a_connect_failure_never_raises(self) -> None:
        self.enable()

        def connect(**kw):
            raise OSError(f"no route {MARK}")
        self.assertEqual(B.run(connect=connect, meta=self.meta), "failed (OSError)")

    def test_a_board_publish_leaves_the_health_record_alone(self) -> None:
        HB.record_publish(True, True, at="2026-10-03T11:12:00", meta=self.meta)
        self.enable()
        B.run(connect=self.connector(BoardConn()), meta=self.meta)
        self.assertEqual(HB.load(self.meta)["publish"],
                         {"configured": True, "last_ok": "2026-10-03T11:12:00"})


class CliTest(BoardCase):
    def run_main(self, *argv) -> tuple[int, str]:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = B.main([*argv, "--meta", str(self.meta)])
        return rc, buf.getvalue()

    def test_a_disabled_publish_says_so_and_exits_0(self) -> None:
        self.configure()
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("board: disabled", out)

    def test_dry_run_connects_to_nothing(self) -> None:
        self.loop("L0001")
        self.conclusion("2026-09-01--answer")
        rc, out = self.run_main("--dry-run")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"board: dry run, 1 pairs, 0 skipped, hash [0-9a-f]{12}")
        self.assertEqual(HB.load(self.meta)["publish_board"], {})


if __name__ == "__main__":
    unittest.main()
