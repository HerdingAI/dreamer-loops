#!/usr/bin/env python3
"""Unit tests for the MCP Write Channel & Owner Authorization (§5, §6.1, §9.3)."""

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

import dreamer_common as dc  # noqa: E402
import dreamer_mcp as M  # noqa: E402
import goals as G  # noqa: E402
import questions as Q  # noqa: E402
import vault as V  # noqa: E402

D = _dt.date.fromisoformat


class OwnerWritesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-ow-"))
        for n in ("loops", "goals", "conclusions", "concepts", "archive", "digests",
                  "sources", "meta", "vault", "resurfacings"):
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
            (self.tmp / "vault" / n).mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        self._orig_goals = dict(dc.CFG.get("goals", {}))
        self._orig_mcp = dict(dc.CFG.get("mcp", {}))
        self._orig_decay = dict(dc.CFG["decay"])

        dc.CFG["paths"].update({
            "vault": str(self.tmp / "vault"),
            "loops": str(self.tmp / "loops"),
            "goals": str(self.tmp / "goals"),
            "conclusions": str(self.tmp / "conclusions"),
            "concepts": str(self.tmp / "concepts"),
            "archive": str(self.tmp / "archive"),
            "digests": str(self.tmp / "digests"),
            "sources": str(self.tmp / "sources"),
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
        dc.CFG["mcp"].update({
            "owner_write_quote_min_chars": 8,
            "owner_write_verify_lag_days": 3,
        })
        dc.CFG["decay"].update({
            "go_live_date": "2026-08-01",
            "decay_weeks": 8,
            "terminal_multiplier": 2,
        })

    def tearDown(self) -> None:
        dc.CFG["paths"].clear(); dc.CFG["paths"].update(self._orig_paths)
        dc.CFG.setdefault("goals", {}).clear(); dc.CFG["goals"].update(self._orig_goals)
        dc.CFG.setdefault("mcp", {}).clear(); dc.CFG["mcp"].update(self._orig_mcp)
        dc.CFG["decay"].clear(); dc.CFG["decay"].update(self._orig_decay)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, date: str, slug: str) -> str:
        rel = Path("sources/transcripts") / date[:4] / date[5:7] / f"{date}--{slug}.md"
        full = self.tmp / "vault" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("---\ntype: transcript\n---\n\nbody\n", encoding="utf-8")
        return f"[[{rel.with_suffix('')}]]"


class ToolAuthorizationTest(OwnerWritesCase):
    def test_short_owner_quote_refused(self) -> None:
        res = json.loads(M.tool_commit_goal(
            title="Goal", finish_line="Finish", why="Why", plan=["Step 1"],
            owner_quote="short", session_id="session_123",
        ))
        self.assertIn("error", res)
        self.assertIn("at least 8 characters", res["error"])

    def test_missing_session_id_refused(self) -> None:
        res = json.loads(M.tool_commit_goal(
            title="Goal", finish_line="Finish", why="Why", plan=["Step 1"],
            owner_quote="valid length quote here", session_id="",
        ))
        self.assertIn("error", res)
        self.assertIn("session_id is mandatory", res["error"])


class LegBlockingTest(OwnerWritesCase):
    def test_state_changing_writes_refused_when_owner_write_blocked(self) -> None:
        # Simulate blocked owner-write leg in run-state.json
        state_file = self.tmp / "meta" / "run-state.json"
        state_file.write_text(json.dumps({
            "health": {
                "blocked_legs": ["owner-write"],
                "assertions": [{"ok": False, "blocks": ["owner-write"], "detail": "goal cap violated"}],
            }
        }), encoding="utf-8")

        # 1. commit_goal blocked
        res_commit = json.loads(M.tool_commit_goal(
            title="Goal", finish_line="Finish", why="Why", plan=["Step 1"],
            owner_quote="valid length quote here", session_id="s1",
        ))
        self.assertIn("error", res_commit)
        self.assertIn("owner-write leg is blocked", res_commit["error"])

        # 2. revise_plan blocked
        res_rev = json.loads(M.tool_revise_plan(
            goal_id="G0001", plan=["- [ ] New plan"],
            owner_quote="valid length quote here", session_id="s1",
        ))
        self.assertIn("error", res_rev)
        self.assertIn("owner-write leg is blocked", res_rev["error"])

        # 3. complete_step blocked
        res_step = json.loads(M.tool_complete_step(
            goal_id="G0001", step=1, citation="citation",
            owner_quote="valid length quote here", session_id="s1",
        ))
        self.assertIn("error", res_step)
        self.assertIn("owner-write leg is blocked", res_step["error"])

        # 4. set_status blocked
        res_status = json.loads(M.tool_set_status(
            id="G0001", status="paused",
            owner_quote="valid length quote here", session_id="s1",
        ))
        self.assertIn("error", res_status)
        self.assertIn("owner-write leg is blocked", res_status["error"])

        # 5. answer_question blocked
        res_ans = json.loads(M.tool_answer_question(
            question_id="Q-123", answer="answer",
            owner_quote="valid length quote here", session_id="s1",
        ))
        self.assertIn("error", res_ans)
        self.assertIn("owner-write leg is blocked", res_ans["error"])

        # Evidence-tier write is NOT blocked (§5.4)
        res_evidence = json.loads(M.tool_assert_evidence(
            assertion="I finished section 1",
            owner_quote="finished section 1 today",
            session_id="s1",
        ))
        self.assertTrue(res_evidence.get("ok"))


class EvidenceTriageTest(OwnerWritesCase):
    """Spec §5.2: the owner says what happened off-camera; the tool works out
    what it is about and what it could change, and never changes state itself."""

    def setUp(self) -> None:
        super().setUp()
        self.g = G.commit_goal(
            title="Reach 1600 on the chess puzzle ladder", finish_line="Puzzle ladder shows 1600",
            target_date=None, why="Club team selection",
            plan_steps=["Finish the puzzle workbook", "Play one timed game a day",
                        "Enter the spring club tournament"],
            owner_quote="commit goal 1", session_id="s1", ref=D("2026-08-01"),
        )

    def evidence(self, assertion: str, **kw) -> dict:
        kw.setdefault("owner_quote", assertion)
        kw.setdefault("session_id", "s2")
        return json.loads(M.tool_assert_evidence(assertion=assertion, **kw))

    def test_without_a_target_it_offers_candidates_and_writes_nothing(self) -> None:
        res = self.evidence("I did two puzzle sections on Saturday")
        self.assertFalse(res["ok"])
        self.assertTrue(res["needs_target"])
        self.assertEqual([c["id"] for c in res["candidates"]], ["G0001"])
        self.assertEqual(G.load_goal("G0001").last_mention, D("2026-08-01"))
        self.assertEqual(len(Q.load_owner_writes()), 0, "ambiguity is a question, never a write")

    def test_matching_nothing_is_recorded_unattached(self) -> None:
        res = self.evidence("I organised the toolbox")
        self.assertTrue(res["ok"])
        self.assertIsNone(res["matched_goal"])
        self.assertEqual(Q.load_owner_writes()[0]["target_type"], "assertion")

    def test_unknown_target_is_an_error_not_a_silent_unattached_write(self) -> None:
        for kw in ({"goal_id": "G0099"}, {"loop_id": "L0099"}):
            res = self.evidence("I did two puzzle sections", **kw)
            self.assertIn("error", res)
        self.assertEqual(Q.load_owner_writes(), [])

    def test_a_mention_moves_only_the_mention_clock_and_lands_on_the_page(self) -> None:
        res = self.evidence("I have been reading about tournament dates",
                            goal_id="G0001", date="2026-08-20")
        self.assertTrue(res["ok"])
        g = G.load_goal("G0001")
        self.assertEqual((g.last_mention, g.last_movement), (D("2026-08-20"), D("2026-08-01")))
        self.assertEqual(g.status, "active")
        assertions = g.body.split("## Owner assertions")[1]
        self.assertIn("I have been reading about tournament dates", assertions)
        self.assertNotIn("[x]", g.body, "evidence never completes a step")

    def test_movement_moves_both_clocks_and_never_backwards(self) -> None:
        self.evidence("I did a timed section", goal_id="G0001", kind="movement",
                      date="2026-08-20")
        self.evidence("I also did one earlier", goal_id="G0001", kind="movement",
                      date="2026-08-10")
        g = G.load_goal("G0001")
        self.assertEqual((g.last_mention, g.last_movement), (D("2026-08-20"), D("2026-08-20")))

    def test_bad_kind_and_future_dates_are_refused(self) -> None:
        self.assertIn("error", self.evidence("x happened today ok", goal_id="G0001", kind="done"))
        future = (dc.today() + _dt.timedelta(days=2)).isoformat()
        self.assertIn("error", self.evidence("x happened today ok", goal_id="G0001", date=future))
        self.assertEqual(Q.load_owner_writes(), [])

    def test_consequences_name_the_open_steps_and_flag_the_likely_one(self) -> None:
        G.complete_step("G0001", 1, "assertion: said so", "finished the puzzle workbook", "s3",
                        ref=D("2026-08-05"))
        res = self.evidence("I played a timed game this morning",
                            goal_id="G0001", kind="movement")
        steps = res["consequences"]["open_steps"]
        self.assertEqual([s["step"] for s in steps], [2, 3], "done steps are not offered")
        self.assertEqual([s["step"] for s in steps if s["related"]], [2])
        self.assertEqual(res["consequences"]["finish_line"], "Puzzle ladder shows 1600")
        self.assertIn("complete_step", res["next"])

    def test_unrelated_evidence_flags_no_step(self) -> None:
        res = self.evidence("I talked to a club member about openings", goal_id="G0001")
        self.assertEqual([s for s in res["consequences"]["open_steps"] if s["related"]], [])

    def test_loop_evidence_offers_resolution_without_resolving(self) -> None:
        tr = self.tmp / "vault" / "sources" / "transcripts" / "2026" / "08"
        tr.mkdir(parents=True, exist_ok=True)
        (tr / "2026-08-01--houseplant.md").write_text("## Human\n\nx\n", encoding="utf-8")
        loop = V.create_loop("Which houseplant survives the north window?",
                             "[[sources/transcripts/2026/08/2026-08-01--houseplant]]", D("2026-08-01"))
        res = self.evidence("The snake plant settled the north-window question", loop_id=loop.id)
        self.assertTrue(res["ok"])
        self.assertEqual(res["matched_loop"], loop.id)
        self.assertIn("resolved", res["next"])
        self.assertEqual(V.load_loop(loop.id).status, "open")


class PlanRevisionKeepsProgressTest(OwnerWritesCase):
    """Rewriting the route must not quietly un-do work already cited as done."""

    def setUp(self) -> None:
        super().setUp()
        G.commit_goal(
            title="Throw a bowl on the pottery wheel", finish_line="A thrown bowl that holds water",
            target_date=None, why="Weekend hobby",
            plan_steps=["List three nearby pottery classes",
                        "Book a taster class", "Attend one class a week"],
            owner_quote="commit goal 1", session_id="s1", ref=D("2026-08-01"))
        G.complete_step("G0001", 1, "assertion: it is built", "the class list is built", "s2",
                        ref=D("2026-08-05"))

    def revise(self, plan: list[str]) -> dict:
        return json.loads(M.tool_revise_plan(goal_id="G0001", plan=plan,
                                             owner_quote="change the plan like that", session_id="s3"))

    def test_a_reworded_done_step_stays_done_with_its_citation(self) -> None:
        res = self.revise(["List three nearby pottery classes", "Practise centring clay",
                           "Attend two classes a week"])
        self.assertTrue(res["ok"])
        g = G.load_goal("G0001")
        self.assertEqual([n for n, _ in G.open_steps(g)], [2, 3])
        current = g.body.split("## Plan history")[0]
        self.assertIn("- [x] 2026-08-05 — List three nearby pottery classes — assertion: it is built",
                      current)
        self.assertNotIn("no longer in the plan", res["confirmation"])

    def test_dropping_a_done_step_is_said_out_loud(self) -> None:
        res = self.revise(["Practise centring clay", "Attend two classes a week"])
        self.assertIn("no longer in the plan", res["confirmation"])
        self.assertIn("List three nearby pottery classes", res["confirmation"])
        self.assertIn("List three nearby pottery classes", G.load_goal("G0001").body,
                      "it survives in the plan history")


class QuestionToolsTest(OwnerWritesCase):
    def test_leave_it_defers_instead_of_answering(self) -> None:
        q = Q.enqueue_question("silence", "Chess quiet for 10 days?", goal_id="G0001")
        res = json.loads(M.tool_answer_question(
            question_id=q["id"], answer="not now", defer=True,
            owner_quote="leave it for now please", session_id="s1"))
        self.assertTrue(res["ok"])
        self.assertIn("will not be asked again before", res["confirmation"])
        stored = Q.load_questions()[0]
        self.assertEqual(stored["status"], "deferred")
        self.assertEqual(stored["last_response"], "leave it for now please")
        self.assertEqual(Q.list_questions(surface="async"), [])

    def test_listing_questions_does_not_spend_the_async_ask(self) -> None:
        q = Q.enqueue_question("silence", "Chess quiet for 10 days?", goal_id="G0001")
        Q.enqueue_question("drift", "What is this for?", loop_id="L0001")
        res = json.loads(M.tool_list_open_questions())
        self.assertEqual(res["count"], 2)
        self.assertEqual({x["id"] for x in res["questions"]} >= {q["id"]}, True)
        self.assertEqual(Q.load_questions()[0]["status"], "pending")


class WriteToolsExecutionTest(OwnerWritesCase):
    def test_commit_goal_and_owner_writes_recording(self) -> None:
        res = json.loads(M.tool_commit_goal(
            title="Reach 1600 on the chess puzzle ladder",
            finish_line="Puzzle ladder >= 1600",
            why="Club team selection",
            plan=["- [ ] 1. Puzzle workbook", "- [ ] 2. Timed game"],
            owner_quote="yes let's make the chess ladder a committed goal",
            session_id="session_01ABC",
            target_date="2030-05-01",
        ))
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["goal_id"], "G0001")
        self.assertIn("Noted: Goal G0001 committed", res["confirmation"])

        # Check owner-writes.json
        writes = Q.load_owner_writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["tool"], "commit_goal")
        self.assertEqual(writes[0]["target_id"], "G0001")
        self.assertEqual(writes[0]["verified"], None)

    def test_complete_step_and_revise_plan(self) -> None:
        M.tool_commit_goal(
            title="Goal 1", finish_line="F1", why="W1",
            plan=["- [ ] 1. Step one", "- [ ] 2. Step two"],
            owner_quote="commit goal 1 now", session_id="s1",
        )
        occ = self.transcript("2026-09-02", "step1-done")

        # Complete step 1 with plain text citation (should normalize to assertion:)
        res_step = json.loads(M.tool_complete_step(
            goal_id="G0001",
            step=1,
            citation="owner assertion on test section",
            owner_quote="I finished step one today",
            session_id="s2",
        ))
        self.assertTrue(res_step.get("ok"))
        self.assertIn("G0001 step marked done", res_step["confirmation"])

        g = G.load_goal("G0001")
        # The step is stamped with the day it was completed, not a fixed date.
        today = G.today().isoformat()
        self.assertIn(f"- [x] {today} — 1. Step one — assertion: owner assertion on test section", g.body)

        # Revise plan
        res_rev = json.loads(M.tool_revise_plan(
            goal_id="G0001",
            plan=["- [/] 1. New active step", "- [ ] 2. Later step"],
            owner_quote="let's revise the plan for goal 1",
            session_id="s3",
        ))
        self.assertTrue(res_rev.get("ok"))
        self.assertEqual(res_rev["plan_revision"], 2)

    def test_set_status_goal_and_loop(self) -> None:
        M.tool_commit_goal(
            title="Goal 1", finish_line="F1", why="W1",
            plan=["- [ ] 1. Step one"],
            owner_quote="commit goal 1 now", session_id="s1",
        )
        # Pause goal
        res_pause = json.loads(M.tool_set_status(
            id="G0001",
            status="paused",
            owner_quote="pause this goal for now",
            session_id="s2",
        ))
        self.assertTrue(res_pause.get("ok"))
        self.assertEqual(res_pause["status"], "paused")

        # Resolve loop
        t = self.transcript("2026-07-01", "loop-trans")
        loop = V.create_loop("Loop to resolve", t, D("2026-07-01"))
        res_loop = json.loads(M.tool_set_status(
            id=loop.id,
            status="resolved",
            owner_quote="settled this outside the system",
            session_id="s3",
        ))
        self.assertTrue(res_loop.get("ok"))
        self.assertEqual(res_loop["status"], "resolved")

    def test_assert_evidence_sync_triage_and_clock_reset(self) -> None:
        g = G.commit_goal(
            title="Reach 1600 on the chess puzzle ladder", finish_line="Puzzle ladder >= 1600",
            target_date=None, why="Club team selection", plan_steps=["- [ ] 1. Step"],
            owner_quote="commit goal 1", session_id="s1", ref=D("2026-08-01"),
        )
        g.last_mention = D("2026-08-01")
        g.save()

        res = json.loads(M.tool_assert_evidence(
            assertion="I played a timed game today and reached 1650",
            owner_quote="played a timed game reached 1650",
            session_id="s2",
            goal_id="G0001",
        ))
        self.assertTrue(res.get("ok"))

        reloaded = G.load_goal("G0001")
        self.assertEqual(reloaded.last_mention, dc.today())

    def test_answer_silence_question_resets_goal_mention_clock(self) -> None:
        g = G.commit_goal(
            title="Reach 1600 on the chess puzzle ladder", finish_line="Puzzle ladder >= 1600",
            target_date=None, why="Club team selection", plan_steps=["- [ ] 1. Step"],
            owner_quote="commit goal 1", session_id="s1", ref=D("2026-08-01"),
        )
        g.last_mention = D("2026-08-01")
        g.save()

        q = Q.enqueue_question("silence", "Still working on G0001?", goal_id="G0001")
        res = json.loads(M.tool_answer_question(
            question_id=q["id"],
            answer="Yes, resuming today",
            owner_quote="yes, resuming today",
            session_id="s1",
        ))
        self.assertTrue(res.get("ok"))
        self.assertEqual(Q.load_questions()[0]["status"], "answered")

        reloaded = G.load_goal("G0001")
        self.assertEqual(reloaded.last_mention, dc.today())

    def test_pending_questions_envelope_marks_asked_and_throttles(self) -> None:
        # Enqueue silence question
        Q.enqueue_question("silence", "Chess quiet for 10 days?", goal_id="G0001")

        call_msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {
                "name": "list_active_goals",
                "arguments": {},
            },
        }
        res1 = M.handle(call_msg)
        self.assertIsNotNone(res1)
        result1 = res1.get("result", {})
        self.assertIn("pending_questions", result1)
        self.assertEqual(len(result1["pending_questions"]), 1)
        self.assertEqual(result1["pending_questions"][0]["text"], "Chess quiet for 10 days?")
        # MCP clients hand the model `content` only. A question that lives
        # solely in a sibling key is marked asked without ever being seen.
        shown = "\n".join(c["text"] for c in result1["content"])
        self.assertIn("Chess quiet for 10 days?", shown)

        # 2nd tool call: Question must NOT be repeated (§6.3 async throttle)
        res2 = M.handle(call_msg)
        result2 = res2.get("result", {})
        self.assertNotIn("pending_questions", result2)

    def test_tool_get_goal_priority_lookup(self) -> None:
        G.commit_goal(
            title="G0002 prep work", finish_line="F1", target_date=None, why="W1",
            plan_steps=["- [ ] Step 1"], owner_quote="commit g1", session_id="s1",
        )
        G.commit_goal(
            title="Real target", finish_line="F2", target_date=None, why="W2",
            plan_steps=["- [ ] Step 2"], owner_quote="commit g2", session_id="s2",
        )
        # Querying by ID "G0002" should return G0002 ("Real target"), not G0001 whose title contains "G0002"
        res = json.loads(M.tool_get_goal("G0002"))
        self.assertEqual(res["id"], "G0002")
        self.assertEqual(res["title"], "Real target")


if __name__ == "__main__":
    unittest.main(verbosity=2)
