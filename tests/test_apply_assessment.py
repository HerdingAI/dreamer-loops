#!/usr/bin/env python3
"""Unit tests for apply_assessment.py and goal assessment folds (§4, §7.2, §9.3)."""

from __future__ import annotations

import datetime as _dt
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import apply_assessment as AA  # noqa: E402
import dreamer_common as dc  # noqa: E402
import goals as G  # noqa: E402
import questions as Q  # noqa: E402
import vault as V  # noqa: E402

D = _dt.date.fromisoformat


class ApplyAssessmentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-assess-"))
        for n in ("loops", "goals", "conclusions", "concepts", "archive", "digests",
                  "sources", "meta", "vault", "resurfacings"):
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
            (self.tmp / "vault" / n).mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        self._orig_goals = dict(dc.CFG.get("goals", {}))
        self._orig_decay = dict(dc.CFG["decay"])

        dc.CFG["paths"].update({
            "vault": str(self.tmp / "vault"),
            "loops": str(self.tmp / "loops"),
            "goals": str(self.tmp / "goals"),
            "conclusions": str(self.tmp / "conclusions"),
            "concepts": str(self.tmp / "concepts"),
            "archive": str(self.tmp / "archive"),
            "digests": str(self.tmp / "digests"),
            # As in production config: the transcripts root, inside the vault.
            "sources": str(self.tmp / "vault" / "sources" / "transcripts"),
            "meta": str(self.tmp / "meta"),
            "resurfacings": str(self.tmp / "resurfacings"),
        })
        dc.CFG["goals"].update({
            "max_active": 4,
            "silence_ping_days": 10,
            "movement_stale_days": 21,
            "drift_recurrence_min": 3,
            "max_open_questions": 2,
            "back_off_days": 14,
        })
        dc.CFG["decay"].update({
            "go_live_date": "2026-08-01",
            "decay_weeks": 8,
            "terminal_multiplier": 2,
        })

    def tearDown(self) -> None:
        dc.CFG["paths"].clear(); dc.CFG["paths"].update(self._orig_paths)
        dc.CFG.setdefault("goals", {}).clear(); dc.CFG["goals"].update(self._orig_goals)
        dc.CFG["decay"].clear(); dc.CFG["decay"].update(self._orig_decay)
        shutil.rmtree(self.tmp, ignore_errors=True)

    HUMAN = ("I was thinking about the tournament date. "
             "I completed 40 puzzles.")

    def transcript(self, date: str, slug: str, human: str | None = None,
                   assistant: str = "Noted.") -> str:
        rel = Path("sources/transcripts") / date[:4] / date[5:7] / f"{date}--{slug}.md"
        full = self.tmp / "vault" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(
            f"---\ntype: transcript\ndate: {date}\n---\n\n"
            f"## Human\n\n{human or self.HUMAN}\n\n## Assistant\n\n{assistant}\n",
            encoding="utf-8")
        return f"[[{rel.with_suffix('')}]]"


class AssessmentResultsApplicationTest(ApplyAssessmentCase):
    def test_mention_updates_mention_clock_only(self) -> None:
        g = G.commit_goal(
            "Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
            "quote", "sess", ref=D("2026-09-01")
        )
        occ = self.transcript("2026-09-05", "chess-talk")
        payload = {
            "attachments": [{
                "goal_id": g.id,
                "transcript": occ,
                "kind": "mention",
                "date": "2026-09-05",
                "anchor": "I was thinking about the tournament date",
                "summary": "Discussed tournament timing",
            }],
            "unattached": [],
            "observations": [],
        }
        res = AA.apply_assessment_results(payload, ref=D("2026-09-05"))
        self.assertEqual(res["applied_attachments"], 1)

        reloaded = G.load_goal(g.id)
        self.assertEqual(reloaded.last_mention, D("2026-09-05"))
        self.assertEqual(reloaded.last_movement, D("2026-09-01"), "mention must not advance movement clock")
        self.assertIn(occ, reloaded.attachments)

    def test_movement_updates_both_clocks(self) -> None:
        g = G.commit_goal(
            "Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
            "quote", "sess", ref=D("2026-09-01")
        )
        occ = self.transcript("2026-09-08", "chess-drilled")
        payload = {
            "attachments": [{
                "goal_id": g.id,
                "transcript": occ,
                "kind": "movement",
                "date": "2026-09-08",
                "anchor": "I completed 40 puzzles",
                "summary": "Drilled 40 puzzles",
            }],
            "unattached": [],
            "observations": [],
        }
        res = AA.apply_assessment_results(payload, ref=D("2026-09-08"))
        self.assertEqual(res["applied_attachments"], 1)

        reloaded = G.load_goal(g.id)
        self.assertEqual(reloaded.last_mention, D("2026-09-08"))
        self.assertEqual(reloaded.last_movement, D("2026-09-08"))

    def test_cost_observation_enqueues_question(self) -> None:
        g = G.commit_goal(
            "Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
            "quote", "sess", ref=D("2026-09-01")
        )
        occ = self.transcript("2026-09-05", "tired",
                              human="I am worn out and frustrated because of prep.")
        payload = {
            "attachments": [],
            "unattached": [],
            "observations": [{
                "kind": "cost",
                "goal_id": g.id,
                "transcript": occ,
                "anchor": "I am worn out and frustrated because of prep",
                "observation": "Exhaustion and frustration noted against study pace",
            }],
        }
        res = AA.apply_assessment_results(payload, ref=D("2026-09-05"))
        self.assertEqual(res["questions_enqueued"], 1)

        questions = Q.load_questions()
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["kind"], "cost")
        self.assertEqual(questions[0]["goal_id"], g.id)
        self.assertIn(occ, questions[0]["text"], "an observation is cited or it is not made")

    def test_cost_observation_not_in_the_owners_words_is_dropped(self) -> None:
        # D10: wellbeing is evidence the owner produced, never a diagnosis.
        g = G.commit_goal("Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
                          "quote", "sess", ref=D("2026-09-01"))
        occ = self.transcript("2026-09-05", "fine", human="Prep is going fine.",
                              assistant="You sound exhausted and burnt out.")
        for obs in ({"transcript": occ, "anchor": "You sound exhausted and burnt out"},
                    {"anchor": "I am exhausted"}):
            res = AA.apply_assessment_results(
                {"attachments": [], "unattached": [],
                 "observations": [{"kind": "cost", "goal_id": g.id,
                                   "observation": "Exhaustion", **obs}]},
                ref=D("2026-09-05"))
            self.assertEqual(res["questions_enqueued"], 0)
        self.assertEqual(Q.load_questions(), [])


class GoalAssessmentFoldTest(ApplyAssessmentCase):
    def test_apply_assessment_fold_updates_now_and_trajectory(self) -> None:
        g = G.commit_goal(
            "Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
            "quote", "sess", ref=D("2026-09-01")
        )
        occ = self.transcript("2026-09-05", "section-practice")
        g.attachments.append(occ)
        g.save()

        fold_payload = {
            "now": f"The owner played a timed game and reached 1650 ({occ}).",
            "trajectory_line": "Reached 1650 on timed game 1",
        }
        res = AA.apply_assessment_fold(g.id, occ, D("2026-09-05"), fold_payload)
        self.assertFalse(res["noop"])
        self.assertEqual(res["trajectory_lines"], 2)  # initial commit line + 1 new

        reloaded = G.load_goal(g.id)
        self.assertIn("The owner played a timed game", reloaded.body)
        self.assertIn("via thread", reloaded.body)
        self.assertIn(f"- 2026-09-05 — Reached 1650 on timed game 1 — {occ}", reloaded.body)

    def test_fold_refuses_uncited_foreign_wikilinks(self) -> None:
        g = G.commit_goal(
            "Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
            "quote", "sess", ref=D("2026-09-01")
        )
        occ = self.transcript("2026-09-05", "section-practice")
        g.attachments.append(occ)
        g.save()

        foreign = self.transcript("2026-09-06", "foreign-doc")
        bad_payload = {
            "now": f"Progress made ({foreign}).",
            "trajectory_line": "Progress note",
        }
        with self.assertRaises(AA.GoalFoldError):
            AA.apply_assessment_fold(g.id, occ, D("2026-09-05"), bad_payload)

    def test_build_assessment_fold_prompt_inlines_plan(self) -> None:
        g = G.commit_goal(
            "Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Complete the placement puzzles", "- [ ] Review openings"],
            "quote", "sess", ref=D("2026-09-01")
        )
        occ = self.transcript("2026-09-05", "section-practice")
        g.attachments.append(occ)
        g.save()

        prompt_out = self.tmp / "prompt-test.md"
        tr_path = V._resolve_wikilink(occ)
        AA.build_assessment_fold_prompt(g.id, occ, str(tr_path), prompt_out)

        prompt_content = prompt_out.read_text(encoding="utf-8")
        self.assertIn("## Current plan", prompt_content)
        self.assertIn("- [ ] Complete the placement puzzles", prompt_content)
        self.assertIn("- [ ] Review openings", prompt_content)

    def test_monotonic_clock_updates_on_out_of_order_attachments(self) -> None:
        g = G.commit_goal(
            "Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
            "quote", "sess", ref=D("2026-09-01")
        )
        # Apply attachment from 2026-09-10
        anchor = "I completed 40 puzzles"
        p1 = {
            "attachments": [{"goal_id": g.id, "transcript": self.transcript("2026-09-10", "t2"), "date": "2026-09-10", "kind": "movement", "anchor": anchor}],
            "unattached": [], "observations": [],
        }
        AA.apply_assessment_results(p1, ref=D("2026-09-10"))
        reloaded = G.load_goal(g.id)
        self.assertEqual(reloaded.last_mention, D("2026-09-10"))
        self.assertEqual(reloaded.last_movement, D("2026-09-10"))

        # Apply older attachment from 2026-09-05 (out-of-order)
        p2 = {
            "attachments": [{"goal_id": g.id, "transcript": self.transcript("2026-09-05", "t1"), "date": "2026-09-05", "kind": "movement", "anchor": anchor}],
            "unattached": [], "observations": [],
        }
        AA.apply_assessment_results(p2, ref=D("2026-09-05"))
        reloaded = G.load_goal(g.id)
        # Must not regress to 2026-09-05
        self.assertEqual(reloaded.last_mention, D("2026-09-10"))
        self.assertEqual(reloaded.last_movement, D("2026-09-10"))


class AttachmentValidationTest(ApplyAssessmentCase):
    """§4.3's inverted bias made mechanical: an attachment that cannot be
    checked against the owner's own words does not move a clock."""

    def goal(self):
        return G.commit_goal("Chess", "Puzzle ladder 1600", None, "Club team selection",
                             ["- [ ] Step 1"], "quote", "sess", ref=D("2026-09-01"))

    def apply(self, g, transcript, anchor, *, kind="movement", date=None, ref="2026-09-20"):
        att = {"goal_id": g.id, "transcript": transcript, "kind": kind, "anchor": anchor}
        if date:
            att["date"] = date
        return AA.apply_assessment_results(
            {"attachments": [att], "unattached": [], "observations": []}, ref=D(ref))

    def test_missing_transcript_is_rejected(self) -> None:
        g = self.goal()
        res = self.apply(g, "[[sources/transcripts/2026/09/2026-09-05--never-existed]]",
                         "I completed 40 puzzles")
        self.assertEqual((res["applied_attachments"], len(res["rejected"])), (0, 1))
        self.assertEqual(G.load_goal(g.id).last_mention, D("2026-09-01"))

    def test_date_comes_from_the_transcript_not_the_model(self) -> None:
        # A model that stamps an old conversation with today's date would
        # silence a ping that should fire.
        g = self.goal()
        occ = self.transcript("2026-09-05", "old-talk")
        self.apply(g, occ, "I completed 40 puzzles", date="2026-09-20")
        self.assertEqual(G.load_goal(g.id).last_movement, D("2026-09-05"))
        self.apply(g, self.transcript("2026-09-06", "undated"),
                   "I completed 40 puzzles")
        self.assertEqual(G.load_goal(g.id).last_movement, D("2026-09-06"))

    def test_anchor_found_only_in_an_assistant_turn_is_rejected(self) -> None:
        g = self.goal()
        occ = self.transcript("2026-09-05", "cheerleading", human="What should I do next?",
                              assistant="Great work finishing the puzzle set!")
        res = self.apply(g, occ, "Great work finishing the puzzle set")
        self.assertEqual(res["applied_attachments"], 0)
        self.assertIn("Human turn", res["rejected"][0]["reason"])
        self.assertEqual(G.load_goal(g.id).last_movement, D("2026-09-01"))
        self.assertNotIn(occ, G.load_goal(g.id).attachments)

    def test_missing_anchor_is_rejected(self) -> None:
        g = self.goal()
        res = self.apply(g, self.transcript("2026-09-05", "t"), "")
        self.assertEqual(res["applied_attachments"], 0)

    def test_anchor_match_tolerates_whitespace_case_and_quote_style(self) -> None:
        g = self.goal()
        occ = self.transcript("2026-09-05", "wrapped",
                              human="I finished\n  the puzzle set, and it\u2019s done.")
        res = self.apply(g, occ, "i finished the Puzzle set, and it's done")
        self.assertEqual(res["applied_attachments"], 1)


class DetectorQuestionSettlingTest(ApplyAssessmentCase):
    """A detector's question stands only while its condition does. Asking
    "what's the blocker?" the night after the work was done is the nag."""

    def setUp(self) -> None:
        super().setUp()
        self.g = G.commit_goal("Chess", "Puzzle ladder 1600", None, "Club team selection", ["- [ ] Step 1"],
                               "quote", "sess", ref=D("2026-08-01"))

    def night(self, ref: str, attachments: list | None = None) -> dict:
        return AA.apply_assessment_results(
            {"attachments": attachments or [], "unattached": [], "observations": []},
            ref=D(ref))

    def open_kinds(self) -> list[str]:
        return sorted(q["kind"] for q in Q.load_questions() if q["status"] != "answered")

    def test_fresh_movement_settles_silence_and_stale(self) -> None:
        self.night("2026-08-30")
        self.assertEqual(self.open_kinds(), ["silence", "stale"])
        occ = self.transcript("2026-08-31", "worked")
        self.night("2026-08-31", [{"goal_id": self.g.id, "transcript": occ, "kind": "movement",
                                   "anchor": "I completed 40 puzzles"}])
        self.assertEqual(self.open_kinds(), [])
        self.assertTrue(all("2026-08-31" in q["answer"] for q in Q.load_questions()))

    def test_a_mention_settles_silence_but_not_stale(self) -> None:
        self.night("2026-08-30")
        occ = self.transcript("2026-08-31", "talked")
        self.night("2026-08-31", [{"goal_id": self.g.id, "transcript": occ, "kind": "mention",
                                   "anchor": "I was thinking about the tournament date"}])
        self.assertEqual(self.open_kinds(), ["stale"])

    def test_a_goal_no_longer_active_settles_its_questions(self) -> None:
        self.night("2026-08-30")
        G.set_goal_status(G.load_goal(self.g.id), "paused", "pause it for now", "s2")
        self.night("2026-08-31")
        self.assertEqual(self.open_kinds(), [])

    def test_drift_question_settles_when_the_loop_is_no_longer_drifting(self) -> None:
        loop = V.create_loop("Recurring thing?", self.transcript("2026-08-28", "l0"),
                             D("2026-08-28"))
        for n in range(3):
            V.add_occurrence(loop, self.transcript("2026-08-29", f"l{n + 1}"), D("2026-08-29"))
        self.night("2026-08-30")
        self.assertIn("drift", self.open_kinds())
        loop = V.load_loop(loop.id)
        loop.status = "resolved"
        loop.save()
        self.night("2026-08-31")
        self.assertNotIn("drift", self.open_kinds())

    def test_an_owner_answer_is_never_overwritten(self) -> None:
        self.night("2026-08-30")
        q = next(q for q in Q.load_questions() if q["kind"] == "silence")
        Q.answer_question(q["id"], "still on it, was travelling")
        occ = self.transcript("2026-08-31", "worked")
        self.night("2026-08-31", [{"goal_id": self.g.id, "transcript": occ, "kind": "movement",
                                   "anchor": "I completed 40 puzzles"}])
        kept = next(x for x in Q.load_questions() if x["id"] == q["id"])
        self.assertEqual(kept["answer"], "still on it, was travelling")


class TranscriptSelectionTest(ApplyAssessmentCase):
    """The leg reads each transcript once, by ledger — never by today's date:
    a session ingested tonight is dated the day the conversation happened."""

    def setUp(self) -> None:
        super().setUp()
        dc.CFG["goals"]["assess_batch_size"] = 2

    def test_no_active_goals_selects_nothing(self) -> None:
        self.transcript("2026-09-05", "a")
        self.assertEqual(AA.select_transcripts(), [])

    def test_selects_unassessed_oldest_first_and_caps_the_batch(self) -> None:
        G.commit_goal("Chess", "F", None, "W", ["S"], "quote", "s", ref=D("2026-09-03"))
        before = self.transcript("2026-09-01", "before-the-goal-existed")
        a = self.transcript("2026-09-03", "a")
        b = self.transcript("2026-09-04", "b")
        c = self.transcript("2026-09-05", "c")
        rels = lambda files: [f"[[{AA.rel_of(f)}]]" for f in files]  # noqa: E731
        self.assertEqual(rels(AA.select_transcripts()), [a, b])
        self.assertNotIn(before, rels(AA.select_transcripts()))

        AA.mark_assessed([AA.rel_of(f) for f in AA.select_transcripts()])
        self.assertEqual(rels(AA.select_transcripts()), [c])
        AA.mark_assessed([AA.rel_of(f) for f in AA.select_transcripts()])
        self.assertEqual(AA.select_transcripts(), [])


class SchedulingTest(unittest.TestCase):
    def test_the_assess_leg_is_scheduled_after_extraction(self) -> None:
        # Guards against the assess leg being built but never scheduled.
        cron = (ROOT / "bin" / "install-cron.sh").read_text(encoding="utf-8")
        lines = [l for l in cron.splitlines()
                 if "nightly-assess.sh" in l and not l.lstrip().startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertLess(lines[0].index("nightly-extract.sh"), lines[0].index("nightly-assess.sh"),
                        "drift is measured in loops, so tonight's extraction must run first")


if __name__ == "__main__":
    unittest.main(verbosity=2)
