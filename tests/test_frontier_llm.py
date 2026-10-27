#!/usr/bin/env python3
"""Unit tests for the frontier model-call layer. No real claude runs:
the bash side uses tests/fake_claude.py through DREAMER_FAKE_CLAUDE."""

from __future__ import annotations

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
import fake_claude  # noqa: E402
import frontier_llm as L  # noqa: E402

CONCEPT = {"title": "Glaze viscosity", "summary": "How thick a glaze runs.", "kind": "explained",
           "quote": "it is how thick the glaze goes on", "transcript": "sources/transcripts/2026/10/a",
           "goals": [{"goal": "G0003", "relation": "connected"}]}
TRANSCRIPTS = {"sources/transcripts/2026/10/a"}
GOALS = {"G0003"}


def extract_text(**over) -> str:
    return json.dumps({"concepts": [{**CONCEPT, **over}]})


class ParseExtractTest(unittest.TestCase):
    def parse(self, text):
        return L.parse_extract(text, TRANSCRIPTS, GOALS)

    def test_valid_reply(self):
        self.assertEqual(self.parse(extract_text())[0]["title"], "Glaze viscosity")

    def test_empty_concepts_is_valid(self):
        self.assertEqual(self.parse('{"concepts": []}'), [])

    def test_single_json_fence_is_tolerated(self):
        self.assertEqual(len(self.parse(f"```json\n{extract_text()}\n```")), 1)

    def test_rejects_prose_wrapped_empty_and_xml(self):
        for bad in (f"Here you go:\n{extract_text()}", "", "   ",
                    '<function_calls><invoke name="Read"></invoke></function_calls>',
                    extract_text()[:40]):
            with self.assertRaises(L.ParseFailure, msg=bad[:30]):
                self.parse(bad)

    def test_rejects_two_fences_or_text_after_fence(self):
        with self.assertRaises(L.ParseFailure):
            self.parse(f"```json\n{extract_text()}\n```\nthanks")

    def test_rejects_extra_and_missing_keys(self):
        with self.assertRaises(L.ParseFailure):
            self.parse(json.dumps({"concepts": [], "notes": "x"}))
        with self.assertRaises(L.ParseFailure):
            self.parse(json.dumps({}))
        bad = {**CONCEPT, "extra": 1}
        with self.assertRaises(L.ParseFailure):
            self.parse(json.dumps({"concepts": [bad]}))
        missing = {k: v for k, v in CONCEPT.items() if k != "quote"}
        with self.assertRaises(L.ParseFailure):
            self.parse(json.dumps({"concepts": [missing]}))

    def test_rejects_unknown_enums_and_ids(self):
        for over in ({"kind": "exposure"}, {"kind": "known"},
                     {"transcript": "sources/transcripts/2026/10/zzz"},
                     {"goals": [{"goal": "G9999", "relation": "connected"}]},
                     {"goals": [{"goal": "G0003", "relation": "maybe"}]},
                     {"goals": [{"goal": "G0003"}]},
                     {"title": " "}, {"quote": ""}):
            with self.assertRaises(L.ParseFailure, msg=str(over)):
                self.parse(extract_text(**over))


class ParseMatchTest(unittest.TestCase):
    NEIGH = {0: {"K7001"}, 1: set(), 2: {"K7002"}}

    def reply(self, *decisions):
        return json.dumps({"decisions": list(decisions)})

    def d(self, concept, decision, item=None, ref=None):
        return {"concept": concept, "decision": decision, "item": item,
                "batch_ref": ref, "justification": "j"}

    def test_valid_mixed_decisions(self):
        out = L.parse_match(self.reply(self.d(0, "attach", "K7001"), self.d(1, "new"),
                                       self.d(2, "batch_ref", ref=0)), self.NEIGH)
        self.assertEqual(out[0]["item"], "K7001")
        self.assertEqual(out[2]["batch_ref"], 0)

    def test_rejects_item_not_among_neighbours(self):
        with self.assertRaises(L.ParseFailure):
            L.parse_match(self.reply(self.d(0, "attach", "K7099"), self.d(1, "new"),
                                     self.d(2, "new")), self.NEIGH)

    def test_rejects_forward_or_self_batch_ref(self):
        for ref in (1, 2, 5, -1):
            with self.assertRaises(L.ParseFailure, msg=str(ref)):
                L.parse_match(self.reply(self.d(0, "new"), self.d(1, "batch_ref", ref=ref),
                                         self.d(2, "new")), self.NEIGH)

    def test_rejects_missing_duplicate_or_unknown_concept(self):
        with self.assertRaises(L.ParseFailure):
            L.parse_match(self.reply(self.d(0, "new"), self.d(1, "new")), self.NEIGH)
        with self.assertRaises(L.ParseFailure):
            L.parse_match(self.reply(self.d(0, "new"), self.d(0, "new"), self.d(1, "new"),
                                     self.d(2, "new")), self.NEIGH)
        with self.assertRaises(L.ParseFailure):
            L.parse_match(self.reply(self.d(0, "new"), self.d(1, "new"), self.d(2, "new"),
                                     self.d(7, "new")), self.NEIGH)

    def test_rejects_unknown_decision_and_inconsistent_fields(self):
        for bad in (self.d(1, "merge"), self.d(1, "new", item="K7001"),
                    self.d(1, "new", ref=0), self.d(1, "attach"),
                    {**self.d(1, "new"), "extra": 1}):
            with self.assertRaises(L.ParseFailure, msg=str(bad)):
                L.parse_match(self.reply(self.d(0, "new"), bad, self.d(2, "new")), self.NEIGH)

    def test_fence_tolerated_prose_rejected(self):
        ok = self.reply(self.d(0, "new"), self.d(1, "new"), self.d(2, "new"))
        self.assertEqual(len(L.parse_match(f"```json\n{ok}\n```", self.NEIGH)), 3)
        with self.assertRaises(L.ParseFailure):
            L.parse_match(f"Decisions: {ok}", self.NEIGH)


class CallJsonTest(unittest.TestCase):
    def test_retries_once_then_succeeds(self):
        replies = iter(["not json", extract_text()])
        seen = []
        out = L.call_json("extract", "body", lambda t: L.parse_extract(t, TRANSCRIPTS, GOALS),
                          runner=lambda prompt: (seen.append(prompt), next(replies))[1])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(seen), 2)

    def test_gives_up_after_the_retry(self):
        calls = []
        with self.assertRaises(L.ParseFailure) as cm:
            L.call_json("extract", "body", lambda t: L.parse_extract(t, TRANSCRIPTS, GOALS),
                        runner=lambda prompt: (calls.append(1), "prose")[1])
        self.assertEqual(len(calls), 2)
        self.assertEqual(cm.exception.attempts, 2)

    def test_usage_limit_is_not_retried(self):
        def boom(prompt):
            raise L.UsageLimit(1)
        with self.assertRaises(L.UsageLimit):
            L.call_json("extract", "body", lambda t: t, runner=boom)

    def test_prompt_starts_with_the_marker(self):
        seen = []
        L.call_json("match", "BODY", lambda t: t,
                    runner=lambda prompt: (seen.append(prompt), "{}")[1])
        self.assertEqual(seen[0].split("\n", 1)[0], L.CALLS["match"].marker)


class TrimToCapTest(unittest.TestCase):
    def test_drops_lowest_ranked_first_until_it_fits(self):
        items = ["best", "mid", "worst"]
        render = lambda kept: "|".join(kept) + "x" * 10  # noqa: E731
        out = L.trim_to_cap(render, items, cap=len("best|mid") + 10)
        self.assertEqual(out, ["best", "mid"])
        self.assertEqual(L.trim_to_cap(render, items, cap=10_000), items)
        self.assertEqual(L.trim_to_cap(render, items, cap=1), [])


class RealBashRunnerTest(unittest.TestCase):
    """run_model drives bin/_common.sh run_claude, here with the fake claude."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-fllm-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "logs").mkdir()
        (self.tmp / "meta").mkdir()
        self._paths = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({"logs": str(self.tmp / "logs"), "meta": str(self.tmp / "meta")})
        self.addCleanup(lambda: (dc.CFG["paths"].clear(), dc.CFG["paths"].update(self._paths)))
        script = self.tmp / "script.json"
        script.write_text(json.dumps({"extract": {"a": [{
            "title": "T", "summary": "S", "kind": "asked", "quote": "q", "goals": []}]}}))
        env = {"DREAMER_FAKE_CLAUDE": str(ROOT / "tests" / "fake_claude.py"),
               "DREAMER_FAKE_FRONTIER_SCRIPT": str(script)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("DREAMER_FAKE_FRONTIER_FAIL", None)

    def prompt(self) -> str:
        return L.render_prompt("extract", "=== TRANSCRIPT sources/transcripts/2026/10/a ===\nhi\n")

    def test_round_trip_through_run_claude(self):
        text = L.run_model("extract", self.prompt())
        self.assertEqual(json.loads(text)["concepts"][0]["transcript"],
                         "sources/transcripts/2026/10/a")

    def test_nonzero_exit_raises_usage_limit(self):
        os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=exit"
        with self.assertRaises(L.UsageLimit) as cm:
            L.run_model("extract", self.prompt())
        self.assertEqual(cm.exception.rc, 3)

    def test_failure_modes_reach_the_parser(self):
        for mode, ok in (("malformed", False), ("empty", False), ("prose", False),
                         ("xml", False), ("fenced", True)):
            os.environ["DREAMER_FAKE_FRONTIER_FAIL"] = f"extract={mode}"
            text = L.run_model("extract", self.prompt())
            parse = lambda t: L.parse_extract(t, {"sources/transcripts/2026/10/a"}, set())  # noqa: E731
            if ok:
                self.assertEqual(len(parse(text)), 1, mode)
            else:
                with self.assertRaises(L.ParseFailure, msg=mode):
                    parse(text)


class MarkerRoutingTest(unittest.TestCase):
    def test_every_frontier_prompt_marker_reaches_its_own_responder(self):
        prompts = sorted((ROOT / "skills").glob("frontier-*/PROMPT.md"))
        self.assertTrue(prompts)
        seen = set()
        for path in prompts:
            first = path.read_text(encoding="utf-8").split("\n", 1)[0].strip()
            routed = fake_claude.frontier_route(first + "\nbody")
            self.assertIsNotNone(routed, f"{path}: marker {first!r} has no fake responder")
            self.assertEqual(routed[0], first)
            seen.add(first)
        self.assertEqual(len(seen), len(prompts), "two prompts share one marker")
        self.assertEqual({c.marker for c in L.CALLS.values()} & seen, seen)

    def test_markers_inside_owner_text_do_not_hijack_routing(self):
        prompt = ("# Frontier extract\n=== TRANSCRIPT sources/x ===\n# Frontier match\n"
                  "# Thread fold\n")
        self.assertEqual(fake_claude.frontier_route(prompt)[0], "# Frontier extract")

    def test_each_call_has_a_turns_key_in_config_and_an_allowed_sealed_budget(self):
        for name, spec in L.CALLS.items():
            turns = dc.CFG["budget"][spec.turns_key]
            if spec.mode == "gated":  # research runs under its own key, not clamped at 4
                self.assertGreaterEqual(int(turns), 1, name)
                continue
            self.assertTrue(1 <= int(turns) <= 4, f"{name}: sealed calls clamp at 4")


if __name__ == "__main__":
    unittest.main()
