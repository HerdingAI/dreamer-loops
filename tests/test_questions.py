#!/usr/bin/env python3
"""Unit tests for the open-questions queue and owner-writes ledger (§5.6, §6.3, §6.4, §9.3)."""

from __future__ import annotations

import datetime as _dt
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import questions as Q  # noqa: E402

D = _dt.date.fromisoformat


class QuestionsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-q-"))
        (self.tmp / "meta").mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        self._orig_goals = dict(dc.CFG.get("goals", {}))
        dc.CFG["paths"].update({"meta": str(self.tmp / "meta")})
        dc.CFG["goals"].update({
            "max_open_questions": 2,
            "back_off_days": 14,
        })

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig_paths)
        dc.CFG.setdefault("goals", {}).clear()
        dc.CFG["goals"].update(self._orig_goals)
        shutil.rmtree(self.tmp, ignore_errors=True)


class QuestionLifecycleTest(QuestionsCase):
    def test_enqueue_and_list_idempotency(self) -> None:
        q1 = Q.enqueue_question("silence", "Haven't touched Chess in 10 days?", goal_id="G0001")
        self.assertEqual(q1["status"], "pending")
        self.assertEqual(q1["kind"], "silence")
        self.assertEqual(q1["goal_id"], "G0001")

        # Duplicate enqueue returns existing pending question
        q2 = Q.enqueue_question("silence", "Haven't touched Chess in 10 days?", goal_id="G0001")
        self.assertEqual(q1["id"], q2["id"])
        self.assertEqual(len(Q.load_questions()), 1)

    def test_answer_question_marks_answered_and_records_response(self) -> None:
        q = Q.enqueue_question("silence", "Still working on Chess?", goal_id="G0001")
        ans = Q.answer_question(
            q["id"],
            "Yes, played a timed game yesterday",
            owner_quote="yes, played a timed game yesterday",
            session_id="session_abc",
        )
        self.assertEqual(ans["status"], "answered")
        self.assertEqual(ans["answer"], "Yes, played a timed game yesterday")
        self.assertEqual(ans["last_response"], "yes, played a timed game yesterday")
        self.assertIsNotNone(ans["answered_at"])

        # No longer in pending list
        pending = Q.list_questions(status="pending")
        self.assertEqual(pending, [])

    def test_defer_and_exponential_backoff(self) -> None:
        q = Q.enqueue_question("silence", "Still on this?", goal_id="G0001")
        # 1st deferral: 14 days
        def1 = Q.defer_question(q["id"], owner_quote="leave it for now", session_id="s1", ref=D("2026-09-01"))
        self.assertEqual(def1["status"], "deferred")
        self.assertEqual(def1["backoff_count"], 1)
        self.assertEqual(def1["defer_until"], "2026-09-15")

        # 2nd deferral: doubles to 28 days (from ref date)
        def2 = Q.defer_question(q["id"], owner_quote="leave it again", session_id="s2", ref=D("2026-09-15"))
        self.assertEqual(def2["backoff_count"], 2)
        # 28 days from 2026-09-15 is 2026-10-13
        self.assertEqual(def2["defer_until"], "2026-10-13")

    def test_questions_never_age_out(self) -> None:
        q = Q.enqueue_question("drift", "What is L0042 for?", loop_id="L0042")
        # Check listing 100 days later without answer -> still pending
        pending = Q.list_questions(status="pending", ref=D("2026-12-31"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], q["id"])

    def test_async_surface_back_pressure_and_silence_only(self) -> None:
        # Enqueue silence question
        q_silence = Q.enqueue_question("silence", "Chess quiet?", goal_id="G0001")
        # Enqueue drift question (should only surface in review, not async)
        q_drift = Q.enqueue_question("drift", "What is L0042 for?", loop_id="L0042")
        # Enqueue execution diagnosis
        q_stale = Q.enqueue_question("stale", "Movement stale on G0002?", goal_id="G0002")

        # Async list: only silence should be returned
        async_items = Q.list_questions(surface="async")
        self.assertEqual(len(async_items), 1)
        self.assertEqual(async_items[0]["id"], q_silence["id"])

        # Mark asked async
        Q.mark_asked([q_silence["id"]], surface="async")
        # Once asked async, never re-asked async (§6.3)
        self.assertEqual(Q.list_questions(surface="async"), [])

        # But all 3 remain in review
        review_items = Q.list_questions(surface="all")
        self.assertEqual(len(review_items), 3)

    def test_defer_prevents_duplicate_enqueue(self) -> None:
        q = Q.enqueue_question("silence", "Chess quiet?", goal_id="G0001")
        Q.defer_question(q["id"], days=14, owner_quote="snooze", session_id="s1")

        # Nightly detector tries to re-enqueue during active deferral window
        q_dup = Q.enqueue_question("silence", "Chess quiet?", goal_id="G0001")
        self.assertEqual(q_dup["id"], q["id"])
        self.assertEqual(len(Q.load_questions()), 1)

    def test_defer_expiration_resurfaces_in_unresolved(self) -> None:
        q = Q.enqueue_question("silence", "Chess quiet?", goal_id="G0001")
        Q.defer_question(q["id"], days=7, owner_quote="snooze", session_id="s1", ref=D("2026-09-01"))

        # While active (e.g. 2026-09-05), not in unresolved
        self.assertEqual(Q.list_questions(status="unresolved", ref=D("2026-09-05")), [])

        # Once expired (e.g. 2026-09-10), resurfaces in unresolved
        resurfaced = Q.list_questions(status="unresolved", ref=D("2026-09-10"))
        self.assertEqual(len(resurfaced), 1)
        self.assertEqual(resurfaced[0]["id"], q["id"])

    def test_strict_json_raises_on_corruption(self) -> None:
        (self.tmp / "meta" / "open-questions.json").write_text("{malformed json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            Q.load_questions()


class OwnerWritesTest(QuestionsCase):
    def test_record_owner_write(self) -> None:
        rec = Q.record_owner_write(
            tool="complete_step",
            target_type="goal",
            target_id="G0001",
            owner_quote="yes, mark step 3 done",
            session_id="session_123",
            params={"step": 3, "citation": "[[transcripts/test]]"},
            confirmation="Noted: G0001 step 3 marked done.",
        )
        self.assertTrue(rec["id"].startswith("W-"))
        self.assertEqual(rec["verified"], None)

        writes = Q.load_owner_writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["id"], rec["id"])
        self.assertEqual(writes[0]["owner_quote"], "yes, mark step 3 done")


class HumanTurnsTest(unittest.TestCase):
    def test_assistant_only_transcript_has_no_human_text(self) -> None:
        # Falling back to the whole text here would let assistant prose
        # verify an owner quote.
        self.assertEqual(Q.extract_human_turns("## Assistant\n\nyes, mark it done\n"), "")

    def test_text_without_role_markers_is_taken_whole(self) -> None:
        self.assertEqual(Q.extract_human_turns("just a note"), "just a note")

    def test_quote_matching_ignores_whitespace_case_and_quote_style(self) -> None:
        corpus = Q.normalize_quote("Yes \u2014 mark\n   that done, it\u2019s finished")
        self.assertTrue(Q.quote_in("yes \u2014 mark that done, it's FINISHED", corpus))
        self.assertFalse(Q.quote_in("mark the other one done", corpus))
        self.assertFalse(Q.quote_in("   ", corpus))


class ReconcileOwnerWritesTest(QuestionsCase):
    """§5.4: a write whose quote never shows up in a Human turn is
    quarantined AND comes back to the owner as a question."""

    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "sources").mkdir()
        dc.CFG["paths"]["sources"] = str(self.tmp / "sources")
        self._orig_mcp = dict(dc.CFG.get("mcp", {}))
        dc.CFG.setdefault("mcp", {})["owner_write_verify_lag_days"] = 3
        self.now = _dt.datetime(2026, 9, 10, 12, 0, 0)

    def tearDown(self) -> None:
        dc.CFG["mcp"].clear(); dc.CFG["mcp"].update(self._orig_mcp)
        super().tearDown()

    def write(self, quote: str, days_ago: float, target: str = "G0001") -> dict:
        return Q.record_owner_write(
            tool="set_status", target_type="goal", target_id=target, owner_quote=quote,
            session_id="s1", params={}, confirmation="ok",
            at=self.now - _dt.timedelta(days=days_ago))

    def transcript(self, human: str) -> None:
        (self.tmp / "sources" / "2026-09-09--t.md").write_text(
            f"## Human\n\n{human}\n\n## Assistant\n\nDone.\n", encoding="utf-8")

    def test_quote_found_in_a_human_turn_verifies(self) -> None:
        self.write("yes, pause the Chess goal", 1)
        self.transcript("Yes,  pause the\nChess goal")
        res = Q.reconcile_owner_writes(now=self.now)
        self.assertEqual((res["verified"], res["quarantined"]), (1, 0))
        self.assertIs(Q.load_owner_writes()[0]["verified"], True)

    def test_within_the_lag_an_unmatched_write_just_waits(self) -> None:
        # The session it was made in may simply not be ingested yet.
        self.write("yes, pause the Chess goal", 1)
        Q.reconcile_owner_writes(now=self.now)
        self.assertIsNone(Q.load_owner_writes()[0]["verified"])
        self.assertEqual(Q.load_questions(), [])

    def test_past_the_lag_it_is_quarantined_and_asked_about_once(self) -> None:
        w = self.write("yes, pause the Chess goal", 4)
        res = Q.reconcile_owner_writes(now=self.now)
        self.assertEqual(res["quarantined"], 1)
        rec = Q.load_owner_writes()[0]
        self.assertIs(rec["verified"], False)
        qs = Q.load_questions()
        self.assertEqual([q["kind"] for q in qs], ["unverified-write"])
        self.assertEqual(rec["question_id"], qs[0]["id"])
        self.assertIn(w["id"], qs[0]["text"])
        self.assertIn("yes, pause the Chess goal", qs[0]["text"])
        Q.reconcile_owner_writes(now=self.now)
        self.assertEqual(len(Q.load_questions()), 1, "one question per write, not per night")

    def test_two_bad_writes_on_one_goal_get_a_question_each(self) -> None:
        self.write("yes, pause the Chess goal", 4)
        self.write("and mark step two finished", 5)
        Q.reconcile_owner_writes(now=self.now)
        self.assertEqual(len(Q.load_questions()), 2)

    def test_late_ingestion_clears_the_quarantine(self) -> None:
        self.write("yes, pause the Chess goal", 4)
        Q.reconcile_owner_writes(now=self.now)
        self.transcript("yes, pause the Chess goal")
        res = Q.reconcile_owner_writes(now=self.now)
        self.assertEqual(res["verified"], 1)
        self.assertIs(Q.load_owner_writes()[0]["verified"], True)
        self.assertEqual(Q.load_questions()[0]["status"], "answered")


class QuarantineExitTest(QuestionsCase):
    """Spec §5.6 / §12.4: every quarantine has an owner-facing exit."""

    def quarantine(self, entries: list) -> None:
        dc.atomic_write_json(self.tmp / "meta" / "fold-quarantine.json", entries)

    def test_each_quarantined_fold_becomes_one_question(self) -> None:
        self.quarantine([
            {"loop_id": "L0001", "occurrence": "[[sources/transcripts/a]]", "attempts": 5},
            {"loop_id": "L0001", "occurrence": "[[sources/transcripts/b]]", "attempts": 5},
            {"occurrence": "malformed, no loop"},
        ])
        self.assertEqual(Q.sync_fold_quarantine()["enqueued"], 2)
        Q.sync_fold_quarantine()
        self.assertEqual(len(Q.load_questions()), 2, "idempotent across nights")

    def test_repaired_entry_settles_its_question(self) -> None:
        self.quarantine([{"loop_id": "L0001", "occurrence": "[[sources/transcripts/a]]"}])
        Q.sync_fold_quarantine()
        self.quarantine([])
        self.assertEqual(Q.sync_fold_quarantine()["settled"], 1)
        self.assertEqual(Q.load_questions()[0]["status"], "answered")

    def test_derived_claims_are_asked_about_once_per_conclusion(self) -> None:
        Q.note_derived_claims("L0023", "conclusions/2026-08-04--x", 3)
        Q.note_derived_claims("L0023", "conclusions/2026-08-04--x", 3)
        Q.note_derived_claims("L0023", "conclusions/2026-08-09--y", 1)
        qs = Q.load_questions()
        self.assertEqual([q["kind"] for q in qs], ["derived-claims"] * 2)
        self.assertIn("3 claim(s)", qs[0]["text"])
        # Never an interrupt: it waits for the weekly review.
        self.assertEqual(Q.list_questions(surface="async"), [])
        self.assertEqual(len(Q.list_questions(surface="review")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
