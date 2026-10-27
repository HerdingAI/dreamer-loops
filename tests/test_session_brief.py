#!/usr/bin/env python3
"""§6.1 — the session-start surface: the Friend speaks first, once, to the owner."""

from __future__ import annotations

import datetime as _dt
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import goals as G  # noqa: E402
import questions as Q  # noqa: E402
import session_brief as B  # noqa: E402

D = _dt.date.fromisoformat


class BriefCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-brief-"))
        names = ("meta", "goals", "loops", "archive", "digests", "vault")
        for n in names:
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        self._orig_goals = dict(dc.CFG.get("goals", {}))
        dc.CFG["paths"].update({n: str(self.tmp / n) for n in names})
        dc.CFG.setdefault("goals", {}).update({"max_open_questions": 2, "max_active": 4})

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig_paths)
        dc.CFG["goals"].clear()
        dc.CFG["goals"].update(self._orig_goals)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def digest(self, name: str) -> None:
        (self.tmp / "digests" / name).write_text("# digest\n", encoding="utf-8")

    def goal(self) -> None:
        G.commit_goal("Reach 1600 on the chess puzzle ladder", "Puzzle ladder shows 1600", None, "Club team selection",
                      ["Puzzles"], "yes make that a goal", "s1", ref=D("2026-09-01"))


class SilencePingTest(BriefCase):
    def test_nothing_pending_says_nothing(self) -> None:
        self.assertEqual(B.build_brief(), "")

    def test_silence_question_is_surfaced_once(self) -> None:
        q = Q.enqueue_question("silence", "Chess quiet for ten days — still on this?",
                               goal_id="G0001")
        text = B.build_brief()
        self.assertIn("Chess quiet for ten days — still on this?", text)
        self.assertIn(q["id"], text, "the id is what answer_question needs")
        asked = Q.load_questions()[0]
        self.assertEqual((asked["status"], asked["asked_surface"]), ("asked", "async"))
        # §6.3: never re-asked asynchronously — it waits for the review.
        self.assertEqual(B.build_brief(), "")

    def test_only_silence_interrupts(self) -> None:
        Q.enqueue_question("drift", "What is this for?", loop_id="L0001")
        self.assertNotIn("What is this for?", B.build_brief())
        self.assertEqual(Q.load_questions()[0]["status"], "pending")

    def test_back_pressure_caps_what_is_asked_not_what_is_kept(self) -> None:
        for i in range(1, 4):
            Q.enqueue_question("silence", f"Goal {i} quiet?", goal_id=f"G000{i}")
        text = B.build_brief()
        self.assertEqual(sum(f"Goal {i} quiet?" in text for i in range(1, 4)), 2)
        self.assertEqual(len(Q.load_questions()), 3)


class ReviewOfferTest(BriefCase):
    def test_review_is_offered_once_per_digest(self) -> None:
        self.goal()
        self.digest("2026-40.md")
        self.assertIn("weekly review is ready", B.build_brief())
        self.assertEqual(B.build_brief(), "", "one offer per digest, not one per session")
        self.digest("2026-41.md")
        self.assertIn("2026-41", B.build_brief())

    def test_no_offer_when_the_friend_has_nothing_to_review(self) -> None:
        # No goals and no questions: the digest has no Friend business in it.
        self.digest("2026-40.md")
        self.assertEqual(B.build_brief(), "")

    def test_review_questions_alone_justify_an_offer(self) -> None:
        Q.enqueue_question("drift", "What is this for?", loop_id="L0001")
        self.digest("2026-40.md")
        self.assertIn("weekly review is ready", B.build_brief())

    def test_non_digest_files_are_ignored(self) -> None:
        self.goal()
        self.digest("recurrence-histogram.md")
        self.assertEqual(B.build_brief(), "")


class EntrypointGuardTest(BriefCase):
    def run_main(self, entrypoint: str | None) -> str:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_ENTRYPOINT"}
        if entrypoint is not None:
            env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buf):
            self.assertEqual(B.main(), 0)
        return buf.getvalue()

    def test_headless_runs_do_not_consume_the_ping(self) -> None:
        # Dreamer's own jobs are `claude -p` sessions (entrypoint sdk-cli). A
        # ping spent on one of those is a ping the owner never saw.
        Q.enqueue_question("silence", "Chess quiet?", goal_id="G0001")
        self.assertEqual(self.run_main("sdk-cli"), "")
        self.assertEqual(self.run_main(None), "")
        self.assertEqual(Q.load_questions()[0]["status"], "pending")

    def test_interactive_session_gets_the_ping(self) -> None:
        Q.enqueue_question("silence", "Chess quiet?", goal_id="G0001")
        self.assertIn("Chess quiet?", self.run_main("cli"))

    def test_a_broken_queue_never_breaks_the_session(self) -> None:
        (self.tmp / "meta" / "open-questions.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_main("cli"), "")


if __name__ == "__main__":
    unittest.main()
