#!/usr/bin/env python3
"""Tests for the pre-mortem, never-discussed check and consequence judge. Fixtures are synthetic. The model is
tests/fake_claude.py, retrieval is a stub QmdClient, and every path is a temp dir:
nothing here touches the real vault, the real qmd or a real claude."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import dreamer_common as dc  # noqa: E402
import frontier as F  # noqa: E402
import frontier_gaps as FG  # noqa: E402
import frontier_llm as L  # noqa: E402

AT = "2030-01-15T19:00:00"
GOAL = "G0003"
CAUSE = "The mugs crack in the first firing"
GAP = "Fire a test tile before the full load"
SUNK = "Thermal shock and glaze crawling"
OWNER_WORDS = "Ran a test tile first: a single tile went through the kiln before the full load."
ASSISTANT_WORDS = "A test tile means firing a sample before you load the kiln."


def transcript_page(date: str, turns: list[tuple[str, str]]) -> str:
    body = "".join(f"## {role}\n\n{text}\n\n" for role, text in turns)
    return f"---\ntype: transcript\ndate: {date}\nmessage_count: {len(turns)}\n---\n\n# t\n\n{body}"


class FakeQmd:
    """Stub QmdClient: scripted hits per collection for the hybrid query and, separately,
    for the lex-only title search; either kind can be degraded per collection."""

    def __init__(self, hits: dict[str, list[dict]] | None = None, degraded: set[str] | None = None,
                 title_hits: dict[str, list[dict]] | None = None, degraded_lex: set[str] | None = None):
        self.hits = hits or {}
        self.degraded = degraded or set()
        self.title_hits = title_hits or {}
        self.degraded_lex = degraded_lex or set()
        self.calls: list[tuple[str, str, int | None, bool]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def query(self, text, collection, limit=None, lex_only=False):
        self.calls.append((collection, text, limit, lex_only))
        if collection in (self.degraded_lex if lex_only else self.degraded):
            return [], True
        return list((self.title_hits if lex_only else self.hits).get(collection, []))[:limit], False


def hit(file: str, line: int = 1, score: float = 0.9) -> dict:
    return {"docid": "#x", "file": file, "line": line, "title": "", "score": score, "snippet": ""}


class GapsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-gaps-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.dirs = {"vault": self.vault, "sources": self.vault / "sources" / "transcripts",
                     "meta": self.vault / ".vault-meta", "loops": self.vault / "loops",
                     "goals": self.vault / "goals", "frontier": self.vault / "frontier",
                     "conclusions": self.vault / "conclusions", "logs": self.tmp / "logs"}
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        (self.dirs["frontier"] / "items").mkdir(exist_ok=True)
        self._paths = dict(dc.CFG["paths"])
        self._frontier = dict(dc.CFG["frontier"])
        dc.CFG["paths"].update({k: str(v) for k, v in self.dirs.items()})
        dc.CFG["frontier"].update({"vault_lock_wait_seconds": 1, "premortem_every_days": 7,
                                   "check_top_k": 5, "check_title_lex_k": 3,
                                   "max_candidates_per_cause": 3,
                                   "max_parse_attempts": 3, "parse_retries": 1})
        self.addCleanup(self.restore)
        self.script = self.tmp / "script.json"
        self.set_script()
        env = {"DREAMER_FAKE_CLAUDE": str(ROOT / "tests" / "fake_claude.py"),
               "DREAMER_FAKE_FRONTIER_SCRIPT": str(self.script)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("DREAMER_FAKE_FRONTIER_FAIL", "DREAMER_LOCK_HELD"):
            os.environ.pop(k, None)
        self.write_goal(GOAL, "active")

    def restore(self):
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._paths)
        dc.CFG["frontier"].clear()
        dc.CFG["frontier"].update(self._frontier)

    # helpers ---------------------------------------------------------------
    def set_script(self, premortem=None, check=None, consequence=None) -> None:
        self.script.write_text(json.dumps({
            "premortem": premortem if premortem is not None else
            {GOAL: premortem_reply([(CAUSE, [(GAP, "Fire one tile before you fill the kiln.")])])},
            "check": check or {}, "consequence": consequence or {}}))

    def write_goal(self, gid, status, revision=1, created="2029-11-21"):
        (self.dirs["goals"] / f"{gid}.md").write_text(
            f"---\ntype: goal\nid: {gid}\nstatus: {status}\ntitle: Fire a set of mugs\n"
            f"created: {created}\nplan_revision: {revision}\ntarget_date: 2027-06-30\n"
            f"finish_line: Four mugs are fired and glazed.\nwhy: A weekend hobby.\n---\n\n"
            f"# g\n\n## Plan (revision {revision})\n\n- [ ] Pick a clay body\n- [ ] Throw the first mug\n\n"
            f"## Assessment (derived — hypothesis, not evidence)\n\nSECRET-ASSESSMENT-TEXT\n",
            encoding="utf-8")

    def goal(self, gid=GOAL):
        import goals as G
        return G.load_goal(gid)

    def transcript(self, slug, turns, date="2029-12-21"):
        y, m = date[:4], date[5:7]
        path = self.dirs["sources"] / y / m / f"{date}--{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript_page(date, turns), encoding="utf-8")
        return path

    def hit_for(self, path: Path, needle: str, score=0.9) -> dict:
        lines = path.read_text(encoding="utf-8").splitlines()
        n = next(i for i, l in enumerate(lines, 1) if needle in l)
        # Real qmd reports its display path, not the file name: "<date>--<slug>.md"
        # comes back as "<date>-<slug>.md" (qmd handelize). Fixture slugs are plain
        # words, so the "--" is the only character run it rewrites.
        rel = str(path.relative_to(self.dirs["sources"])).replace("--", "-")
        return hit(f"transcripts/{rel}", n, score)

    def rel(self, path: Path) -> str:
        return "sources/transcripts/" + str(path.relative_to(self.dirs["sources"]).with_suffix(""))

    def item(self, title, quadrant="known_known", goals=None, evidence=None, origin="map") -> F.Item:
        it = F.Item(id=F.next_item_id(), title=title, summary=f"About {title}.", quadrant=quadrant,
                    origin=origin, goals=goals or [], evidence=evidence or [])
        it.save()
        return it

    def run_goal(self, qmd=None, at=AT, goal=None):
        return FG.run_goal(goal or self.goal(), qmd_factory=lambda: qmd or FakeQmd(), at=at)

    def record(self, gid=GOAL) -> dict:
        rec = FG.latest_record(gid)
        self.assertIsNotNone(rec, "no pre-mortem record was written")
        return rec

    def prompt(self, call):
        f = self.dirs["logs"] / f".prompt-frontier-{call}.md"
        return f.read_text(encoding="utf-8") if f.exists() else None

    def clear_prompts(self):
        for f in self.dirs["logs"].glob(".prompt-frontier-*.md"):
            f.unlink()

    def some_qmd(self) -> FakeQmd:
        """Retrieval with one unrelated hit: with no hits at all the check is
        answered `none` without a model call."""
        t = self.transcript("unrelated", [("Human", "Notes about the herb planter.")], date="2029-11-21")
        return FakeQmd({"transcripts": [self.hit_for(t, "planter")]})

    def candidate(self, rec=None, title=GAP):
        rec = rec or self.record()
        return next(c for c in rec["candidates"] if c["title"] == title)


def premortem_reply(causes: list[tuple[str, list[tuple[str, str]]]]) -> dict:
    return {"causes": [{"cause": c, "candidates": [{"title": t, "summary": s} for t, s in cands]}
                       for c, cands in causes]}


def check_reply(owner="none", quote="", item=None, exposed=None, requires=None) -> dict:
    out = {"owner_discussed": owner, "quote": quote, "matches_item": item,
           "exposed_in": exposed or []}
    if requires:
        out["requires"] = requires  # fake-only: the fake answers "none" unless this text reached the prompt
    return out


# ==========================================================================
# Parsers
# ==========================================================================

class ParsePremortemTest(unittest.TestCase):
    def parse(self, obj, **kw):
        return L.parse_premortem(json.dumps(obj), max_candidates=kw.get("m", 2), max_causes=kw.get("c", 3))

    def test_valid(self):
        out = self.parse(premortem_reply([("a", [("x", "y")]), ("b", [("z", "w")])]))
        self.assertEqual([c["cause"] for c in out], ["a", "b"])
        self.assertEqual(out[0]["candidates"][0], {"title": "x", "summary": "y"})

    def test_rejects_shape_errors(self):
        good = premortem_reply([("a", [("x", "y")])])
        for bad in ({"causes": []}, {**good, "extra": 1},
                    {"causes": [{"cause": "a", "candidates": []}]},
                    {"causes": [{"cause": "", "candidates": [{"title": "x", "summary": "y"}]}]},
                    {"causes": [{"cause": "a", "candidates": [{"title": "x"}]}]},
                    premortem_reply([("a", [("x", "y"), ("x2", "y"), ("x3", "y")])]),
                    premortem_reply([("a", [("x", "y")])] * 4)):
            with self.assertRaises(L.ParseFailure, msg=json.dumps(bad)[:60]):
                self.parse(bad)

    def test_rejects_prose_and_tolerates_fence(self):
        text = json.dumps(premortem_reply([("a", [("x", "y")])]))
        self.assertEqual(len(L.parse_premortem(f"```json\n{text}\n```", 2, 3)), 1)
        with self.assertRaises(L.ParseFailure):
            L.parse_premortem(f"Here:\n{text}", 2, 3)


class ParseCheckTest(unittest.TestCase):
    ITEMS = {"K7001"}
    TRANSCRIPTS = {"sources/transcripts/2026/10/a"}

    def parse(self, obj):
        return L.parse_check(json.dumps(obj), self.ITEMS, self.TRANSCRIPTS)

    def test_valid_verdicts(self):
        self.assertEqual(self.parse(check_reply())["owner_discussed"], "none")
        got = self.parse(check_reply("explained", "my words", "K7001", ["sources/transcripts/2026/10/a"]))
        self.assertEqual(got["matches_item"], "K7001")

    def test_rejects_bad_replies(self):
        for bad in (check_reply("maybe"), check_reply("explained", ""), check_reply("none", "stray quote"),
                    check_reply(item="K9999"), check_reply(exposed=["sources/transcripts/other"]),
                    {"owner_discussed": "none"}, {**check_reply(), "extra": 1},
                    {**check_reply(), "exposed_in": "x"}):
            with self.assertRaises(L.ParseFailure, msg=json.dumps(bad)[:60]):
                self.parse(bad)


class ParseConsequenceTest(unittest.TestCase):
    def test_valid_and_invalid(self):
        out = L.parse_consequence('{"verdict": "tactical", "reason": "Looked up when needed."}')
        self.assertEqual(out, {"verdict": "tactical", "reason": "Looked up when needed."})
        for bad in ('{"verdict": "maybe", "reason": "x"}', '{"verdict": "tactical", "reason": ""}',
                    '{"verdict": "tactical"}', "tactical",
                    '{"verdict": "tactical", "reason": "x", "extra": 1}'):
            with self.assertRaises(L.ParseFailure, msg=bad):
                L.parse_consequence(bad)


# ==========================================================================
# Prompts and config
# ==========================================================================

class PromptContractTest(unittest.TestCase):
    def read(self, name):
        return (ROOT / "skills" / f"frontier-{name}" / "PROMPT.md").read_text(encoding="utf-8")

    def test_markers_match_calls(self):
        for name, marker in (("premortem", "# Frontier premortem"), ("check", "# Frontier check"),
                             ("consequence", "# Frontier consequence")):
            self.assertEqual(self.read(name).split("\n", 1)[0].strip(), marker)
            self.assertEqual(L.CALLS[name].marker, marker)

    def test_premortem_forces_outcome_causes_not_plan_steps(self):
        text = self.read("premortem").lower()
        self.assertIn("a year from now", text)
        self.assertIn("failed", text)
        self.assertIn("plan steps", text)
        self.assertIn("do not derive", text)
        self.assertIn("most likely", text)

    def test_consequence_uses_the_fixed_rubric(self):
        text = self.read("consequence").lower()
        for phrase in ("consequential", "tactical", "by itself", "finish line", "major decision",
                       "looked up when needed"):
            self.assertIn(phrase, text)

    def test_check_separates_owner_from_derived(self):
        text = self.read("check").lower()
        for phrase in ("derived", "exposure", "only the owner", "verbatim"):
            self.assertIn(phrase, text)

    def test_config_keys_and_turn_budgets(self):
        f = dc.CFG["frontier"]
        for key in ("premortem_every_days", "premortem_map_items", "premortem_max_causes",
                    "max_candidates_per_cause", "check_top_k", "check_title_lex_k", "check_collections",
                    "check_passage_chars", "premortems_dir"):
            self.assertIn(key, f, key)
        self.assertEqual(set(f["check_collections"]), {"frontier", "transcripts", "vault", "conclusions"})
        for call in ("premortem", "check", "consequence"):
            turns = dc.CFG["budget"][f"max_turns_frontier_{call}"]
            self.assertTrue(1 <= int(turns) <= 4, call)


# ==========================================================================
# Due logic
# ==========================================================================

class DueGoalsTest(GapsCase):
    TODAY = dt.date(2030, 1, 15)

    def seed_record(self, date="2030-01-13", revision=1, candidates=None, gid=GOAL):
        FG.write_record(gid, {"date": date, "plan_revision": revision, "causes": [],
                              "candidates": candidates or []})

    def due(self):
        return [g.id for g in FG.due_goals(today=self.TODAY)]

    def test_no_record_is_due(self):
        self.assertEqual(self.due(), [GOAL])

    def test_recent_record_is_not_due(self):
        self.seed_record("2030-01-13")
        self.assertEqual(self.due(), [])

    def test_old_record_is_due(self):
        self.seed_record("2029-12-30")
        self.assertEqual(self.due(), [GOAL])

    def test_exactly_the_interval_is_not_yet_older(self):
        self.seed_record("2030-01-08")  # 7 days old; frontier.premortem_every_days is 7
        self.assertEqual(self.due(), [])

    def test_newer_plan_revision_is_due(self):
        self.seed_record("2030-01-14", revision=1)
        self.write_goal(GOAL, "active", revision=2)
        self.assertEqual(self.due(), [GOAL])

    def test_goal_created_after_the_record_is_due(self):
        self.seed_record("2030-01-09")
        self.write_goal(GOAL, "active", created="2030-01-11")
        self.assertEqual(self.due(), [GOAL])

    def test_paused_achieved_and_abandoned_goals_are_never_due(self):
        for status in ("paused", "achieved", "abandoned"):
            self.write_goal(GOAL, status)
            self.assertEqual(self.due(), [], status)

    def test_deferred_candidate_at_the_current_revision_is_due_again(self):
        self.seed_record("2030-01-14", candidates=[{"title": "x", "status": "deferred", "reason": "retrieval degraded"}])
        self.assertEqual(self.due(), [GOAL])

    def test_skipped_candidate_is_not_retried(self):
        self.seed_record("2030-01-14", candidates=[{"title": "x", "status": "skipped", "reason": "parse"}])
        self.assertEqual(self.due(), [])

    def test_latest_record_wins_over_an_older_one(self):
        self.seed_record("2029-12-21")
        self.seed_record("2030-01-14")
        self.assertEqual(self.due(), [])


# ==========================================================================
# The per-goal run
# ==========================================================================

class PremortemInputTest(GapsCase):
    def test_prompt_holds_the_goal_page_and_map_titles_never_transcripts(self):
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        known = self.item("Wedging technique", "known_known")
        qmd = FakeQmd({"frontier": [hit(f"items/{known.id}.md")]})
        self.run_goal(qmd)
        prompt = self.prompt("premortem")
        self.assertTrue(prompt.startswith("# Frontier premortem\n"))
        for needle in ("Fire a set of mugs", "Four mugs are fired and glazed.", "A weekend hobby.",
                       "2027-06-30", "Pick a clay body", "Wedging technique | known_known"):
            self.assertIn(needle, prompt)
        self.assertNotIn(OWNER_WORDS, prompt)
        self.assertNotIn("SECRET-ASSESSMENT-TEXT", prompt)   # derived assessment is not an input
        self.assertTrue(any(c[0] == "frontier" and "Fire a set of mugs" in c[1] for c in qmd.calls))
        self.assertIsNotNone(t)

    def test_map_items_are_capped(self):
        dc.CFG["frontier"]["premortem_map_items"] = 2
        hits = []
        for i in range(4):
            it = self.item(f"Concept {i}")
            hits.append(hit(f"items/{it.id}.md", score=1.0 - i / 10))
        self.run_goal(FakeQmd({"frontier": hits}))
        prompt = self.prompt("premortem")
        self.assertIn("Concept 0", prompt)
        self.assertIn("Concept 1", prompt)
        self.assertNotIn("Concept 2", prompt)

    def test_degraded_map_retrieval_defers_the_goal_without_a_record(self):
        self.item("Wedging technique")
        res = self.run_goal(FakeQmd(degraded={"frontier"}))
        self.assertEqual(res["status"], "deferred")
        self.assertIsNone(FG.latest_record(GOAL))
        self.assertIsNone(self.prompt("premortem"))


class RunGoalTest(GapsCase):
    def test_no_owner_discussion_makes_an_unknown_unknown_and_judges_it(self):
        self.set_script(consequence={GAP.lower(): {"verdict": "consequential",
                                                   "reason": "Without a test tile the whole load is at risk."}})
        res = self.run_goal()
        self.assertEqual(res["status"], "done")
        items = [F.Item.from_path(p) for p in F.items_dir().glob("K*.md")]
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it.title, it.quadrant, it.origin), (GAP, "unknown_unknown", "premortem"))
        self.assertEqual([m["to"] for m in it.moves], ["unknown_unknown"])
        self.assertEqual(it.goals, [{"goal": GOAL, "relation": "unconnected"}])
        c = self.candidate()
        self.assertEqual((c["status"], c["check"], c["item"], c["consequence"]),
                         ("done", "none", it.id, "consequential"))
        self.assertIn("load", c["consequence_reason"])

    def test_tactical_survivor_stays_in_the_map_and_is_recorded_not_queued(self):
        self.set_script(consequence={GAP.lower(): {"verdict": "tactical", "reason": "Can be looked up when needed."}})
        self.run_goal()
        it = F.Item.from_path(next(F.items_dir().glob("K*.md")))
        self.assertEqual(it.quadrant, "unknown_unknown")
        self.assertEqual(it.lessons, [])
        c = self.candidate()
        self.assertEqual((c["consequence"], c["status"]), ("tactical", "done"))
        self.assertFalse(list(F.lessons_dir().glob("*.md")) if F.lessons_dir().exists() else [])

    def test_owner_explanation_becomes_evidence_on_the_existing_item_and_no_judge_runs(self):
        t = self.transcript("test-tile", [("Human", OWNER_WORDS), ("Assistant", ASSISTANT_WORDS)])
        existing = self.item("Bisque firing", "known_known", goals=[{"goal": GOAL, "relation": "connected"}])
        self.set_script(check={GAP.lower(): check_reply("explained", OWNER_WORDS, existing.id)})
        qmd = FakeQmd({"transcripts": [self.hit_for(t, "test tile")],
                       "frontier": [hit(f"items/{existing.id}.md")]})
        self.run_goal(qmd)
        items = list(F.items_dir().glob("K*.md"))
        self.assertEqual(len(items), 1, "no second item for a matched candidate")
        it = F.Item.from_path(items[0])
        self.assertEqual(it.quadrant, "known_known")
        self.assertTrue(any(e["kind"] == "explained" and e["quote"] == OWNER_WORDS
                            and e["transcript"] == self.rel(t) for e in it.evidence))
        self.assertIsNone(self.prompt("consequence"), "an owner-known connected item is never judged")
        c = self.candidate()
        self.assertEqual((c["check"], c["item"], c["consequence"]), ("explained", existing.id, ""))
        self.assertEqual(it.lessons, [])

    def test_unconnected_known_item_is_an_unknown_known_and_is_judged(self):
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        existing = self.item("Bisque firing", "unknown_known", goals=[{"goal": GOAL, "relation": "unconnected"}],
                             evidence=[{"kind": "explained", "transcript": self.rel(t), "date": "2029-12-21",
                                        "quote": OWNER_WORDS}])
        self.set_script(check={GAP.lower(): check_reply("explained", OWNER_WORDS, existing.id)},
                        consequence={GAP.lower(): {"verdict": "consequential", "reason": "Decides the clay body."}})
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "test tile")],
                               "frontier": [hit(f"items/{existing.id}.md")]}))
        self.assertEqual(self.candidate()["consequence"], "consequential")
        self.assertIn("Fire a set of mugs", self.prompt("consequence"))

    def test_owner_discussion_with_no_matching_item_creates_it_where_the_evidence_derives(self):
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        self.set_script(check={GAP.lower(): check_reply("asked", OWNER_WORDS)})
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "test tile")]}))
        it = F.Item.from_path(next(F.items_dir().glob("K*.md")))
        self.assertEqual((it.quadrant, it.origin), ("known_unknown", "premortem"))
        self.assertIsNone(self.prompt("consequence"))

    def test_reconstructed_verdict_needs_a_reconstructed_turn(self):
        t = self.transcript("rec", [("Human (reconstructed)", OWNER_WORDS)])
        self.set_script(check={GAP.lower(): check_reply("reconstructed", OWNER_WORDS)})
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "test tile")]}))
        it = F.Item.from_path(next(F.items_dir().glob("K*.md")))
        self.assertEqual(it.quadrant, "known_unknown")
        self.assertEqual(it.evidence[0]["kind"], "reconstructed")

    def test_degraded_search_defers_the_candidate_and_creates_nothing(self):
        for dead in ("frontier", "transcripts", "vault", "conclusions"):
            with self.subTest(collection=dead):
                for p in F.items_dir().glob("K*.md"):
                    p.unlink()
                shutil.rmtree(self.dirs["frontier"] / "premortems", ignore_errors=True)
                self.run_goal(FakeQmd(degraded={dead}))
                self.assertEqual(list(F.items_dir().glob("K*.md")), [])
                c = self.candidate()
                self.assertEqual(c["status"], "deferred")
                self.assertIn("degraded", c["reason"])
                self.assertIsNone(self.prompt("consequence"))

    def test_unverifiable_quote_defers_instead_of_calling_it_never_discussed(self):
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        self.set_script(check={GAP.lower(): check_reply("explained", "I never said this sentence at all")})
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "test tile")]}))
        self.assertEqual(list(F.items_dir().glob("K*.md")), [])
        c = self.candidate()
        self.assertEqual(c["status"], "deferred")
        self.assertIn("quote", c["reason"])

    def test_a_quote_that_keeps_failing_is_skipped_not_retried_forever(self):
        # The same passages tend to yield the same unverifiable quote: count it like a bad reply.
        dc.CFG["frontier"]["max_parse_attempts"] = 2
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        self.set_script(check={GAP.lower(): check_reply("explained", "I never said this sentence at all")})
        qmd = lambda: FakeQmd({"transcripts": [self.hit_for(t, "test tile")]})  # noqa: E731
        self.run_goal(qmd())
        self.assertEqual((self.candidate()["status"], self.candidate()["attempts"]), ("deferred", 1))
        self.run_goal(qmd(), at="2030-01-16T19:00:00")
        self.assertEqual(self.candidate()["status"], "skipped")
        self.assertIn("quote", self.candidate()["reason"])
        self.assertEqual(FG.due_goals(today=dt.date(2030, 1, 16)), [])

    def test_degraded_retrieval_does_not_use_up_attempts(self):
        dc.CFG["frontier"]["max_parse_attempts"] = 1
        self.run_goal(FakeQmd(degraded={"transcripts"}))
        self.run_goal(FakeQmd(degraded={"transcripts"}), at="2030-01-16T19:00:00")
        c = self.candidate()
        self.assertEqual((c["status"], c["attempts"]), ("deferred", 0))

    def test_quote_only_in_an_assistant_passage_is_refused_for_explained(self):
        t = self.transcript("assistant-only", [("Human", "What is a test tile?"),
                                               ("Assistant", ASSISTANT_WORDS), ("Human", "thanks")])
        self.set_script(check={GAP.lower(): check_reply("explained", ASSISTANT_WORDS)})
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "firing a sample")]}))
        self.assertEqual(list(F.items_dir().glob("K*.md")), [])
        self.assertEqual(self.candidate()["status"], "deferred")

    def test_assistant_passage_shows_exposure_only(self):
        t = self.transcript("assistant-only", [("Human", "What is a test tile?"),
                                               ("Assistant", ASSISTANT_WORDS), ("Human", "thanks")])
        self.set_script(check={GAP.lower(): check_reply("none", exposed=[self.rel(t)])},
                        consequence={GAP.lower(): {"verdict": "consequential", "reason": "Decides it."}})
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "firing a sample")]}))
        it = F.Item.from_path(next(F.items_dir().glob("K*.md")))
        self.assertEqual(it.quadrant, "unknown_unknown")
        self.assertEqual([e["kind"] for e in it.evidence], ["exposure"])
        prompt = self.prompt("check")
        self.assertIn("DERIVED", prompt)
        self.assertIn(ASSISTANT_WORDS, prompt)
        owner_part = prompt.split("## Assistant passages")[0]
        self.assertNotIn(ASSISTANT_WORDS, owner_part)

    def test_concept_explained_in_other_words_reaches_the_judge_through_its_matched_line(self):
        other = "Before the big load went in, a sample cup cracked on the shelf. It broke at the rim, so the schedule changed."
        filler = "\n\n".join(f"Unrelated paragraph number {i} about the weather." for i in range(12))
        # (kept under owner_turn_max_chars: a verdict quote must sit in owner_text()'s capped view)
        t = self.transcript("other-words", [("Human", filler + "\n\n" + other + "\n\n" + filler)])
        self.set_script(check={GAP.lower(): check_reply("explained", other.split(". ")[0] + ".",
                                                        requires="a sample cup cracked on the shelf")})
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "sample cup cracked")]}))
        self.assertIn("a sample cup cracked on the shelf", self.prompt("check"))
        it = F.Item.from_path(next(F.items_dir().glob("K*.md")))
        self.assertNotEqual(it.quadrant, "unknown_unknown")
        self.assertEqual(self.candidate()["check"], "explained")

    def test_unparseable_check_defers_with_an_attempt_count(self):
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "check=malformed"
        self.run_goal(self.some_qmd())
        self.assertEqual(list(F.items_dir().glob("K*.md")), [])
        c = self.candidate()
        self.assertEqual((c["status"], c["reason"], c["attempts"]), ("deferred", "parse", 1))

    def test_unparseable_consequence_defers_and_the_next_run_redoes_only_the_judge(self):
        self.set_script(consequence={GAP.lower(): {"verdict": "consequential", "reason": "Decides it."}})
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "consequence=malformed"
        self.run_goal()
        c = self.candidate()
        self.assertEqual((c["status"], c["reason"], c["stage"]), ("deferred", "parse", "consequence"))
        self.assertEqual(len(list(F.items_dir().glob("K*.md"))), 1)
        del os.environ["DREAMER_FAKE_FRONTIER_FAIL"]
        self.clear_prompts()
        self.run_goal(at="2030-01-16T19:00:00")
        self.assertIsNone(self.prompt("premortem"), "a resume does not run a new pre-mortem")
        self.assertIsNone(self.prompt("check"), "the check was already done")
        c = self.candidate()
        self.assertEqual((c["status"], c["consequence"]), ("done", "consequential"))
        self.assertEqual(len(list(F.items_dir().glob("K*.md"))), 1)

    def test_a_candidate_is_skipped_after_max_parse_attempts(self):
        dc.CFG["frontier"]["max_parse_attempts"] = 2
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "check=malformed"
        self.run_goal(self.some_qmd())
        self.run_goal(self.some_qmd(), at="2030-01-16T19:00:00")
        self.assertEqual(self.candidate()["status"], "skipped")
        self.assertEqual(FG.due_goals(today=dt.date(2030, 1, 16)), [])

    def test_usage_limit_in_the_check_stops_the_run_and_defers_what_is_left(self):
        self.set_script(premortem={GOAL: premortem_reply([(CAUSE, [(GAP, "a"), ("Second gap", "b")])])})
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "check=exit"
        res = self.run_goal(self.some_qmd())
        self.assertEqual(res["status"], "stopped")
        rec = self.record()
        self.assertEqual([c["status"] for c in rec["candidates"]], ["deferred", "deferred"])
        self.assertEqual(list(F.items_dir().glob("K*.md")), [])

    def test_usage_limit_in_the_premortem_writes_no_record(self):
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "premortem=exit"
        res = self.run_goal()
        self.assertEqual(res["status"], "stopped")
        self.assertIsNone(FG.latest_record(GOAL))

    def test_unparseable_premortem_records_nothing(self):
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "premortem=prose"
        res = self.run_goal()
        self.assertEqual(res["status"], "failed")
        self.assertIsNone(FG.latest_record(GOAL))

    def test_no_retrieval_at_all_means_none_without_a_model_call(self):
        self.run_goal()
        self.assertIsNone(self.prompt("check"))
        self.assertEqual(self.candidate()["check"], "none")

    def test_record_lists_revision_ranked_causes_and_every_candidate(self):
        self.write_goal(GOAL, "active", revision=3)
        self.set_script(
            premortem={GOAL: premortem_reply([
                (CAUSE, [(GAP, "Test tile."), ("Studio shelf labels", "Mark each mug.")]),
                ("The kiln breaks mid-season", [("Spare element stock", "Replacement parts on hand.")])])},
            check={"studio shelf labels": check_reply("none")},
            consequence={GAP.lower(): {"verdict": "consequential", "reason": "A cracked load, no mugs."},
                         "studio shelf labels": {"verdict": "tactical", "reason": "Tunable later."},
                         "spare element stock": {"verdict": "consequential", "reason": "A dead kiln stops all firing."}})
        self.run_goal()
        rec = self.record()
        self.assertEqual(rec["plan_revision"], 3)
        self.assertEqual([c["rank"] for c in rec["causes"]], [1, 2])
        self.assertEqual([c["cause"] for c in rec["causes"]], [CAUSE, "The kiln breaks mid-season"])
        self.assertEqual([(c["title"], c["cause_rank"], c["consequence"]) for c in rec["candidates"]],
                         [(GAP, 1, "consequential"), ("Studio shelf labels", 1, "tactical"),
                          ("Spare element stock", 2, "consequential")])
        for c in rec["candidates"]:
            self.assertTrue(c["item"].startswith("K"))
            self.assertTrue(c["consequence_reason"])
        text = Path(rec["_path"]).read_text(encoding="utf-8")
        self.assertIn("Spare element stock", text)
        self.assertIn(f"vault/frontier/premortems/{GOAL}/", str(rec["_path"]).replace(str(self.tmp), "vault/..")
                      .replace("vault/../vault", "vault"))

    def test_record_path_is_per_goal_and_dated(self):
        self.run_goal()
        self.assertTrue((self.dirs["frontier"] / "premortems" / GOAL / "2030-01-15.md").exists())

    def test_verdict_cache_holds_until_the_plan_revision_changes(self):
        self.set_script(consequence={GAP.lower(): {"verdict": "consequential", "reason": "First verdict."}})
        self.run_goal()
        self.assertIsNotNone(self.prompt("consequence"))
        # A week later the same gap comes up again and matches the item the first run made.
        item_id = self.candidate()["item"]
        self.set_script(
            check={GAP.lower(): check_reply("none", item=item_id)},
            consequence={GAP.lower(): {"verdict": "tactical", "reason": "Would differ if re-judged."}})
        self.clear_prompts()
        qmd = FakeQmd({"frontier": [hit(f"items/{item_id}.md")]})
        self.run_goal(qmd, at="2030-01-25T19:00:00")
        self.assertIsNone(self.prompt("consequence"), "same plan revision: no new judgement")
        c = self.candidate()
        self.assertEqual((c["consequence"], c["consequence_reason"], c["cached"]),
                         ("consequential", "First verdict.", True))
        self.assertEqual(len(list(F.items_dir().glob("K*.md"))), 1, "a matched candidate adds no second item")
        # The plan changes: the verdict is re-judged.
        self.write_goal(GOAL, "active", revision=2)
        self.clear_prompts()
        self.run_goal(qmd, at="2030-01-26T19:00:00")
        self.assertIsNotNone(self.prompt("consequence"))
        self.assertEqual(self.candidate()["consequence"], "tactical")

    def test_a_repeated_title_reuses_its_item_even_when_the_index_is_stale(self):
        self.set_script(consequence={GAP.lower(): {"verdict": "consequential", "reason": "Decides it."}})
        self.run_goal()
        self.run_goal(at="2030-01-25T19:00:00")  # retrieval returns nothing: the index has not caught up
        self.assertEqual(len(list(F.items_dir().glob("K*.md"))), 1)

    def test_paused_goal_produces_no_candidates(self):
        self.write_goal(GOAL, "paused")
        res = FG.run(qmd_factory=lambda: FakeQmd(), at=AT)
        self.assertEqual(res["goals"], [])
        self.assertIsNone(FG.latest_record(GOAL))
        self.assertIsNone(self.prompt("premortem"))
        self.assertEqual(list(F.items_dir().glob("K*.md")), [])

    def test_run_goal_refuses_a_goal_that_is_no_longer_active(self):
        self.write_goal(GOAL, "paused")
        res = self.run_goal()
        self.assertEqual(res["status"], "skipped")
        self.assertIsNone(self.prompt("premortem"))

    def test_apply_waits_for_the_vault_lock_and_defers_when_it_stays_busy(self):
        import fcntl
        self.dirs["meta"].mkdir(parents=True, exist_ok=True)
        fh = open(self.dirs["meta"] / "wiki.lock", "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        self.addCleanup(fh.close)
        self.run_goal()
        self.assertEqual(list(F.items_dir().glob("K*.md")), [])
        c = self.candidate()
        self.assertEqual(c["status"], "deferred")
        self.assertIn("lock", c["reason"])


class CheckInputTest(GapsCase):
    def test_check_asks_every_configured_collection_for_top_k(self):
        dc.CFG["frontier"]["check_top_k"] = 4
        qmd = FakeQmd()
        FG.check_candidate(GAP, "Test tile.", qmd)
        hybrid = [c for c in qmd.calls if not c[3]]
        self.assertEqual({c[0] for c in hybrid}, {"frontier", "transcripts", "vault", "conclusions"})
        self.assertTrue(all(c[2] == 4 for c in hybrid))

    def test_check_also_searches_every_collection_for_the_title_words_alone(self):
        # qmd ANDs every lex term, so the hybrid query's lex leg (title + summary)
        # almost never matches; the short title, searched on its own, does.
        qmd = FakeQmd()
        FG.check_candidate(GAP, "Test tile.", qmd)
        titled = [c for c in qmd.calls if c[3]]
        self.assertEqual({c[0] for c in titled}, {"frontier", "transcripts", "vault", "conclusions"})
        self.assertTrue(all(c[1] == GAP and c[2] == 3 for c in titled))

    def test_a_chat_only_the_title_search_finds_reaches_the_judge(self):
        t = self.transcript("title-only", [("Human", OWNER_WORDS)])
        FG.check_candidate(GAP, "Test tile.", FakeQmd(title_hits={"transcripts": [self.hit_for(t, "test tile")]}))
        self.assertIn(OWNER_WORDS, self.prompt("check"))

    def test_a_chat_both_searches_find_is_shown_once(self):
        t = self.transcript("both", [("Human", OWNER_WORDS)])
        h = self.hit_for(t, "test tile")
        FG.check_candidate(GAP, "Test tile.", FakeQmd({"transcripts": [h]}, title_hits={"transcripts": [h]}))
        self.assertEqual(self.prompt("check").count(OWNER_WORDS), 1)

    def test_title_hits_rank_after_the_meaning_search_for_the_prompt_cap(self):
        a = self.transcript("meaning", [("Human", "Meaning hit " + "x" * 400)], date="2029-12-21")
        b = self.transcript("title", [("Human", "Title hit " + "y" * 400)], date="2029-12-22")
        qmd = FakeQmd({"transcripts": [self.hit_for(a, "Meaning hit", 0.1)]},
                      title_hits={"transcripts": [self.hit_for(b, "Title hit", 0.99)]})
        dc.CFG["frontier"]["prompt_max_chars"] = len(L.render_prompt("check", "x")) + 900
        FG.check_candidate(GAP, "s", qmd)
        sent = self.prompt("check")
        self.assertIn("Meaning hit", sent)
        self.assertNotIn("Title hit", sent)

    def test_a_degraded_title_search_is_inconclusive(self):
        res = FG.check_candidate(GAP, "Test tile.", FakeQmd(degraded_lex={"transcripts"}))
        self.assertEqual(res["status"], "inconclusive")
        self.assertIn("transcripts", res["reason"])

    def test_prompt_cap_drops_the_lowest_ranked_hit_first(self):
        a = self.transcript("best", [("Human", "Best hit " + "x" * 400)], date="2029-12-21")
        b = self.transcript("worst", [("Human", "Worst hit " + "y" * 400)], date="2029-12-22")
        qmd = FakeQmd({"transcripts": [self.hit_for(a, "Best hit", 0.9), self.hit_for(b, "Worst hit", 0.2)]})
        prompt_len = len(L.render_prompt("check", "x"))
        dc.CFG["frontier"]["prompt_max_chars"] = prompt_len + 900
        FG.check_candidate(GAP, "s", qmd)
        sent = self.prompt("check")
        self.assertIn("Best hit", sent)
        self.assertNotIn("Worst hit", sent)

    def test_a_loop_hit_from_the_vault_collection_is_read_as_derived(self):
        # qmd's `vault` collection is vault/loops only (.qmd/index.yml), so its hits
        # are "vault/L0001.md", not "vault/loops/L0001.md".
        (self.dirs["loops"] / "L0001.md").write_text(
            "---\ntype: loop\nid: L0001\n---\n\n# Loop\n\nHow to fire a test tile before loading.\n",
            encoding="utf-8")
        qmd = FakeQmd({"vault": [hit("vault/L0001.md", 7)]})
        FG.check_candidate(GAP, "Test tile.", qmd)
        sent = self.prompt("check")
        self.assertIsNotNone(sent, "the loop passage must reach the check")
        self.assertIn("How to fire a test tile before loading.", sent)
        # A Dreamer page is derived but is no chat: it sits in its own section, so the
        # judge never offers its label as a transcript in exposed_in
        # (an earlier version offered the page label as a transcript in exposed_in).
        pages = sent.split("## Dreamer pages")[1].split("## Map items")[0]
        self.assertIn("=== PAGE vault:vault/L0001.md line 8 ===\nHow to fire a test tile", pages)
        chats = sent.split("## Assistant passages")[1].split("## Dreamer pages")[0]
        self.assertNotIn("L0001", chats)

    def test_hits_that_resolve_nowhere_defer_instead_of_reading_as_never_discussed(self):
        # An index out of step with the disk is a retrieval failure: answering
        # "never discussed" would pass every candidate as new.
        qmd = FakeQmd({"transcripts": [hit("transcripts/2029/12/2029-12-21-gone.md", 3)],
                       "vault": [hit("vault/L0999.md", 1)]})
        res = FG.check_candidate(GAP, "Test tile.", qmd)
        self.assertEqual(res["status"], "inconclusive")
        self.assertFalse(res["counted"], "a stale index is not the model's fault")
        self.assertIn("not on disk", res["reason"])
        self.assertIsNone(self.prompt("check"))

    def test_one_missing_hit_among_readable_ones_still_checks(self):
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        qmd = FakeQmd({"transcripts": [self.hit_for(t, "test tile"),
                                       hit("transcripts/2029/12/2029-12-21-gone.md", 3)]})
        res = FG.check_candidate(GAP, "Test tile.", qmd)
        self.assertEqual(res["status"], "ok")
        self.assertIn(OWNER_WORDS, self.prompt("check"))

    # qmd's best line usually falls in a long assistant reply; the owner's own words
    # sit in other turns of the same chat.
    def test_the_owner_question_an_assistant_hit_answers_reaches_the_check(self):
        question = "Would a quick sample firing show whether this glaze holds?"
        t = self.transcript("test-tile", [("Human", question), ("Assistant", ASSISTANT_WORDS)])
        self.set_script(check={GAP.lower(): check_reply("asked", question, requires=question)})
        res = FG.check_candidate(GAP, "Test tile.", FakeQmd({"transcripts": [self.hit_for(t, "test tile")]}))
        self.assertEqual((res["status"], res["owner_discussed"], res["transcript"]),
                         ("ok", "asked", self.rel(t)))
        self.assertIn(f"=== OWNER {self.rel(t)}", self.prompt("check"))

    def test_an_owner_paragraph_elsewhere_in_the_chat_reaches_the_check(self):
        words = "Before filling the kiln, a sample tile goes in first, then the glaze recipe gets adjusted."
        t = self.transcript("test-tile", [("Human", "Notes about the herb planter."), ("Assistant", ASSISTANT_WORDS),
                                          ("Human", "What about tomatoes?"), ("Assistant", "Tomatoes like sun."),
                                          ("Human", words)])
        self.set_script(check={GAP.lower(): check_reply("explained", words, requires=words)})
        res = FG.check_candidate(GAP, "Test tile.", FakeQmd({"transcripts": [self.hit_for(t, "test tile")]}))
        self.assertEqual((res["status"], res["owner_discussed"]), ("ok", "explained"))
        self.assertNotIn("What about tomatoes?", self.prompt("check"), "unmatched owner turns stay out")

    def test_assistant_only_exposure_still_is_not_owner_knowledge(self):
        t = self.transcript("test-tile", [("Human", "Can you explain something?"), ("Assistant", ASSISTANT_WORDS),
                                          ("Human", "Thanks, that helps.")])
        self.set_script(check={GAP.lower(): check_reply("explained", ASSISTANT_WORDS)})
        res = FG.check_candidate(GAP, "Test tile.", FakeQmd({"transcripts": [self.hit_for(t, "test tile")]}))
        self.assertEqual(res["status"], "inconclusive")
        self.assertIn("not found in an owner turn", res["reason"])
        self.assertNotIn("Thanks, that helps.", self.prompt("check"))

    def test_owner_passages_are_added_only_for_chats(self):
        (self.dirs["loops"] / "L0001.md").write_text(
            "---\ntype: loop\nid: L0001\n---\n\n# Loop\n\n## Human\n\nFire a test tile before loading.\n",
            encoding="utf-8")
        FG.check_candidate(GAP, "Test tile.", FakeQmd({"vault": [hit("vault/L0001.md", 7)]}))
        self.assertNotIn("=== OWNER", self.prompt("check"), "a loop page is derived even with a Human heading")

    def test_consequence_prompt_has_goal_cause_and_candidate_but_no_transcripts(self):
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        self.run_goal(FakeQmd({"transcripts": [self.hit_for(t, "test tile")]}))
        prompt = self.prompt("consequence")
        self.assertTrue(prompt.startswith("# Frontier consequence\n"))
        for needle in ("Four mugs are fired and glazed.", CAUSE, GAP, "Pick a clay body"):
            self.assertIn(needle, prompt)
        self.assertNotIn(OWNER_WORDS, prompt)

    # The owner's phrases: a title joining two ideas ANDs both halves in the keyword
    # search, so a chat where the owner said one half in passing was never shown and the
    # candidate passed as never discussed.
    def test_a_chat_where_the_owner_said_part_of_the_title_reaches_the_judge(self):
        words = "Better to ramp slowly than later on, after the load is in and you have this thermal shock."
        t = self.transcript("roleplay", [("Human", words), ("Assistant", "Noted.")])
        self.set_script(check={SUNK.lower(): check_reply("explained", words, requires=words)})
        res = FG.check_candidate(SUNK, "Pieces crack when the temperature moves too fast.", FakeQmd())
        self.assertEqual((res["status"], res["owner_discussed"], res["transcript"]),
                         ("ok", "explained", self.rel(t)))

    def test_owner_phrases_match_inflections_but_not_inside_other_words(self):
        self.transcript("plural", [("Human", "Those thermal shocks ruin the shelf.")], date="2029-12-21")
        self.transcript("inside", [("Human", "The nonthermal shock of it all.")], date="2029-12-22")
        FG.check_candidate(SUNK, "s", FakeQmd())
        sent = self.prompt("check")
        self.assertIsNotNone(sent)
        self.assertIn("Those thermal shocks ruin the shelf.", sent)
        self.assertNotIn("nonthermal", sent)

    def test_owner_phrases_ignore_pastes_assistant_turns_and_the_cut_middle_of_a_long_turn(self):
        # Matched in the owner's words as quote verification reads them (find_owner_quote),
        # so a quote the judge lifts from a phrase chat always verifies.
        self.transcript("fenced", [("Human", "Look at this:\n\n```\nwe tracked the thermal shock\n```")])
        self.transcript("quoted", [("Human", "> the thermal shock was huge")])
        self.transcript("assistant", [("Human", "Tell me more."), ("Assistant", "The thermal shock curve is...")])
        filler = "filler " * 1000
        self.transcript("long", [("Human", f"Start of the notes. {filler}the thermal shock in the middle {filler}End.")])
        res = FG.check_candidate(SUNK, "s", FakeQmd())
        self.assertEqual(res["owner_discussed"], "none")
        self.assertIsNone(self.prompt("check"), "no owner phrase matched, so no chat was shown")

    def test_phrase_chats_are_capped_most_phrases_first_then_newest(self):
        dc.CFG["frontier"]["check_phrase_max_chats"] = 2
        self.transcript("old", [("Human", "Our thermal shock back then.")], date="2029-03-23")
        self.transcript("new", [("Human", "Our thermal shock lately.")], date="2029-12-21")
        self.transcript("both", [("Human", "Thermal shock and the glaze crawling, both.")], date="2025-01-01")
        FG.check_candidate(SUNK, "s", FakeQmd())
        sent = self.prompt("check")
        self.assertIn("glaze crawling, both", sent)
        self.assertIn("Our thermal shock lately.", sent)
        self.assertNotIn("Our thermal shock back then.", sent)

    def test_a_phrase_chat_a_search_already_found_is_shown_once(self):
        words = "After the load is in you have this thermal shock."
        t = self.transcript("found", [("Human", words)])
        FG.check_candidate(SUNK, "s", FakeQmd({"transcripts": [self.hit_for(t, "thermal shock")]}))
        self.assertEqual(self.prompt("check").count(words), 1)

    def test_phrase_chats_rank_after_both_searches_for_the_prompt_cap(self):
        a = self.transcript("meaning", [("Human", "Meaning hit " + "x" * 400)], date="2029-12-21")
        self.transcript("phrase", [("Human", "Our thermal shock " + "y" * 400)], date="2029-12-22")
        qmd = FakeQmd({"transcripts": [self.hit_for(a, "Meaning hit", 0.1)]})
        FG.check_candidate(SUNK, "s", qmd)
        self.assertIn("Our thermal shock", self.prompt("check"), "with room, the phrase chat is shown")
        self.clear_prompts()
        dc.CFG["frontier"]["prompt_max_chars"] = len(L.render_prompt("check", "x")) + 900
        FG.check_candidate(SUNK, "s", qmd)
        sent = self.prompt("check")
        self.assertIn("Meaning hit", sent)
        self.assertNotIn("Our thermal shock", sent)


class TitlePhrasesTest(unittest.TestCase):
    def test_the_title_splits_at_its_joints_into_parts_of_two_content_words(self):
        for title, want in [
            ("Clay body versus slip casting", ["clay body", "slip casting"]),
            ("Thermal shock and glaze crawling", ["thermal shock", "glaze crawling"]),
            ("Cone-6 firing risks, glaze defects and shelf space", ["cone 6 firing risks", "glaze defects", "shelf space"]),
            ("The cost of a late firing", ["cost of a late firing"]),
            ("Cooling-phase strategy", ["cooling phase strategy"]),
            ("Access", []),
        ]:
            self.assertEqual(FG.title_phrases(title), want, title)


class RecheckTest(GapsCase):
    """`recheck`: the never-discussed check again for items a pre-mortem already made, once
    the check itself has changed."""

    def unknown_item(self) -> F.Item:
        self.set_script(consequence={GAP.lower(): {"verdict": "consequential", "reason": "No test tile."}})
        self.run_goal()
        return F.Item.from_path(next(F.items_dir().glob("K*.md")))

    def recheck(self, ids, qmd=None):
        return FG.recheck(ids, qmd_factory=lambda: qmd or FakeQmd(), at=AT)

    def test_owner_discussion_found_now_moves_the_same_item_with_its_cause(self):
        it = self.unknown_item()
        t = self.transcript("test-tile", [("Human", OWNER_WORDS)])
        other = self.item("Bisque firing", "known_known")
        # The judge naming another item does not merge them: the evidence lands on this one.
        self.set_script(check={GAP.lower(): check_reply("explained", OWNER_WORDS, other.id, requires=OWNER_WORDS)})
        out = self.recheck([it.id], FakeQmd({"transcripts": [self.hit_for(t, "test tile")],
                                             "frontier": [hit(f"items/{other.id}.md")]}))
        self.assertEqual(out, [{"item": it.id, "goal": GOAL, "status": "moved", "check": "explained",
                                "from": "unknown_unknown", "to": "unknown_known", "matched": other.id}])
        it = F.Item.from_path(F.item_path(it.id))
        # Owner-known but never tied to the goal: still a survivor, now taught as a connection.
        self.assertEqual(it.quadrant, "unknown_known")
        self.assertEqual(it.moves[-1]["cause"], f"recheck {GOAL} {AT[:10]}")
        self.assertTrue(any(e["kind"] == "explained" and e["quote"] == OWNER_WORDS
                            and e["transcript"] == self.rel(t) for e in it.evidence))
        self.assertEqual(F.Item.from_path(F.item_path(other.id)).evidence, [])
        c = self.candidate()
        self.assertEqual((c["check"], c["quote"], c["transcript"], c["quadrant"]),
                         ("explained", OWNER_WORDS, self.rel(t), "unknown_known"))

    def test_an_owner_question_takes_the_item_out_of_the_survivors(self):
        it = self.unknown_item()
        q = "Would a quick sample firing show whether this glaze holds?"
        t = self.transcript("test-tile", [("Human", q)])
        self.set_script(check={GAP.lower(): check_reply("asked", q, requires=q)})
        out = self.recheck([it.id], FakeQmd({"transcripts": [self.hit_for(t, "sample firing")]}))
        self.assertEqual((out[0]["status"], out[0]["to"]), ("moved", "known_unknown"))
        self.assertNotIn(F.Item.from_path(F.item_path(it.id)).quadrant, FG.SURVIVORS)

    def test_nothing_found_leaves_the_item_and_the_record_as_they_were(self):
        it = self.unknown_item()
        before_item = F.item_path(it.id).read_text(encoding="utf-8")
        before_rec = Path(self.record()["_path"]).read_text(encoding="utf-8")
        out = self.recheck([it.id], self.some_qmd())
        self.assertEqual((out[0]["status"], out[0]["check"]), ("unchanged", "none"))
        self.assertEqual(F.item_path(it.id).read_text(encoding="utf-8"), before_item)
        self.assertEqual(Path(self.record()["_path"]).read_text(encoding="utf-8"), before_rec)

    def test_an_item_no_pre_mortem_made_is_skipped(self):
        other = self.item("Bisque firing", "unknown_unknown")
        out = self.recheck([other.id])
        self.assertEqual((out[0]["item"], out[0]["status"]), (other.id, "skipped"))

    def test_an_inconclusive_check_defers_without_a_write(self):
        it = self.unknown_item()
        before_item = F.item_path(it.id).read_text(encoding="utf-8")
        out = self.recheck([it.id], FakeQmd(degraded={"transcripts"}))
        self.assertEqual(out[0]["status"], "deferred")
        self.assertIn("degraded", out[0]["reason"])
        self.assertEqual(F.item_path(it.id).read_text(encoding="utf-8"), before_item)

    def test_a_usage_limit_stops_the_recheck_and_reports_every_item_left(self):
        it = self.unknown_item()
        other = self.item("Bisque firing", "unknown_unknown")
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "check=exit"
        out = self.recheck([it.id, other.id], self.some_qmd())
        self.assertEqual([(o["item"], o["status"]) for o in out], [(it.id, "stopped"), (other.id, "stopped")])
        self.assertEqual(F.Item.from_path(F.item_path(it.id)).quadrant, "unknown_unknown")

    def test_the_command_refuses_while_a_frontier_run_holds_the_lock(self):
        import fcntl
        import io
        from contextlib import redirect_stdout
        it = self.unknown_item()
        with open(self.dirs["meta"] / dc.CFG["frontier"]["lock_file"], "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.clear_prompts()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = FG.main(["recheck", it.id])
        self.assertEqual(rc, 1)
        self.assertIn("frontier.lock", buf.getvalue())
        self.assertIsNone(self.prompt("check"), "no check ran")


class CliTest(GapsCase):
    def test_due_command_lists_due_goals(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = FG.main(["due"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [GOAL])


if __name__ == "__main__":
    unittest.main()
