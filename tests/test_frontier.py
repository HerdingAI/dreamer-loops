#!/usr/bin/env python3
"""Unit tests for the frontier store and quadrant rule."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import frontier as F  # noqa: E402
import goals as G  # noqa: E402
import questions as Q  # noqa: E402

AT = "2030-01-15T19:00:00"
ACTIVE = {"G0003"}


class FrontierCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-fr-"))
        for n in ("frontier", "goals", "meta"):
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        self._orig_frontier = dict(dc.CFG.get("frontier") or {})
        self._orig_mcp = dict(dc.CFG.get("mcp") or {})
        dc.CFG["paths"].update({
            "frontier": str(self.tmp / "frontier"),
            "goals": str(self.tmp / "goals"),
            "meta": str(self.tmp / "meta"),
        })
        dc.CFG["frontier"] = {"vault_lock_wait_seconds": 1, "cause_min_chars": 8}
        dc.CFG["mcp"] = {"owner_write_quote_min_chars": 8, "owner_write_verify_lag_days": 3}

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig_paths)
        dc.CFG["frontier"] = self._orig_frontier
        dc.CFG["mcp"] = self._orig_mcp
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ---------------------------------------------------------------
    def item(self, origin="premortem", title="Wedging technique", relation=None) -> F.Item:
        it = F.create_item(title, "a summary", origin, cause="created in test", at=AT)
        if relation:
            it.set_goal("G0003", relation)
            it.save()
        return it

    def evidence(self, it, kind="explained", transcript="t1", quote="in my own words"):
        return it.add_evidence(kind, transcript, "2029-12-21", quote)

    def aired_lesson(self, it) -> F.Lesson:
        les = F.create_lesson(it, goal="G0003", kind="new", cause="gap", cause_rank=1)
        F.mark_aired(les.id, aired_on="2030-01-16", at=AT)
        return F.Lesson.from_path(F.lesson_path(les.id))


class QuadrantDerivationTest(FrontierCase):
    def test_premortem_without_evidence_is_unknown_unknown(self) -> None:
        it = self.item()
        self.assertEqual(it.quadrant, "unknown_unknown")
        self.assertEqual(it.moves[0]["from"], "")

    def test_explained_evidence_moves_unknown_unknown_to_known_known(self) -> None:
        it = self.item()
        self.evidence(it)
        move = F.apply_quadrant(it, "chat t1", ACTIVE, at=AT)
        self.assertEqual(it.quadrant, "known_known")
        self.assertEqual((move["from"], move["to"]), ("unknown_unknown", "known_known"))
        self.assertEqual(move["cause"], "chat t1")

    def test_assistant_exposure_never_known_known(self) -> None:
        it = self.item(origin="map")
        self.evidence(it, kind="exposure", quote="the assistant explained it")
        F.apply_quadrant(it, "chat t1", ACTIVE, at=AT)
        self.assertEqual(it.quadrant, "unknown_unknown")

    def test_reconstructed_gives_known_unknown(self) -> None:
        it = self.item(origin="map")
        self.evidence(it, kind="reconstructed")
        F.apply_quadrant(it, "chat t1", ACTIVE, at=AT)
        self.assertEqual(it.quadrant, "known_unknown")

    def test_asked_gives_known_unknown(self) -> None:
        it = self.item(origin="map")
        self.evidence(it, kind="asked")
        self.assertEqual(F.derive_quadrant(it, ACTIVE), "known_unknown")

    def test_loop_link_gives_known_unknown(self) -> None:
        it = self.item(origin="map")
        it.loops.append("L0042")
        self.assertEqual(F.derive_quadrant(it, ACTIVE), "known_unknown")

    def test_explained_with_unconnected_active_goal_is_unknown_known(self) -> None:
        it = self.item(origin="map", relation="unconnected")
        self.evidence(it)
        self.assertEqual(F.derive_quadrant(it, ACTIVE), "unknown_known")

    def test_explained_with_connected_goal_is_known_known(self) -> None:
        it = self.item(origin="map", relation="connected")
        self.evidence(it, kind="decided")
        self.assertEqual(F.derive_quadrant(it, ACTIVE), "known_known")

    def test_paused_goal_relation_does_not_create_unknown_known(self) -> None:
        it = self.item(origin="map", relation="unconnected")
        self.evidence(it, kind="applied")
        self.assertEqual(F.derive_quadrant(it, set()), "known_known")  # G0003 not active

    def test_active_goal_ids_reads_only_active_goals(self) -> None:
        for gid, status in (("G0001", "active"), ("G0002", "paused"), ("G0003", "abandoned")):
            G.Goal(id=gid, title=gid, status=status).save()
        self.assertEqual(F.active_goal_ids(), {"G0001"})

    def test_map_origin_with_no_owner_evidence_is_unknown_unknown(self) -> None:
        it = self.item(origin="map")
        self.assertEqual(F.derive_quadrant(it, ACTIVE), "unknown_unknown")


class AiredPinAndTapTest(FrontierCase):
    def test_aired_lesson_pins_quadrant_against_later_explanation(self) -> None:
        it = self.item()
        self.aired_lesson(it)
        it = F.Item.from_path(F.item_path(it.id))
        self.evidence(it)
        self.assertIsNone(F.apply_quadrant(it, "chat t1", ACTIVE, at=AT))
        self.assertEqual(it.quadrant, "unknown_unknown")
        self.assertEqual(len(it.evidence), 1)  # evidence still appended

    def test_dig_deeper_keeps_the_pin(self) -> None:
        it = self.item()
        les = self.aired_lesson(it)
        F.add_tap(les.id, "dig_deeper", "tap-1", at=AT)
        it = F.Item.from_path(F.item_path(it.id))
        self.evidence(it)
        self.assertIsNone(F.apply_quadrant(it, "chat t1", ACTIVE, at=AT))
        self.assertEqual(it.quadrant, "unknown_unknown")
        self.assertEqual(F.Lesson.from_path(F.lesson_path(les.id)).status, "dig_deeper")

    def test_got_it_gives_known_known_and_tombstone_restores(self) -> None:
        it = self.item()
        les = self.aired_lesson(it)
        F.add_tap(les.id, "got_it", "tap-1", at=AT)
        it = F.Item.from_path(F.item_path(it.id))
        self.assertEqual(it.quadrant, "known_known")
        self.assertEqual(it.moves[-1]["to"], "known_known")
        self.assertTrue(it.moves[-1]["cause"])

        F.undo_tap(les.id, "got_it", "operator", cause="operator test tap", at=AT)
        it = F.Item.from_path(F.item_path(it.id))
        self.assertEqual(it.quadrant, "unknown_unknown")
        self.assertEqual((it.moves[-1]["from"], it.moves[-1]["to"]), ("known_known", "unknown_unknown"))
        self.assertIn("operator test tap", it.moves[-1]["cause"])
        self.assertEqual(F.Lesson.from_path(F.lesson_path(les.id)).status, "aired")

    def test_retap_after_undo_counts_again(self) -> None:
        it = self.item()
        les = self.aired_lesson(it)
        F.add_tap(les.id, "got_it", "tap-1", at=AT)
        F.undo_tap(les.id, "got_it", "operator", cause="operator test tap", at=AT)
        F.add_tap(les.id, "got_it", "tap-2", at="2030-01-15T19:00:01")
        self.assertEqual(F.Item.from_path(F.item_path(it.id)).quadrant, "known_known")

    def test_duplicate_tap_id_is_ignored(self) -> None:
        it = self.item()
        les = self.aired_lesson(it)
        F.add_tap(les.id, "got_it", "tap-1", at=AT)
        F.add_tap(les.id, "got_it", "tap-1", at=AT)
        self.assertEqual(len(F.Lesson.from_path(F.lesson_path(les.id)).taps), 1)

    def test_replay_after_crash_between_lesson_save_and_item_sync_moves_item(self) -> None:
        it = self.item()
        les = self.aired_lesson(it)
        # crash simulation: tap landed on the lesson page, item never synced
        les.taps.append({"id": "tap-1", "kind": "got_it", "at": AT})
        les.refresh_status()
        les.save()
        self.assertEqual(F.Item.from_path(F.item_path(it.id)).quadrant, "unknown_unknown")
        F.add_tap(les.id, "got_it", "tap-1", at=AT)
        back = F.Item.from_path(F.item_path(it.id))
        self.assertEqual(back.quadrant, "known_known")
        self.assertEqual(sum(1 for m in back.moves if m["to"] == "known_known"), 1)
        self.assertEqual(len(F.Lesson.from_path(F.lesson_path(les.id)).taps), 1)

    def test_clean_duplicate_replay_adds_no_second_move(self) -> None:
        it = self.item()
        les = self.aired_lesson(it)
        F.add_tap(les.id, "got_it", "tap-1", at=AT)
        n = len(F.Item.from_path(F.item_path(it.id)).moves)
        self.assertIsNone(F.add_tap(les.id, "got_it", "tap-1", at=AT))
        self.assertEqual(len(F.Item.from_path(F.item_path(it.id)).moves), n)

    def test_illegal_tap_kind_rejected(self) -> None:
        it = self.item()
        les = self.aired_lesson(it)
        with self.assertRaises(F.FrontierError):
            F.add_tap(les.id, "like", "tap-1", at=AT)


class EvidenceAndMoveTest(FrontierCase):
    def test_same_transcript_and_quote_added_twice_yields_one_entry(self) -> None:
        it = self.item()
        self.assertTrue(self.evidence(it, quote="I  explained it\nTHIS way"))
        self.assertFalse(self.evidence(it, quote="i explained it this way"))
        self.assertEqual(len(it.evidence), 1)

    def test_same_quote_other_transcript_is_new_evidence(self) -> None:
        it = self.item()
        self.evidence(it, transcript="t1")
        self.assertTrue(self.evidence(it, transcript="t2"))

    def test_illegal_evidence_kind_rejected(self) -> None:
        it = self.item()
        with self.assertRaises(F.FrontierError):
            self.evidence(it, kind="vibes")

    def test_no_move_when_nothing_changes(self) -> None:
        it = self.item()
        n = len(it.moves)
        self.assertIsNone(F.apply_quadrant(it, "nothing new", ACTIVE, at=AT))
        self.assertEqual(len(it.moves), n)

    def test_move_requires_a_cause(self) -> None:
        it = self.item()
        self.evidence(it)
        with self.assertRaises(F.FrontierError):
            F.apply_quadrant(it, "  ", ACTIVE, at=AT)

    def test_failed_write_leaves_previous_page_intact(self) -> None:
        it = self.item()
        before = F.item_path(it.id).read_text()
        self.evidence(it)
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                it.save()
        self.assertEqual(F.item_path(it.id).read_text(), before)
        self.assertEqual([x.name for x in F.item_path(it.id).parent.iterdir()],
                         [F.item_path(it.id).name])  # no stray temp file


class StoreTest(FrontierCase):
    def test_item_round_trips_every_field(self) -> None:
        it = self.item(origin="map")
        self.evidence(it, kind="explained", transcript="t1", quote="q one")
        it.set_goal("G0003", "unconnected", cause="never linked")
        it.loops.append("L0001")
        F.apply_quadrant(it, "chat t1", ACTIVE, at=AT)
        it.lessons.append("K7001-1")
        it.save()
        back = F.Item.from_path(F.item_path(it.id))
        for f in ("id", "title", "summary", "quadrant", "origin", "evidence", "goals",
                  "loops", "moves", "lessons"):
            self.assertEqual(getattr(back, f), getattr(it, f), f)

    def test_lesson_round_trips_every_field(self) -> None:
        it = self.item()
        les = F.create_lesson(it, goal="G0003", kind="follow_up", origin_tap="tap-9",
                              cause="gap", cause_rank=2)
        les.status = "released"
        les.released_at = "2030-01-16T19:40:00+05:00"
        les.body_hash = "abc"
        les.parse_attempts = 2
        les.check = {"verdict": "pass"}
        les.taps.append({"id": "t", "kind": "got_it", "at": AT})
        les.tombstones.append({"kind": "got_it", "at": AT, "actor": "operator",
                               "owner_write": None, "cause": "x"})
        les.body = ("## Lesson\nbody\n\n## Claims\n\n## Sources\n\n"
                    "## Web sources (untrusted)\n\n## Egress record\n")
        les.save()
        back = F.Lesson.from_path(F.lesson_path(les.id))
        for f in ("id", "item", "goal", "kind", "origin_tap", "cause", "cause_rank", "status",
                  "released_at", "body_hash", "aired_on", "taps", "tombstones",
                  "parse_attempts", "check"):
            self.assertEqual(getattr(back, f), getattr(les, f), f)
        self.assertEqual(back.body.strip(), les.body.strip())

    def test_default_lesson_body_has_the_five_sections(self) -> None:
        les = F.create_lesson(self.item(), goal="G0003", kind="new", cause="gap", cause_rank=1)
        body = F.Lesson.from_path(F.lesson_path(les.id)).body
        for h in ("## Lesson", "## Claims", "## Sources", "## Web sources (untrusted)",
                  "## Egress record"):
            self.assertIn(h, body)

    def test_item_ids_are_monotonic_and_counted_from_lesson_pages_too(self) -> None:
        a, b = self.item(), self.item(title="Other")
        self.assertRegex(a.id, r"^K\d{4}$")
        self.assertEqual(int(b.id[1:]), int(a.id[1:]) + 1)
        after = f"K{int(b.id[1:]) + 1:04d}"
        self.assertEqual(F.next_item_id(), after)
        # a lesson page that outlives its item still holds the id
        F.create_lesson(b, goal="G0003", kind="new", cause="gap", cause_rank=1)
        F.item_path(b.id).unlink()
        self.assertEqual(F.next_item_id(), after)

    def test_lesson_ids_follow_the_item(self) -> None:
        it = self.item()
        l1 = F.create_lesson(it, goal="G0003", kind="new", cause="gap", cause_rank=1)
        l2 = F.create_lesson(it, goal="G0003", kind="follow_up", cause="gap", cause_rank=1)
        self.assertEqual((l1.id, l2.id), (f"{it.id}-1", f"{it.id}-2"))
        self.assertEqual(F.Item.from_path(F.item_path(it.id)).lessons, [l1.id, l2.id])

    def test_illegal_quadrant_or_origin_rejected_on_save(self) -> None:
        it = self.item()
        it.quadrant = "nope"
        with self.assertRaises(F.FrontierError):
            it.save()
        with self.assertRaises(F.FrontierError):
            F.create_item("t", "s", "elsewhere", cause="c")


class TapLessonTest(FrontierCase):
    """frontier.tap_lesson: the owner's tap made through Claude (rule 16)."""

    QUOTE = "I already know this one"

    def setUp(self) -> None:
        super().setUp()
        self.it = self.item()
        self.les = self.aired_lesson(self.it)

    def tap(self, kind="got_it", **over):
        args = dict(owner_quote=self.QUOTE, session_id="s1")
        args.update(over)
        return F.tap_lesson(self.les.id, kind, **args)

    def test_got_it_moves_the_item_exactly_as_a_phone_tap_does(self) -> None:
        res = self.tap()
        self.assertEqual(res["move"]["to"], "known_known")
        # The reference: the same lesson tapped by add_tap on a twin item.
        twin = self.item(title="Twin")
        twin_les = self.aired_lesson(twin)
        via_consumer = F.add_tap(twin_les.id, "got_it", "7")
        mine = F.Item.from_path(F.item_path(self.it.id))
        theirs = F.Item.from_path(F.item_path(twin.id))
        self.assertEqual(mine.quadrant, theirs.quadrant)
        self.assertEqual((res["move"]["from"], res["move"]["to"]), (via_consumer["from"], via_consumer["to"]))
        les = F.Lesson.from_path(F.lesson_path(self.les.id))
        self.assertEqual(les.status, "got_it")

    def test_tap_id_is_prefixed_so_it_cannot_collide_with_the_shows_integers(self) -> None:
        first = self.tap("dig_deeper")["tap"]
        self.assertEqual(first, f"mcp-{self.les.id}-1")  # unique across the item's lessons
        # A second kind gets a fresh id; a got_it after dig_deeper is a different tap.
        second = self.tap("got_it")["tap"]
        self.assertNotEqual(first, second)
        self.assertEqual(second, f"mcp-{self.les.id}-2")

    def test_owner_write_is_recorded_with_quote_and_session(self) -> None:
        self.tap()
        writes = Q.load_owner_writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual((writes[0]["tool"], writes[0]["target_id"], writes[0]["owner_quote"],
                          writes[0]["session_id"]), ("tap_lesson", self.les.id, self.QUOTE, "s1"))

    def test_refused_without_quote_session_or_on_a_bad_kind(self) -> None:
        for over in ({"owner_quote": ""}, {"owner_quote": "ok"}, {"session_id": " "}):
            with self.assertRaises(F.FrontierError):
                self.tap(**over)
        with self.assertRaises(F.FrontierError):
            self.tap("love_it")
        self.assertEqual(Q.load_owner_writes(), [])
        self.assertEqual(F.Lesson.from_path(F.lesson_path(self.les.id)).taps, [])

    def test_refused_on_a_lesson_that_never_reached_the_show(self) -> None:
        ready = F.create_lesson(self.it, goal="G0003", kind="new", cause="gap", cause_rank=1)
        with self.assertRaises(F.FrontierError):
            F.tap_lesson(ready.id, "got_it", owner_quote=self.QUOTE, session_id="s1")

    def test_a_second_tap_of_a_kind_in_effect_is_refused(self) -> None:
        self.tap()
        with self.assertRaises(F.FrontierError):
            self.tap()
        self.assertEqual(len(Q.load_owner_writes()), 1)

    def test_a_tap_right_after_an_undo_is_not_swallowed_by_it(self) -> None:
        self.tap()
        F.undo_tap(self.les.id, "got_it", "owner", cause="reversed the decision",
                   owner_quote="I did not get that", session_id="s1")
        self.tap()  # same second as the undo's tombstone
        les = F.Lesson.from_path(F.lesson_path(self.les.id))
        self.assertTrue(les.tap_in_effect("got_it"))


class UndoTapTest(FrontierCase):
    def setUp(self) -> None:
        super().setUp()
        self.it = self.item()
        self.les = self.aired_lesson(self.it)
        F.add_tap(self.les.id, "got_it", "tap-1", at=AT)

    def test_operator_undo_requires_a_cause(self) -> None:
        for bad in ("", "  ", "x"):
            with self.assertRaises(F.FrontierError):
                F.undo_tap(self.les.id, "got_it", "operator", cause=bad, at=AT)
        self.assertEqual(F.Lesson.from_path(F.lesson_path(self.les.id)).tombstones, [])

    def test_operator_undo_records_actor_and_writes_no_owner_ledger(self) -> None:
        F.undo_tap(self.les.id, "got_it", "operator", cause="operator test tap", at=AT)
        les = F.Lesson.from_path(F.lesson_path(self.les.id))
        ts = les.tombstones[0]
        self.assertEqual((ts["kind"], ts["actor"], ts["owner_write"]), ("got_it", "operator", None))
        self.assertEqual(ts["cause"], "operator test tap")
        self.assertEqual(Q.load_owner_writes(), [])
        it = F.Item.from_path(F.item_path(self.it.id))
        self.assertIn("operator", it.moves[-1]["cause"])

    def test_owner_undo_needs_quote_and_session_and_hits_the_ledger(self) -> None:
        with self.assertRaises(F.FrontierError):
            F.undo_tap(self.les.id, "got_it", "owner", cause="reversed the decision", at=AT)
        F.undo_tap(self.les.id, "got_it", "owner", cause="reversed the decision",
                   owner_quote="I did not actually get that one", session_id="s1", at=AT)
        writes = Q.load_owner_writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["tool"], "undo_tap")
        les = F.Lesson.from_path(F.lesson_path(self.les.id))
        self.assertEqual(les.tombstones[0]["owner_write"], writes[0]["id"])
        self.assertEqual(les.tombstones[0]["actor"], "owner")

    def test_unknown_actor_and_missing_tap_rejected(self) -> None:
        with self.assertRaises(F.FrontierError):
            F.undo_tap(self.les.id, "got_it", "robot", cause="operator test tap", at=AT)

    def test_undo_with_nothing_to_undo_is_refused_before_delivery(self) -> None:
        les = F.create_lesson(self.it, goal="G0003", kind="new", cause="gap", cause_rank=1)
        for status in ("ready", "held", "failed", "rejected", "dropped"):
            les.status = status
            les.save()
            with self.assertRaises(F.FrontierError):
                F.undo_tap(les.id, "got_it", "operator", cause="operator test tap", at=AT)
        self.assertEqual(F.Lesson.from_path(F.lesson_path(les.id)).tombstones, [])

    def test_undo_with_no_live_tap_on_a_delivered_lesson_writes_a_preemptive_tombstone(self) -> None:
        for status in ("released", "aired", "got_it", "dig_deeper"):
            les = F.create_lesson(self.it, goal="G0003", kind="new", cause="gap", cause_rank=1)
            les.status = status
            les.save()
            F.undo_tap(les.id, "dig_deeper", "operator", cause="operator test tap", at=AT)
            back = F.Lesson.from_path(F.lesson_path(les.id))
            ts = back.tombstones[0]
            self.assertEqual((ts["kind"], ts["tap"], ts["actor"]), ("dig_deeper", "", "operator"), status)

    def test_second_undo_with_no_tap_left_is_a_preemptive_tombstone(self) -> None:
        F.undo_tap(self.les.id, "got_it", "operator", cause="operator test tap", at=AT)
        F.undo_tap(self.les.id, "got_it", "operator", cause="operator test tap", at="2030-01-15T19:05:00")
        les = F.Lesson.from_path(F.lesson_path(self.les.id))
        self.assertEqual([t["tap"] for t in les.tombstones], ["tap-1", ""])

    def test_tap_at_or_before_a_tombstone_is_ignored_and_a_later_one_counts(self) -> None:
        les = self.aired_lesson(self.item(title="Other concept"))
        F.undo_tap(les.id, "got_it", "owner", cause="already knew it", owner_quote="I already know this one",
                   session_id="s1", at="2030-01-15T20:00:00")
        before = F.Item.from_path(F.item_path(les.item)).quadrant
        self.assertIsNone(F.add_tap(les.id, "got_it", "tap-old", at="2030-01-15T19:59:00"))
        self.assertIsNone(F.add_tap(les.id, "got_it", "tap-same", at="2030-01-15T20:00:00"))
        back = F.Lesson.from_path(F.lesson_path(les.id))
        self.assertEqual(back.taps, [])
        self.assertEqual(F.Item.from_path(F.item_path(les.item)).quadrant, before)
        # another kind is not affected, and a tap after the undo stands
        F.add_tap(les.id, "dig_deeper", "tap-dig", at="2030-01-15T19:59:00")
        F.add_tap(les.id, "got_it", "tap-new", at="2030-01-15T20:01:00")
        self.assertEqual([t["id"] for t in F.Lesson.from_path(F.lesson_path(les.id)).taps],
                         ["tap-dig", "tap-new"])


class CliTest(FrontierCase):
    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = F.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self.it = self.item()
        self.les = self.aired_lesson(self.it)
        F.add_tap(self.les.id, "got_it", "tap-1", at=AT)

    def test_undo_tap_cli_undoes_as_operator(self) -> None:
        rc, out, _ = self.run_cli("undo-tap", self.les.id, "got_it", "--cause", "operator test tap")
        self.assertEqual(rc, 0, out)
        self.assertEqual(F.Item.from_path(F.item_path(self.it.id)).quadrant, "unknown_unknown")
        self.assertEqual(Q.load_owner_writes(), [])

    def test_undo_tap_cli_requires_cause(self) -> None:
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                F.main(["undo-tap", self.les.id, "got_it"])

    def test_undo_tap_cli_reports_domain_errors(self) -> None:
        ready = F.create_lesson(self.it, goal="G0003", kind="new", cause="gap", cause_rank=1)
        rc, _, err = self.run_cli("undo-tap", ready.id, "dig_deeper", "--cause", "operator test tap")
        self.assertEqual(rc, 1)
        self.assertIn("dig_deeper", err)

    def test_undo_tap_cli_takes_wiki_lock_and_fails_when_held(self) -> None:
        import fcntl
        lockfile = self.tmp / "meta" / "wiki.lock"
        held = open(lockfile, "a")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DREAMER_LOCK_HELD", None)
                rc, _, err = self.run_cli("undo-tap", self.les.id, "got_it",
                                          "--cause", "operator test tap")
        finally:
            held.close()
        self.assertEqual(rc, 2)
        self.assertIn("wiki.lock", err)
        # nothing was written while the lock was unavailable
        self.assertEqual(F.Lesson.from_path(F.lesson_path(self.les.id)).tombstones, [])


if __name__ == "__main__":
    unittest.main()
