#!/usr/bin/env python3
"""Tests for the frontier map builder. Fixtures are synthetic. The model is tests/fake_claude.py, retrieval is a stub
QmdClient, and every path is a temp dir: nothing here touches the real vault."""

from __future__ import annotations

import fcntl
import json
import os
import re
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
import frontier_map as FM  # noqa: E402

AT = "2030-01-15T19:00:00"
PASTE_QUOTE = "def fire(tile): return 50"


def page(date: str, turns: list[tuple[str, str]], count: int | None = None) -> str:
    n = len(turns) if count is None else count
    body = "".join(f"## {role}\n\n{text}\n\n" for role, text in turns)
    return (f"---\ntype: transcript\ndate: {date}\nmessage_count: {n}\n---\n\n# t\n\n{body}")


def concept(title, quote, kind="explained", summary="A concept.", goals=None):
    return {"title": title, "summary": summary, "kind": kind, "quote": quote,
            "goals": goals or []}


class FakeQmd:
    """Token-overlap retrieval over the temp frontier items, shaped like QmdClient."""

    def __init__(self, items_dir: Path, degraded: bool = False):
        self.items_dir, self.degraded = items_dir, degraded
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def query(self, text, collection, limit=None):
        self.queries.append(text)
        if self.degraded:
            return [], True
        words = set(re.findall(r"[a-z]+", text.lower()))
        hits = []
        for path in sorted(self.items_dir.glob("K*.md")):
            it = F.Item.from_path(path)
            overlap = len(words & set(re.findall(r"[a-z]+", it.title.lower())))
            if overlap:
                hits.append({"docid": f"#{it.id}", "file": f"items/{path.name}", "line": 1,
                             "title": it.title, "score": float(overlap), "snippet": ""})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit], False


class MapCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-map-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.dirs = {"vault": self.vault, "sources": self.vault / "sources" / "transcripts",
                     "meta": self.vault / ".vault-meta", "loops": self.vault / "loops",
                     "goals": self.vault / "goals", "frontier": self.vault / "frontier",
                     "logs": self.tmp / "logs"}
        for d in self.dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        (self.dirs["frontier"] / "items").mkdir(exist_ok=True)
        self._paths = dict(dc.CFG["paths"])
        self._frontier = dict(dc.CFG["frontier"])
        dc.CFG["paths"].update({k: str(v) for k, v in self.dirs.items()})
        dc.CFG["frontier"].update({
            "vault_lock_wait_seconds": 1, "map_batch_chars": 100_000, "match_top_k": 5,
            "max_parse_attempts": 3, "max_items_per_transcript": 8})
        self.addCleanup(self.restore)
        self.script = self.tmp / "script.json"
        self.set_script({}, {})
        env = {"DREAMER_FAKE_CLAUDE": str(ROOT / "tests" / "fake_claude.py"),
               "DREAMER_FAKE_FRONTIER_SCRIPT": str(self.script)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("DREAMER_FAKE_FRONTIER_FAIL", "DREAMER_LOCK_HELD"):
            os.environ.pop(k, None)
        self.write_goal("G0003", "active")

    def restore(self):
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._paths)
        dc.CFG["frontier"].clear()
        dc.CFG["frontier"].update(self._frontier)

    # helpers ---------------------------------------------------------------
    def set_script(self, extract: dict, match: dict) -> None:
        self.script.write_text(json.dumps({"extract": extract, "match": match}))

    def write_goal(self, gid, status):
        (self.dirs["goals"] / f"{gid}.md").write_text(
            f"---\ntype: goal\nid: {gid}\nstatus: {status}\ntitle: Fire a set of mugs\n"
            f"finish_line: Four mugs are fired and glazed.\n---\n\n# g\n", encoding="utf-8")

    def transcript(self, slug, turns, date="2029-12-21", count=None) -> str:
        y, m = date[:4], date[5:7]
        path = self.dirs["sources"] / y / m / f"{date}--{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page(date, turns, count), encoding="utf-8")
        return f"sources/transcripts/{y}/{m}/{date}--{slug}"

    def qmd(self, **kw):
        return lambda: FakeQmd(self.dirs["frontier"] / "items", **kw)

    def run_batch(self, **kw):
        kw.setdefault("qmd_factory", self.qmd())
        return FM.run_batch(at=AT, **kw)

    def items(self) -> list[F.Item]:
        return [F.Item.from_path(p) for p in sorted((self.dirs["frontier"] / "items").glob("K*.md"))]

    def ledger(self) -> dict:
        return json.loads((self.dirs["meta"] / "frontier-map-ledger.json").read_text())

    def done(self) -> dict:
        path = self.dirs["meta"] / "frontier-map-ledger.json"
        return json.loads(path.read_text()).get("done", {}) if path.exists() else {}

    def events(self) -> list[dict]:
        path = self.dirs["meta"] / "run-state.json"
        return json.loads(path.read_text()).get("events", []) if path.exists() else []


class MapBuildTest(MapCase):
    def test_batch_creates_items_with_verified_quotes(self):
        q = "Decision: mix the first glaze with fifty grams of frit per bucket."
        rel = self.transcript("batching", [("Human", q), ("Assistant", "Sounds good.")])
        self.set_script({"2029-12-21--batching": [concept("Glaze batching", q, "decided")]}, {})
        res = self.run_batch()
        self.assertEqual(res["status"], "done")
        (item,) = self.items()
        self.assertEqual(item.title, "Glaze batching")
        self.assertEqual(item.origin, "map")
        self.assertEqual(item.quadrant, "known_known")
        self.assertEqual(item.evidence[0]["quote"], q)
        self.assertEqual(item.evidence[0]["transcript"], rel)
        self.assertEqual(item.evidence[0]["date"], "2029-12-21")
        self.assertEqual(item.moves[-1]["cause"], f"chat {rel}")
        self.assertIn(rel, self.done())
        self.assertEqual(self.run_batch()["status"], "drained")

    def test_map_item_records_one_move_not_a_fake_unknown_unknown_first(self):
        q = "Decision: batch the glaze by weight."
        self.transcript("a", [("Human", q)])
        self.set_script({"2029-12-21--a": [concept("Glaze batching", q, "decided")]}, {})
        self.run_batch()
        (item,) = self.items()
        self.assertEqual([(m["from"], m["to"]) for m in item.moves], [("", "known_known")])

    def test_quote_not_in_a_human_turn_is_dropped_assistant_only(self):
        self.transcript("assist", [
            ("Human", "Tell me about kiln cones."),
            ("Assistant", "A pyrometric cone is a slim pyramid that bends when the kiln reaches temperature."),
            ("Human", "thanks")])
        self.set_script({"2029-12-21--assist": [
            concept("Pyrometric cone", "A pyrometric cone is a slim pyramid that bends when the kiln reaches temperature.")]}, {})
        res = self.run_batch()
        self.assertEqual(self.items(), [])
        self.assertEqual(res["dropped_quotes"], 1)
        self.assertIn("sources/transcripts/2029/12/2029-12-21--assist", self.done())

    def test_pasted_text_inside_a_human_turn_yields_no_known_known(self):
        self.transcript("paste", [(
            "Human", f"Look at this:\n```\n{PASTE_QUOTE}\n```\n> {PASTE_QUOTE} quoted\nthoughts?")])
        self.set_script({"2029-12-21--paste": [
            concept("Tile firing function", PASTE_QUOTE, "explained"),
            concept("Quoted firing", f"{PASTE_QUOTE} quoted", "explained")]}, {})
        self.run_batch()
        self.assertEqual(self.items(), [])

    def test_reconstructed_quote_never_places_a_known_known(self):
        self.transcript("recon", [("Human (reconstructed)", "The owner explained pyrometric cones well.")])
        self.set_script({"2029-12-21--recon": [
            concept("Pyrometric cones", "The owner explained pyrometric cones well.", "explained")]}, {})
        self.run_batch()
        (item,) = self.items()
        self.assertEqual(item.evidence[0]["kind"], "reconstructed")
        self.assertEqual(item.quadrant, "known_unknown")

    def test_goal_relation_unconnected_makes_unknown_known_and_inactive_goal_is_ignored(self):
        q = "Wrote up how bisque cones bend for the studio newsletter."
        self.transcript("cones", [("Human", q)])
        self.set_script({"2029-12-21--cones": [concept(
            "Cone bending", q, goals=[{"goal": "G0003", "relation": "unconnected"}])]}, {})
        self.run_batch()
        (item,) = self.items()
        self.assertEqual(item.quadrant, "unknown_known")
        self.assertEqual(item.goals, [{"goal": "G0003", "relation": "unconnected"}])

    def test_max_items_per_transcript(self):
        dc.CFG["frontier"]["max_items_per_transcript"] = 2
        text = " ".join(f"Noted topic{i} in detail." for i in range(5))
        self.transcript("many", [("Human", text)])
        self.set_script({"2029-12-21--many": [
            concept(f"Topic number {chr(97 + i)}", f"Noted topic{i} in detail.")
            for i in range(5)]}, {})
        self.run_batch()
        self.assertEqual(len(self.items()), 2)

    def test_markers_in_owner_text_do_not_change_what_the_fake_answers(self):
        q = "# Frontier match and # Thread fold are headings I typed."
        self.transcript("marker", [("Human", q)])
        self.set_script({"2029-12-21--marker": [concept("Heading markers", q)]}, {})
        self.run_batch()
        self.assertEqual(len(self.items()), 1)


class MatchingTest(MapCase):
    def test_second_mention_attaches_as_evidence_and_creates_no_duplicate(self):
        q1, q2 = "Showed the class how wedging removes air bubbles.", "Again went through wedging with the class."
        self.transcript("one", [("Human", q1)], date="2029-12-21")
        self.set_script({"2029-12-21--one": [concept("Wedging", q1)]}, {})
        self.run_batch()
        self.transcript("two", [("Human", q2)], date="2029-12-22")
        self.set_script({"2029-12-22--two": [concept("Wedging", q2)]}, {})
        res = self.run_batch()
        (item,) = self.items()
        self.assertEqual(len(item.evidence), 2)
        self.assertEqual(res["items_attached"], 1)
        self.assertEqual(res["items_created"], 0)

    def test_every_match_decision_is_recorded_with_its_justification(self):
        # constitution rule 7: each Stage-B decision is kept with a one-line justification
        q1, q2 = "Showed the class how wedging removes air bubbles.", "Again went through wedging with the class."
        self.transcript("one", [("Human", q1)], date="2029-12-21")
        self.set_script({"2029-12-21--one": [concept("Wedging", q1)]}, {})
        self.run_batch()
        q3 = "The extruder sits unused in the corner."
        self.transcript("two", [("Human", q2), ("Human", q3)], date="2029-12-22")
        self.set_script({"2029-12-22--two": [concept("Wedging", q2),
                                             concept("Idle equipment", q3)]}, {})
        self.run_batch()
        done = FM.load_ledger()["done"]
        (item,) = [i for i in self.items() if i.title == "Wedging"]
        (new,) = [i for i in self.items() if i.title == "Idle equipment"]
        self.assertEqual(done["sources/transcripts/2029/12/2029-12-22--two"]["decisions"], [
            {"concept": "Wedging", "decision": "attach", "item": item.id,
             "justification": "same title"},
            {"concept": "Idle equipment", "decision": "new", "item": new.id,
             "justification": "scripted: no clear match"}])

    def test_uncertain_match_creates_a_new_item(self):
        q1, q2 = "Showed the class how wedging removes air bubbles.", "Decided how to rank the glaze options."
        self.transcript("one", [("Human", q1)], date="2029-12-21")
        self.set_script({"2029-12-21--one": [concept("Wedging", q1)]}, {})
        self.run_batch()
        self.transcript("two", [("Human", q2)], date="2029-12-22")
        # shares a word with the neighbour, but the judge says new
        self.set_script({"2029-12-22--two": [concept("Glaze ranking", q2)]}, {})
        self.run_batch()
        self.assertEqual(len(self.items()), 2)

    def test_same_batch_batch_ref_attaches_to_the_earlier_concept(self):
        q1, q2 = "Showed the class how wedging removes air bubbles.", "Wedging came up again for the class."
        self.transcript("one", [("Human", q1)], date="2029-12-21")
        self.transcript("two", [("Human", q2)], date="2029-12-22")
        self.set_script({"2029-12-21--one": [concept("Wedging", q1)],
                         "2029-12-22--two": [concept("Wedging", q2)]}, {})
        self.run_batch()
        (item,) = self.items()
        self.assertEqual([e["quote"] for e in item.evidence], [q1, q2])

    def test_batch_ref_to_a_dropped_concept_creates_a_new_item(self):
        # the earlier concept's quote is assistant-only, so it never exists; its twin stands alone
        self.transcript("one", [("Human", "hi"), ("Assistant", "Wedging is X.")],
                        date="2029-12-21")
        q2 = "Wedging mattered to the class today."
        self.transcript("two", [("Human", q2)], date="2029-12-22")
        self.set_script({"2029-12-21--one": [concept("Wedging", "Wedging is X.")],
                         "2029-12-22--two": [concept("Wedging", q2)]}, {})
        self.run_batch()
        (item,) = self.items()
        self.assertEqual(len(item.evidence), 1)

    def test_attach_to_an_item_that_vanished_creates_a_new_one(self):
        q = "Decided to book a kiln technician."
        self.transcript("a", [("Human", q)])
        self.set_script({"2029-12-21--a": [concept("Technician booking", q)]},
                        {"technician booking": {"attach": "K7999"}})
        res = self.run_batch()
        self.assertEqual(res["items_created"], 1)

    def test_empty_frontier_needs_no_retrieval(self):
        q = "Explained slow cooling."
        self.transcript("a", [("Human", q)])
        self.set_script({"2029-12-21--a": [concept("Slow cooling", q)]}, {})

        def boom():
            raise AssertionError("qmd must not be consulted when no item exists")
        self.assertEqual(self.run_batch(qmd_factory=boom)["status"], "done")

    def test_degraded_retrieval_defers_and_never_creates(self):
        q1 = "Explained slow cooling."
        self.transcript("a", [("Human", q1)], date="2029-12-21")
        self.set_script({"2029-12-21--a": [concept("Slow cooling", q1)]}, {})
        self.run_batch()
        q2 = "Slow cooling again for the class."
        self.transcript("b", [("Human", q2)], date="2029-12-22")
        self.set_script({"2029-12-22--b": [concept("Slow cooling", q2)]}, {})
        res = self.run_batch(qmd_factory=self.qmd(degraded=True))
        self.assertEqual(res["status"], "deferred")
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(len(self.items()[0].evidence), 1)
        self.assertNotIn("sources/transcripts/2029/12/2029-12-22--b", self.done())
        self.assertEqual(self.run_batch()["status"], "done")  # healthy retrieval resumes
        self.assertEqual(len(self.items()[0].evidence), 2)


class GrowthAndResumeTest(MapCase):
    def prompt_text(self) -> str:
        return (self.dirs["logs"] / ".prompt-frontier-extract.md").read_text()

    def test_grown_chat_maps_only_its_new_turns(self):
        q1 = "Explained the cone chart to the intern."
        rel = self.transcript("grow", [("Human", q1), ("Assistant", "ok")])
        self.set_script({"2029-12-21--grow": [concept("Cone chart", q1)]}, {})
        self.run_batch()
        self.assertEqual(self.done()[rel]["turns"], 2)
        q2 = "Later decided on a four hour soak schedule."
        self.transcript("grow", [("Human", q1), ("Assistant", "ok"), ("Human", q2),
                                 ("Assistant", "fine")])
        self.set_script({"2029-12-21--grow": [concept("Soak schedule", q2, "decided")]}, {})
        res = self.run_batch()
        self.assertEqual(res["status"], "done")
        text = self.prompt_text()
        self.assertIn(q2, text)
        self.assertNotIn(q1, text)
        self.assertEqual(self.done()[rel]["turns"], 4)
        self.assertEqual({i.title for i in self.items()}, {"Cone chart", "Soak schedule"})
        self.assertEqual(self.run_batch()["status"], "drained")

    def test_unchanged_chat_is_not_selected_again(self):
        self.transcript("same", [("Human", "Explained glaze thickness.")])
        self.run_batch()
        sel = FM.select(10_000)
        self.assertEqual(sel.batch, [])

    def test_chat_without_owner_text_is_marked_without_a_model_call(self):
        rel = self.transcript("quiet", [("Assistant", "Hello there.")])
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=exit"  # any call would defer
        res = self.run_batch()
        self.assertEqual(res["status"], "drained")
        self.assertIn(rel, self.done())

    def test_batch_respects_map_batch_chars(self):
        dc.CFG["frontier"]["map_batch_chars"] = 400
        for i in range(4):
            self.transcript(f"c{i}", [("Human", "x" * 150)], date=f"2029-12-2{i + 1}")
        self.assertEqual(len(FM.select(400).batch), 2)
        big = self.transcript("big", [("Human", "y" * 900)], date="2029-11-21")
        sel = FM.select(400)
        self.assertEqual([c.rel for c in sel.batch], [big])  # an oversized chat goes alone

    def test_crash_after_half_the_transcripts_then_resume_with_different_answers(self):
        qa, qb = "Explained rim thickness to the club.", "Decided to log kiln cooling weekly."
        ra = self.transcript("a", [("Human", qa)], date="2029-12-21")
        rb = self.transcript("b", [("Human", qb)], date="2029-12-22")
        self.set_script({"2029-12-21--a": [concept("Rim thickness", qa)],
                         "2029-12-22--b": [concept("Cooling log", qb, "decided")]}, {})
        real = FM.mark_done
        calls = []

        def crash_on_second(*a, **kw):
            calls.append(a)
            if len(calls) == 2:
                raise RuntimeError("simulated crash after B applied, before B is marked")
            return real(*a, **kw)

        with mock.patch.object(FM, "mark_done", crash_on_second):
            with self.assertRaises(RuntimeError):
                self.run_batch()
        self.assertEqual(set(self.done()), {ra})
        self.assertEqual(len(self.items()), 2)  # B's item was written
        # resumed model answers differently: other titles, same owner quotes
        self.set_script({"2029-12-22--b": [concept("Weekly cooling log", qb, "decided")]}, {})
        res = self.run_batch()
        self.assertEqual(res["status"], "done")
        self.assertEqual(set(self.done()), {ra, rb})
        items = self.items()
        self.assertEqual(len(items), 2)
        self.assertEqual(sorted(len(i.evidence) for i in items), [1, 1])

    def test_resume_is_a_noop_when_nothing_changed(self):
        q = "Explained shelf spacing."
        self.transcript("a", [("Human", q)])
        self.set_script({"2029-12-21--a": [concept("Shelf spacing", q)]}, {})
        self.run_batch()
        before = [(i.id, i.evidence, i.moves) for i in self.items()]
        self.assertEqual(self.run_batch()["status"], "drained")
        self.assertEqual([(i.id, i.evidence, i.moves) for i in self.items()], before)


class FailureTest(MapCase):
    def setUp(self):
        super().setUp()
        self.q = "Explained stacking limits."
        self.rel = self.transcript("a", [("Human", self.q)])
        self.set_script({"2029-12-21--a": [concept("Stacking limits", self.q)]}, {})

    def counter(self, call="extract") -> int:
        f = self.dirs["logs"] / f".fake-frontier-count-{call}"
        return int(f.read_text()) if f.exists() else 0

    def test_nonzero_exit_stops_the_run_with_the_ledger_unmarked(self):
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=exit"
        res = self.run_batch()
        self.assertEqual(res["status"], "stopped")
        self.assertEqual(self.done(), {})
        self.assertEqual(self.items(), [])
        self.assertEqual(self.counter(), 1)  # a usage limit is not retried

    def test_nonzero_exit_on_the_match_call_also_stops_and_marks_nothing(self):
        self.run_batch()
        q2 = "Stacking limits was on the agenda."
        self.transcript("b", [("Human", q2)], date="2029-12-22")
        self.set_script({"2029-12-22--b": [concept("Stacking limits", q2)]}, {})
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "match=exit"
        res = self.run_batch()
        self.assertEqual(res["status"], "stopped")
        self.assertEqual(set(self.done()), {self.rel})
        self.assertEqual(len(self.items()), 1)

    def test_malformed_reply_is_retried_once_then_succeeds(self):
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=malformed:1"
        res = self.run_batch()
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.counter(), 2)
        self.assertEqual(len(self.items()), 1)

    def test_persistent_malformed_reply_defers_parse_with_an_attempt_count(self):
        for mode in ("malformed", "empty", "prose", "xml"):
            self.setUp()
            os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = f"extract={mode}"
            res = self.run_batch()
            self.assertEqual(res["status"], "done", mode)
            self.assertEqual(self.counter(), 2, mode)  # first try plus exactly one retry
            self.assertEqual(self.done(), {}, mode)
            entry = self.ledger()["deferred"][self.rel]
            self.assertEqual((entry["reason"], entry["attempts"]), ("parse", 1), mode)
            self.assertEqual(self.items(), [], mode)

    def test_fenced_reply_is_accepted(self):
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=fenced"
        self.assertEqual(self.run_batch()["status"], "done")
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.counter(), 1)

    def test_skipped_after_max_parse_attempts_with_a_degraded_event(self):
        dc.CFG["frontier"]["max_parse_attempts"] = 2
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=prose"
        self.run_batch()
        self.assertEqual(self.ledger()["deferred"][self.rel]["attempts"], 1)
        self.run_batch()
        led = self.ledger()
        self.assertNotIn(self.rel, led["deferred"])
        self.assertEqual(led["skipped"][self.rel]["attempts"], 2)
        ev = [e for e in self.events() if e["kind"] == "degraded"]
        self.assertEqual(len(ev), 1)
        self.assertIn(self.rel, ev[0]["detail"])
        self.assertEqual(self.run_batch()["status"], "drained")

    def test_parse_failed_chat_is_retried_alone_so_it_cannot_poison_a_batch(self):
        self.set_script({"2029-12-21--a": [concept("Stacking limits", self.q)]}, {})
        q2 = "Decided on a loading plan."
        rel2 = self.transcript("b", [("Human", q2)], date="2029-12-22")
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=prose:2"
        self.run_batch()  # both fail together, once
        self.assertEqual(set(self.ledger()["deferred"]), {self.rel, rel2})
        sel = FM.select(100_000)
        self.assertEqual(len(sel.batch), 1)

    def test_match_parse_failure_defers_the_batch_and_creates_nothing(self):
        self.run_batch()
        q2 = "Stacking limits was on the agenda."
        rel2 = self.transcript("b", [("Human", q2)], date="2029-12-22")
        self.set_script({"2029-12-22--b": [concept("Stacking limits", q2)]}, {})
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "match=prose"
        self.run_batch()
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(len(self.items()[0].evidence), 1)
        self.assertEqual(self.ledger()["deferred"][rel2]["attempts"], 1)

    def test_vault_lock_timeout_defers_with_the_ledger_unmarked(self):
        dc.CFG["frontier"]["vault_lock_wait_seconds"] = 0.3
        fh = open(self.dirs["meta"] / "wiki.lock", "a")
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        res = self.run_batch()
        self.assertEqual(res["status"], "deferred")
        self.assertEqual(self.done(), {})
        self.assertEqual(self.items(), [])
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        self.assertEqual(self.run_batch()["status"], "done")

    def test_the_model_is_not_called_while_wiki_lock_is_held(self):
        # Model calls run without the vault lock; the lock is only for apply.
        seen = []
        import frontier_llm as L
        real = L.run_model

        def spy(name, prompt):
            fh = open(self.dirs["meta"] / "wiki.lock", "a")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except OSError:
                seen.append("held")
            finally:
                fh.close()
            return real(name, prompt)

        with mock.patch.object(L, "run_model", spy):
            self.run_batch()
        self.assertTrue(seen and set(seen) == {"free"})


class SeedLoopsTest(MapCase):
    def loop(self, lid, status, occ):
        occs = "\n".join(f"- '[[{o}]]'" for o in occ)
        (self.dirs["loops"] / f"{lid}.md").write_text(
            f"---\ntype: loop\nid: {lid}\nstatus: {status}\ntitle: Should I {lid}?\n"
            f"created: '2029-10-21'\nfirst_seen: '2029-05-31'\nlast_seen: '2029-06-20'\n"
            f"recurrence_count: {len(occ)}\noccurrences:\n{occs}\n---\n\n# t\n\n"
            f"## Theme\n\nA theme for {lid}.\n", encoding="utf-8")

    def test_one_known_unknown_item_per_open_loop_idempotent(self):
        self.loop("L0001", "open", ["sources/transcripts/2029/05/2029-05-31--a",
                                    "sources/transcripts/2029/06/2029-06-20--b"])
        self.loop("L0002", "paused", ["sources/transcripts/2029/06/2029-06-21--c"])
        self.loop("L0003", "archived", ["sources/transcripts/2029/06/2029-06-22--d"])
        self.loop("L0004", "resolved", ["sources/transcripts/2029/06/2029-06-23--e"])
        first = FM.seed_loops(at=AT)
        again = FM.seed_loops(at=AT)
        self.assertEqual((first["created"], again["created"]), (2, 0))
        items = {i.loops[0]: i for i in self.items()}
        self.assertEqual(set(items), {"L0001", "L0002"})
        it = items["L0001"]
        self.assertEqual(it.quadrant, "known_unknown")
        self.assertEqual(it.origin, "map")
        self.assertEqual([e["transcript"] for e in it.evidence],
                         ["sources/transcripts/2029/05/2029-05-31--a",
                          "sources/transcripts/2029/06/2029-06-20--b"])
        self.assertEqual({e["kind"] for e in it.evidence}, {"asked"})
        self.assertEqual(it.evidence[0]["date"], "2029-05-31")
        self.assertEqual(it.moves[0]["cause"], "seed from loop L0001")

    def test_seeded_evidence_names_the_chat_but_quotes_no_one(self):
        """An occurrence carries no quote, and the loop title is Dreamer's text (rule 13):
        filed as a quote it would read back to the owner as their own words. Some loop
        occurrences carry a `vault/` prefix; evidence is always stored vault-relative."""
        self.loop("L0001", "open", ["vault/sources/transcripts/2029/05/2029-05-31--a",
                                    "sources/transcripts/2029/06/2029-06-20--b"])
        FM.seed_loops(at=AT)
        FM.seed_loops(at=AT)
        (it,) = self.items()
        self.assertEqual([(e["transcript"], e["quote"]) for e in it.evidence],
                         [("sources/transcripts/2029/05/2029-05-31--a", ""),
                          ("sources/transcripts/2029/06/2029-06-20--b", "")])

    def test_a_new_occurrence_on_a_seeded_loop_is_added_once(self):
        self.loop("L0001", "open", ["sources/transcripts/2029/05/2029-05-31--a"])
        FM.seed_loops(at=AT)
        self.loop("L0001", "open", ["sources/transcripts/2029/05/2029-05-31--a",
                                    "sources/transcripts/2029/06/2029-06-20--b"])
        FM.seed_loops(at=AT)
        FM.seed_loops(at=AT)
        (item,) = self.items()
        self.assertEqual(len(item.evidence), 2)
        self.assertEqual(len(item.moves), 1)

    def test_seeded_items_are_matchable_by_chat_concepts(self):
        self.loop("L0001", "open", ["sources/transcripts/2029/05/2029-05-31--a"])
        FM.seed_loops(at=AT)
        (seed,) = self.items()
        q = "Explained should I L0001 in my own words."
        self.transcript("n", [("Human", q)])
        self.set_script({"2029-12-21--n": [concept(seed.title, q)]}, {})
        self.run_batch()
        (item,) = self.items()
        self.assertEqual(item.id, seed.id)
        self.assertEqual(len(item.evidence), 2)


class CliTest(MapCase):
    def test_cli_run_batch_prints_json_and_returns_zero_on_done_or_drained(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = FM.main(["run-batch"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["status"], "drained")


if __name__ == "__main__":
    unittest.main()
