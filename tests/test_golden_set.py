#!/usr/bin/env python3
"""DoD 6.6 — the golden-set runner must actually score, and must refuse to
score when scoring would mislead."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import golden_set as G  # noqa: E402


class GoldenSetCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-golden-"))
        for n in ("loops", "archive", "meta", "vault", "digests", "conclusions"):
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({
            "vault": str(self.tmp / "vault"), "loops": str(self.tmp / "loops"),
            "archive": str(self.tmp / "archive"), "meta": str(self.tmp / "meta"),
            "digests": str(self.tmp / "digests"),
            "conclusions": str(self.tmp / "conclusions"),
        })

    def tearDown(self) -> None:
        dc.CFG["paths"].clear(); dc.CFG["paths"].update(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("DREAMER_FAKE_CLAUDE", None)

    def write_set(self, pairs: list[dict]) -> None:
        (self.tmp / "meta" / "golden-set.json").write_text(
            json.dumps({"pairs": pairs}, indent=2), encoding="utf-8")

    def pair(self, a: str, b: str, label: str, source: str = "sampled") -> dict:
        return {"a": "", "a_title": a, "b": "", "b_title": b,
                "owner_label": label, "source": source}

    def complete_set(self) -> list[dict]:
        """10 same / 10 distinct, 5 of them hand-written adversarial."""
        same = [self.pair(f"How should agent memory persist across sessions {i}?",
                          f"How should agent memory persist across sessions {i}?",
                          "same") for i in range(8)]
        distinct = [self.pair(f"Which database should host the warehouse {i}?",
                              f"What indoor activity absorbs restlessness {i}?",
                              "distinct") for i in range(7)]
        adversarial = [
            self.pair("Should we adopt an LLM judge for matching?",
                      "Should we drop the LLM judge for matching?",
                      "distinct", "adversarial"),
            self.pair("Which database should we choose?",
                      "How should we host the database we chose?",
                      "distinct", "adversarial"),
            self.pair("How do I price this one contract?",
                      "How should I price work in general?",
                      "distinct", "adversarial"),
            self.pair("Where should durable agent state live?",
                      "How should agent memory persist across sessions?",
                      "same", "adversarial"),
            self.pair("Is the repost engine worth keeping?",
                      "Should I keep reposting top-creator content?",
                      "same", "adversarial"),
        ]
        # 20 pairs: 10 same (8 sampled + 2 adversarial), 10 distinct
        # (7 sampled + 3 adversarial), 5 adversarial in total.
        return same + distinct + adversarial


class ValidationTest(GoldenSetCase):
    def test_unlabelled_set_is_not_scoreable(self) -> None:
        self.write_set([self.pair("a", "b", "")] * 20)
        ok, problems = G.validate(G.load())
        self.assertFalse(ok)
        self.assertTrue(any("unlabelled" in x for x in problems))

    def test_placeholder_adversarial_rows_are_caught(self) -> None:
        pairs = self.complete_set()
        pairs[-1] = {"a": "", "a_title": "<hand-written near-miss A>", "b": "",
                     "b_title": "<hand-written near-miss B>",
                     "owner_label": "same", "source": "adversarial"}
        self.write_set(pairs)
        ok, problems = G.validate(G.load())
        self.assertFalse(ok)
        self.assertTrue(any("hand-written adversarial" in x for x in problems))

    def test_imbalanced_set_is_flagged(self) -> None:
        pairs = [self.pair(f"q{i}", f"q{i}", "same") for i in range(18)]
        pairs += [self.pair("x", "y", "distinct", "adversarial") for _ in range(5)]
        self.write_set(pairs)
        ok, problems = G.validate(G.load())
        self.assertFalse(ok)
        self.assertTrue(any("balance" in x for x in problems))

    def test_complete_set_validates(self) -> None:
        self.write_set(self.complete_set())
        ok, problems = G.validate(G.load())
        self.assertTrue(ok, f"unexpected problems: {problems}")


class ScoringTest(GoldenSetCase):
    def test_incomplete_set_refuses_to_produce_a_number(self) -> None:
        """A partial score reads as a result. It must not be emitted."""
        self.write_set([self.pair("a", "b", "")] * 20)
        out = G.run("lexical")
        self.assertFalse(out["scored"])
        self.assertNotIn("accuracy_overall", out)
        self.assertIn("would read as", out["note"])

    def test_lexical_judge_scores_and_stratifies(self) -> None:
        self.write_set(self.complete_set())
        out = G.run("lexical")
        self.assertTrue(out["scored"])
        self.assertEqual(out["n"], 20)
        self.assertIsNotNone(out["accuracy_overall"])
        # Stratified reporting is the point: sampled pairs are easy, the
        # adversarial ones are where a lexical judge should struggle.
        self.assertIsNotNone(out["accuracy_sampled"])
        self.assertIsNotNone(out["accuracy_adversarial"])
        self.assertEqual(out["n_adversarial"], 5)
        self.assertGreater(out["accuracy_sampled"], out["accuracy_adversarial"],
                           "lexical overlap should do worse on near-misses")

    def test_false_merge_and_split_counted_separately(self) -> None:
        """These are not symmetric: a false merge is undetectable downstream."""
        self.write_set(self.complete_set())
        out = G.run("lexical")
        self.assertIn("false_merge", out)
        self.assertIn("false_split", out)
        self.assertEqual(
            out["false_merge"] + out["false_split"],
            sum(1 for r in out["rows"] if not r["agree"]))

    def test_gate_is_enforced(self) -> None:
        self.write_set(self.complete_set())
        out = G.run("lexical")
        self.assertEqual(out["passes"], out["accuracy_overall"] >= G.GATE)

    def test_result_is_persisted_for_regression(self) -> None:
        self.write_set(self.complete_set())
        G.run("lexical")
        self.assertTrue((self.tmp / "meta" / "golden-set-result-lexical.json").exists())

    def test_llm_judge_path_parses_a_verdict(self) -> None:
        fake = self.tmp / "fake_judge.sh"
        fake.write_text('#!/usr/bin/env bash\necho \'{"verdict":"same",'
                        '"confidence":"high","reason":"fixture"}\'\n',
                        encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["DREAMER_FAKE_CLAUDE"] = str(fake)
        self.write_set(self.complete_set())
        out = G.run("llm")
        self.assertTrue(out["scored"])
        self.assertEqual(out["errors"], 0)
        # Every verdict is "same", so accuracy must equal the share of
        # same-labelled pairs — proving the runner scores the judge's real
        # output rather than a hardcoded path.
        expected = sum(1 for r in out["rows"] if r["label"] == "same") / out["n"]
        self.assertAlmostEqual(out["accuracy_overall"], expected, places=6)

    def test_unparseable_judge_output_counts_as_error_not_agreement(self) -> None:
        fake = self.tmp / "bad_judge.sh"
        fake.write_text('#!/usr/bin/env bash\necho "I think they are similar"\n',
                        encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["DREAMER_FAKE_CLAUDE"] = str(fake)
        self.write_set(self.complete_set())
        out = G.run("llm")
        self.assertEqual(out["n"], 0)
        self.assertEqual(out["errors"], 20)
        self.assertFalse(out["passes"], "no verdicts must never pass the gate")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TemplateDetectionTest(GoldenSetCase):
    """An unedited scaffold must never pass the gate. Placeholders are embedded
    mid-string ('Should we adopt <X>?'), so a startswith check is insufficient."""

    def test_midstring_placeholder_is_detected(self) -> None:
        self.assertTrue(G._is_template({"a_title": "Should we adopt <X>?",
                                        "b_title": "Should we drop <X>?"}))

    def test_leading_placeholder_is_detected(self) -> None:
        self.assertTrue(G._is_template({"a_title": "<hand-written near-miss A>",
                                        "b_title": "real title"}))

    def test_real_titles_are_not_flagged(self) -> None:
        self.assertFalse(G._is_template(
            {"a_title": "Should we adopt an LLM judge for matching?",
             "b_title": "Should we drop the LLM judge for matching?"}))

    def test_unedited_scaffold_is_rejected_wholesale(self) -> None:
        import calibrate
        calibrate.golden_scaffold()
        ok, problems = G.validate(G.load())
        self.assertFalse(ok)
        self.assertTrue(any("0 hand-written adversarial" in x for x in problems),
                        f"expected zero credited, got: {problems}")
