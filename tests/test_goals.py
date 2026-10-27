#!/usr/bin/env python3
"""Unit tests for Goal mechanics (§3, §4, §9.3)."""

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
import goals as G  # noqa: E402
import vault as V  # noqa: E402

D = _dt.date.fromisoformat


class GoalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-goals-"))
        for name in ("loops", "goals", "conclusions", "archive", "digests",
                     "sources", "meta", "vault"):
            (self.tmp / name).mkdir(parents=True, exist_ok=True)
            (self.tmp / "vault" / name).mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        self._orig_decay = dict(dc.CFG["decay"])
        self._orig_goals = dict(dc.CFG.get("goals", {}))
        self._orig_match = dict(dc.CFG["matching"])

        dc.CFG["paths"].update({
            "vault": str(self.tmp / "vault"),
            "loops": str(self.tmp / "loops"),
            "goals": str(self.tmp / "goals"),
            "conclusions": str(self.tmp / "conclusions"),
            "archive": str(self.tmp / "archive"),
            "digests": str(self.tmp / "digests"),
            "sources": str(self.tmp / "sources"),
            "meta": str(self.tmp / "meta"),
        })
        dc.CFG["goals"].update({
            "max_active": 4,
            "silence_ping_days": 10,
            "movement_stale_days": 21,
            "drift_recurrence_min": 3,
            "max_open_questions": 2,
            "back_off_days": 14,
            "silence_escalation_weeks": 3,
        })
        dc.CFG["decay"].update({
            "go_live_date": "2026-08-01",
            "decay_weeks": 8,
            "terminal_multiplier": 2,
        })
        dc.CFG["matching"].update({
            "recurrence_min": 2,
            "recency_half_life_days": 60,
        })

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig_paths)
        dc.CFG["decay"].clear()
        dc.CFG["decay"].update(self._orig_decay)
        dc.CFG.setdefault("goals", {}).clear()
        dc.CFG["goals"].update(self._orig_goals)
        dc.CFG["matching"].clear()
        dc.CFG["matching"].update(self._orig_match)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, date: str, slug: str) -> str:
        rel = Path("sources/transcripts") / date[:4] / date[5:7] / f"{date}--{slug}.md"
        full = self.tmp / "vault" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("---\ntype: transcript\n---\n\nbody\n", encoding="utf-8")
        return f"[[{rel.with_suffix('')}]]"


class GoalStateMachineTest(GoalCase):
    def test_commit_creates_active_goal_and_assigns_id(self) -> None:
        g = G.commit_goal(
            title="Reach 1600 on the chess puzzle ladder",
            finish_line="Puzzle ladder >= 1600",
            target_date="2030-05-01",
            why="Club team selection",
            plan_steps=["1. Puzzle workbook drilling", "2. Timed games"],
            owner_quote="let's make that a goal",
            session_id="session_123",
            ref=D("2026-09-02"),
        )
        self.assertEqual(g.id, "G0001")
        self.assertEqual(g.status, "active")
        self.assertEqual(g.created, D("2026-09-02"))
        self.assertEqual(g.last_mention, D("2026-09-02"))
        self.assertEqual(g.last_movement, D("2026-09-02"))
        self.assertTrue((self.tmp / "goals" / "G0001.md").exists())

    def test_ids_are_monotonic(self) -> None:
        g1 = G.commit_goal(
            title="Goal 1", finish_line="Done 1", target_date=None, why="Why 1",
            plan_steps=["Step 1"], owner_quote="quote 1", session_id="s1", ref=D("2026-09-01"),
        )
        g2 = G.commit_goal(
            title="Goal 2", finish_line="Done 2", target_date=None, why="Why 2",
            plan_steps=["Step 1"], owner_quote="quote 2", session_id="s2", ref=D("2026-09-02"),
        )
        self.assertEqual([g1.id, g2.id], ["G0001", "G0002"])

    def test_max_active_cap_enforced_at_commit(self) -> None:
        dc.CFG["goals"]["max_active"] = 2
        G.commit_goal("G1", "F1", None, "W1", ["S1"], "q", "s")
        G.commit_goal("G2", "F2", None, "W2", ["S1"], "q", "s")
        with self.assertRaises(ValueError):
            G.commit_goal("G3", "F3", None, "W3", ["S1"], "q", "s")

    def test_pause_and_resume_and_cap(self) -> None:
        dc.CFG["goals"]["max_active"] = 2
        g1 = G.commit_goal("G1", "F1", None, "W1", ["S1"], "q", "s")
        g2 = G.commit_goal("G2", "F2", None, "W2", ["S1"], "q", "s")
        # Pause g1 to make room
        G.set_goal_status(g1, "paused", "pause G1", "s")
        self.assertEqual(g1.status, "paused")
        self.assertEqual(G.active_goals_count(), 1)
        # Now G3 can be committed
        g3 = G.commit_goal("G3", "F3", None, "W3", ["S1"], "q", "s")
        self.assertEqual(G.active_goals_count(), 2)
        # Resuming g1 should fail because cap is reached
        with self.assertRaises(ValueError):
            G.set_goal_status(g1, "active", "resume G1", "s")

    def test_all_legal_state_transitions(self) -> None:
        # active -> achieved
        g_ach = G.commit_goal("G", "F", None, "W", ["S"], "q", "s")
        G.set_goal_status(g_ach, "achieved", "I passed", "s")
        self.assertEqual(g_ach.status, "achieved")

        # active -> abandoned
        g_ab1 = G.commit_goal("G", "F", None, "W", ["S"], "q", "s")
        G.set_goal_status(g_ab1, "abandoned", "dropping this", "s")
        self.assertEqual(g_ab1.status, "abandoned")

        # paused -> abandoned
        g_ab2 = G.commit_goal("G", "F", None, "W", ["S"], "q", "s")
        G.set_goal_status(g_ab2, "paused", "pause", "s")
        G.set_goal_status(g_ab2, "abandoned", "kill it", "s")
        self.assertEqual(g_ab2.status, "abandoned")

    def test_illegal_state_transitions_refused(self) -> None:
        g = G.commit_goal("G", "F", None, "W", ["S"], "q", "s")
        G.set_goal_status(g, "achieved", "done", "s")
        # Achieved is terminal
        with self.assertRaises(ValueError):
            G.set_goal_status(g, "active", "reopen", "s")
        with self.assertRaises(ValueError):
            G.set_goal_status(g, "paused", "pause", "s")


class PlanRevisionTest(GoalCase):
    def test_revise_plan_preserves_history_and_bumps_revision(self) -> None:
        g = G.commit_goal(
            title="Reach 1600 on the chess puzzle ladder",
            finish_line="Puzzle ladder >= 1600",
            target_date="2030-05-01",
            why="Club team selection",
            plan_steps=["- [ ] 1. Read Endgame Guide", "- [ ] 2. Practice section 1"],
            owner_quote="make goal",
            session_id="s1",
            ref=D("2026-09-01"),
        )
        self.assertEqual(g.plan_revision, 1)

        G.revise_plan(
            g,
            new_steps=["- [/] 1. Puzzle workbook", "- [ ] 2. Full mock tournament game"],
            owner_quote="let's change the plan to focus on drilling first",
            session_id="s2",
            ref=D("2026-09-10"),
        )
        self.assertEqual(g.plan_revision, 2)
        reloaded = G.Goal.from_path(g.path)
        self.assertEqual(reloaded.plan_revision, 2)
        self.assertIn("## Plan (revision 2)", reloaded.body)
        self.assertIn("## Plan history", reloaded.body)
        self.assertIn("### Revision 1 (superseded 2026-09-10)", reloaded.body)
        self.assertIn("1. Read Endgame Guide", reloaded.body)
        self.assertIn("1. Puzzle workbook", reloaded.body)

    def test_complete_step_requires_citation_and_updates_movement(self) -> None:
        g = G.commit_goal(
            title="Reach 1600 on the chess puzzle ladder",
            finish_line="Puzzle ladder >= 1600",
            target_date="2030-05-01",
            why="Club team selection",
            plan_steps=["- [ ] 1. Read Endgame Guide", "- [ ] 2. Practice game 1"],
            owner_quote="make goal",
            session_id="s1",
            ref=D("2026-09-01"),
        )
        occ = self.transcript("2026-09-05", "puzzle-done")
        # Completing without citation fails
        with self.assertRaises(ValueError):
            G.complete_step(g, 1, "", "I finished it", "s2", ref=D("2026-09-05"))

        # Completing with citation succeeds
        G.complete_step(g, 1, occ, "I finished the endgame guide", "s2", ref=D("2026-09-05"))
        self.assertEqual(g.last_movement, D("2026-09-05"))
        self.assertEqual(g.last_mention, D("2026-09-05"))

        reloaded = G.Goal.from_path(g.path)
        self.assertIn(f"- [x] 2026-09-05 — 1. Read Endgame Guide — {occ}", reloaded.body)
        self.assertIn("## Owner assertions", reloaded.body)


class DetectorsTest(GoalCase):
    def test_silence_detector_boundary_cases(self) -> None:
        g = G.commit_goal(
            "G1", "F1", None, "W1", ["S1"], "q", "s", ref=D("2026-09-01")
        )
        g.last_mention = D("2026-09-01")
        g.save()

        # Day 9: silence should not fire (limit = 10)
        self.assertFalse(G.silence_detector(g, ref=D("2026-09-10")))
        # Day 10: silence fires
        self.assertTrue(G.silence_detector(g, ref=D("2026-09-11")))
        # If goal is paused, silence does not fire
        g.status = "paused"
        self.assertFalse(G.silence_detector(g, ref=D("2026-09-11")))

    def test_stale_movement_detector_boundary_cases(self) -> None:
        g = G.commit_goal(
            "G1", "F1", None, "W1", ["S1"], "q", "s", ref=D("2026-08-01")
        )
        g.last_movement = D("2026-08-01")
        g.last_mention = D("2026-08-15")  # fresh mention, stale movement
        g.save()

        # Day 20: stale does not fire (limit = 21)
        self.assertFalse(G.stale_detector(g, ref=D("2026-08-21")))
        # Day 21: stale fires
        self.assertTrue(G.stale_detector(g, ref=D("2026-08-22")))

    def test_drift_detector_with_attached_and_unattached_loops(self) -> None:
        # Create 2 loops
        t1 = self.transcript("2026-09-01", "t1")
        t2 = self.transcript("2026-09-01", "t2")
        l1 = V.create_loop("Loop 1 (Unattached recurring)", t1, D("2026-09-01"))
        V.add_occurrence(l1, self.transcript("2026-09-02", "t1-b"), D("2026-09-02"))
        V.add_occurrence(l1, self.transcript("2026-09-03", "t1-c"), D("2026-09-03"))
        V.add_occurrence(l1, self.transcript("2026-09-03", "t1-d"), D("2026-09-03"))

        l2 = V.create_loop("Loop 2 (Attached to goal)", t2, D("2026-09-01"))
        V.add_occurrence(l2, self.transcript("2026-09-02", "t2-b"), D("2026-09-02"))
        V.add_occurrence(l2, self.transcript("2026-09-03", "t2-c"), D("2026-09-03"))
        V.add_occurrence(l2, self.transcript("2026-09-03", "t2-d"), D("2026-09-03"))

        # Goal attaches l2
        g = G.commit_goal(
            "G1", "F1", None, "W1", ["S1"], "q", "s", loops=["L0002"], ref=D("2026-09-01")
        )
        drifts = G.drift_detector(ref=D("2026-09-03"))
        drift_ids = [l.id for l, _ in drifts]
        self.assertIn("L0001", drift_ids)
        self.assertNotIn("L0002", drift_ids, "attached loop must not fire drift detector")

    def test_drift_detector_ignores_loops_that_are_not_open(self) -> None:
        # "What is this for?" is only a fair question about a thread still
        # open. A paused or resolved loop already has its answer on file.
        for i, status in enumerate(("open", "paused", "resolved"), start=1):
            loop = V.create_loop(f"Loop {status}", self.transcript("2026-09-01", f"s{i}"),
                                 D("2026-09-01"))
            for n in range(3):
                V.add_occurrence(loop, self.transcript("2026-09-03", f"s{i}-{n}"),
                                 D("2026-09-03"))
            loop.status = status
            loop.save()
        drift_ids = [l.id for l, _ in G.drift_detector(ref=D("2026-09-03"))]
        self.assertEqual(drift_ids, ["L0001"])


class GoalLintTest(GoalCase):
    def test_clean_goals_pass_lint(self) -> None:
        G.commit_goal("G1", "F1", None, "W1", ["- [ ] Step 1"], "q", "s")
        G.regenerate_catalog()
        self.assertEqual(G.lint(), [])

    def test_numeric_progress_field_fails_lint(self) -> None:
        g = G.commit_goal("G1", "F1", None, "W1", ["- [ ] Step 1"], "q", "s")
        g.body += "\nprogress: 50%\n"
        g.save()
        problems = G.lint()
        self.assertTrue(any("numeric progress" in p for p in problems))

    def test_uncited_done_step_fails_lint(self) -> None:
        g = G.commit_goal("G1", "F1", None, "W1", ["- [x] 2026-09-01 — Step 1"], "q", "s")
        problems = G.lint()
        self.assertTrue(any("done step lacks citation" in p for p in problems))

    def test_clock_sanity_fails_lint_on_inverted_clocks(self) -> None:
        g = G.commit_goal("G1", "F1", None, "W1", ["- [ ] Step 1"], "q", "s", ref=D("2026-09-01"))
        g.last_movement = D("2026-09-05")
        g.last_mention = D("2026-09-02")  # movement > mention is insane
        g.save()
        problems = G.lint()
        self.assertTrue(any("last_movement" in p and "last_mention" in p for p in problems))


class LoopResolvedStatusTest(GoalCase):
    def test_resolved_loop_status_and_reopening(self) -> None:
        t = self.transcript("2026-07-01", "a")
        loop = V.create_loop("Loop to resolve", t, D("2026-07-01"))
        loop.status = "resolved"
        loop.save()
        self.assertEqual(loop.status, "resolved")
        self.assertEqual(V.load_loop(loop.id).status, "resolved")

        # Resurfacing reopens resolved loop to open
        t2 = self.transcript("2026-08-01", "b")
        V.add_occurrence(loop, t2, D("2026-08-01"))
        self.assertEqual(loop.status, "open")

    def test_resolved_loop_uses_terminal_multiplier_for_decay(self) -> None:
        t = self.transcript("2024-11-02", "a")
        loop = V.create_loop("Decaying resolved loop", t, D("2024-11-02"))
        loop.last_seen = D("2024-11-02")
        loop.status = "resolved"
        loop.save()
        # 8w (56d) from 2026-08-01 is 2026-09-26. 8w + 1d (2026-09-27) is not decayed for terminal statuses
        self.assertFalse(loop.is_decayed(D("2026-09-27")), "resolved should use 16w double window")
        # 16w (112d) from 2026-08-01 is 2026-11-21. 2026-11-22 is decayed
        self.assertTrue(loop.is_decayed(D("2026-11-22")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
