#!/usr/bin/env python3
"""Merge proposer — the safety net that makes rule 7's split-bias legitimate.

CLAUDE.md rule 7 tells the matcher to create a NEW loop on genuine
uncertainty, and justifies it by promising a false split self-heals: both
loops accrue occurrences and the weekly run proposes a merge. That promise is
only as good as this proposer. Measured against the owner-labelled golden set,
the original lexical-Jaccard-0.55 detector caught 2 of 9 confirmed duplicates
— so the split-bias was, in practice, shipping permanent duplicates.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import merge_proposals as MP  # noqa: E402

GOLDEN = ROOT / "vault" / ".vault-meta" / "golden-set.json"


class CandidateRecallTest(unittest.TestCase):
    """Stage A must not lose a duplicate before the judge ever sees it."""

    def _pairs(self, label: str) -> list[dict]:
        if not GOLDEN.exists():
            self.skipTest("no golden set on this machine")
        raw = json.loads(GOLDEN.read_text(encoding="utf-8"))
        pairs = raw if isinstance(raw, list) else raw.get("pairs", [])
        return [p for p in pairs if p.get("owner_label") == label]

    def test_candidate_floor_recalls_owner_confirmed_duplicates(self) -> None:
        same = self._pairs("same")
        self.assertGreaterEqual(len(same), 5, "golden set too small to test")
        missed = [p for p in same
                  if MP.similarity(p.get("a_title", ""), p.get("b_title", ""))
                  < MP.CANDIDATE_FLOOR]
        # Stage A is deliberately asymmetric: an extra candidate costs one
        # judge call, a missed one is a duplicate nothing will ever detect.
        self.assertEqual(
            missed, [],
            "candidate floor dropped owner-confirmed duplicates before "
            f"judging: {[p.get('a_title', '')[:50] for p in missed]}")

    def test_old_threshold_would_have_failed_this(self) -> None:
        """Pins the regression itself, so nobody 'simplifies' back to it."""
        same = self._pairs("same")
        caught = sum(1 for p in same
                     if MP.similarity(p.get("a_title", ""),
                                      p.get("b_title", "")) >= MP.THRESHOLD)
        self.assertLess(caught, len(same),
                        "if the old 0.55 cut now catches everything, this "
                        "test is stale — re-derive the floor from the data")


class IsolatedJudgments:
    """detect() persists judge verdicts, so a test that calls it would write
    into the real vault and leak a cached verdict into the next test — which
    is exactly how the outage test started seeing zero proposals."""

    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        self._jdir = tempfile.mkdtemp(prefix="dreamer-judgments-")
        self._orig_jpath = MP._judgments_path
        MP._judgments_path = lambda: Path(self._jdir) / "merge-judgments.json"

    def tearDown(self) -> None:  # noqa: N802
        MP._judgments_path = self._orig_jpath
        shutil.rmtree(self._jdir, ignore_errors=True)
        super().tearDown()


class JudgmentCacheTest(IsolatedJudgments, unittest.TestCase):
    """The cache must retire re-judging without retiring rule 7's self-heal."""

    TITLES = ("Should routing be intent-based or keyword-based?",
              "Should routing be intent-based or keyword-driven?")

    def _run(self, verdict, loops=None):
        calls: list[tuple[str, str]] = []

        def judge(a, b):
            calls.append((a, b))
            return {"verdict": verdict, "reason": "test"}

        loops = loops or [_loop("L0001", self.TITLES[0]),
                          _loop("L0002", self.TITLES[1])]
        orig_load, orig_neigh = MP.V.load_loops, MP._semantic_neighbours
        MP.V.load_loops = lambda **k: loops
        MP._semantic_neighbours = lambda ls, limit=4: set()
        sys.modules.setdefault("golden_set", type(sys)("golden_set"))
        import golden_set
        orig_gj = getattr(golden_set, "judge_llm", None)
        golden_set.judge_llm = judge
        try:
            proposals = MP.detect(judge="llm")
        finally:
            MP.V.load_loops, MP._semantic_neighbours = orig_load, orig_neigh
            if orig_gj:
                golden_set.judge_llm = orig_gj
        return proposals, calls

    def test_cached_distinct_is_not_rejudged(self) -> None:
        self._run("distinct")
        _, calls = self._run("distinct")
        self.assertEqual(calls, [], "a settled 'distinct' was re-paid for")

    def test_new_occurrence_reopens_a_cached_distinct(self) -> None:
        """Rule 7 promises a false split self-heals as both loops accrue
        occurrences. A cache keyed on titles alone would retire that promise,
        because the loops needing a second look keep their title."""
        self._run("distinct")
        grown = [_loop("L0001", self.TITLES[0], ["[[sources/transcripts/x]]"]),
                 _loop("L0002", self.TITLES[1])]
        _, calls = self._run("distinct", loops=grown)
        self.assertEqual(len(calls), 1,
                         "new evidence did not reopen the cached verdict")

    def test_judge_outage_is_never_cached(self) -> None:
        """An outage is a transient failure, not a judgment. Caching it would
        freeze a whole run's pairs on the strength of an exception."""
        self._run("error")
        self.assertEqual(MP._load_judgments(), {},
                         "an outage verdict was written to the cache")
        _, calls = self._run("distinct")
        self.assertEqual(len(calls), 1, "outage pair was not re-judged")

    def test_cache_frees_slots_so_the_queue_drains(self) -> None:
        """The bug this fixes: the cap was applied before de-dup, so settled
        pairs consumed judge slots and the backlog never moved."""
        loops = [_loop(f"L{i:04d}", f"Should routing be intent-based v{i}?")
                 for i in range(1, 5)]
        orig_cap = MP.MAX_JUDGED
        MP.MAX_JUDGED = 2
        try:
            _, first = self._run("distinct", loops=loops)
            _, second = self._run("distinct", loops=loops)
        finally:
            MP.MAX_JUDGED = orig_cap
        self.assertEqual(len(first), 2, "cap not respected")
        self.assertEqual(len(second), 2, "second run re-judged instead of "
                                         "advancing to unjudged pairs")
        self.assertFalse(set(first) & set(second),
                         "the same pairs were judged twice")


class SemanticPriorityTest(IsolatedJudgments, unittest.TestCase):
    def test_semantic_neighbours_outrank_lexical_band(self) -> None:
        """The cap must not drop the semantically-close, lexically-distant
        pairs — those are the entire reason the embedding leg was added."""
        loops = [
            _loop("L0001", "Newsletter growth by resharing curated posts"),
            _loop("L0002", "Reverse-engineering creator structure onto takes"),
            _loop("L0003", "Something else entirely about database hosting"),
        ]
        orig_load, orig_neigh, orig_cap = (
            MP.V.load_loops, MP._semantic_neighbours, MP.MAX_JUDGED)
        judged: list[tuple[str, str]] = []

        def fake_judge(a, b):
            judged.append((a[:20], b[:20]))
            return {"verdict": "distinct", "reason": "test"}

        MP.V.load_loops = lambda **k: loops
        MP._semantic_neighbours = lambda ls, limit=4: {("L0001", "L0002")}
        MP.MAX_JUDGED = 1
        sys.modules.setdefault("golden_set", type(sys)("golden_set"))
        import golden_set
        orig_gj = getattr(golden_set, "judge_llm", None)
        golden_set.judge_llm = fake_judge
        try:
            MP.detect(judge="llm")
        finally:
            MP.V.load_loops, MP._semantic_neighbours, MP.MAX_JUDGED = (
                orig_load, orig_neigh, orig_cap)
            if orig_gj:
                golden_set.judge_llm = orig_gj

        self.assertEqual(len(judged), 1, "cap not respected")
        self.assertIn("Newsletter growth", judged[0][0] + judged[0][1])


class JudgeOutageTest(IsolatedJudgments, unittest.TestCase):
    """A failing judge must not be indistinguishable from a clean 'distinct'.

    Rule 7 justifies the conservative-split bias on the promise that the weekly
    run proposes the merge back. If a judge outage silently yields zero
    proposals, false splits accumulate forever and — in rule 7's own words —
    nothing detects it. So an unjudgeable pair falls back to the deterministic
    token-overlap rule and the run reports how many pairs it could not judge.
    """

    def _run_with_judge(self, verdict_fn, titles):
        loops = [_loop("L0001", titles[0]), _loop("L0002", titles[1])]
        orig_load, orig_neigh = MP.V.load_loops, MP._semantic_neighbours
        MP.V.load_loops = lambda **k: loops
        MP._semantic_neighbours = lambda ls, limit=4: set()
        sys.modules.setdefault("golden_set", type(sys)("golden_set"))
        import golden_set
        orig_gj = getattr(golden_set, "judge_llm", None)
        golden_set.judge_llm = verdict_fn
        try:
            return MP.detect(judge="llm")
        finally:
            MP.V.load_loops, MP._semantic_neighbours = orig_load, orig_neigh
            if orig_gj:
                golden_set.judge_llm = orig_gj

    def test_judge_error_falls_back_to_lexical_instead_of_dropping(self) -> None:
        near_dupes = ("Should routing be intent-based or keyword-based?",
                      "Should routing be intent-based or keyword-driven?")
        # Sanity: this pair is well above the deterministic rule's threshold,
        # so dropping it would be a real lost merge, not a borderline call.
        self.assertGreaterEqual(MP.similarity(*near_dupes), MP.THRESHOLD)

        outage = lambda a, b: {"verdict": "error", "reason": "claude exited 1"}
        proposals = self._run_with_judge(outage, near_dupes)

        self.assertEqual(len(proposals), 1,
                         "judge outage silently swallowed a near-duplicate pair")
        self.assertIn("judge unavailable", proposals[0]["reason"])
        self.assertEqual(MP.detect.last_judge_errors, 1,
                         "run did not report that it failed to judge")

    def test_genuine_distinct_verdict_still_drops(self) -> None:
        distinct = lambda a, b: {"verdict": "distinct", "reason": "different"}
        proposals = self._run_with_judge(
            distinct, ("Should routing be intent-based or keyword-based?",
                       "Should routing be intent-based or keyword-driven?"))
        self.assertEqual(proposals, [],
                         "a real 'distinct' must still suppress the proposal")
        self.assertEqual(MP.detect.last_judge_errors, 0)


def _loop(lid: str, title: str, occurrences: list[str] | None = None):
    from types import SimpleNamespace
    return SimpleNamespace(id=lid, title=title, status="open",
                           recurrence_count=1,
                           occurrences=list(occurrences or []))


if __name__ == "__main__":
    unittest.main()
