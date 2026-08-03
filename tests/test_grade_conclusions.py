#!/usr/bin/env python3
"""Conclusion-quality rubric. Guards the checks that guard the conclusions."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import grade_conclusions as G  # noqa: E402

GOOD = """\
## The loop, restated

Whether to buy hardware now or shrink the database first.

## What you previously concluded

- **[✓ accepted]** The two reference tables are the actual growth driver.
  - source: `[[sources/transcripts/2026/05/2026-05-03--nas]]`
- **[~ provisional]** An N100 board would satisfy the constraints.
  - source: `[[sources/transcripts/2026/05/2026-05-11--boards]]`

## Evidence ledger

✓ accepted: 1 · ~ provisional: 1

## Synthesis

Do not buy hardware yet. Export the two reference tables to Parquet and
measure what Postgres actually weighs afterward. That number has never been
measured and every downstream hardware decision depends on it, so buying now
means sizing a machine against a figure nobody has. The export is reversible
and costs an afternoon; the hardware is neither. Once the measurement exists,
the choice between an N100 board and a larger box is arithmetic rather than
argument, and it can be made in ten minutes instead of another week.

## Open sub-questions

- After the export, what does PostgreSQL actually weigh?
"""

# Fluent, cited, and useless: it surveys and refuses to land anywhere. This is
# the exact artifact the rubric exists to catch, because it reads as thoughtful.
HEDGE = """\
## The loop, restated

Whether to buy hardware now or shrink the database first.

## Synthesis

It depends on a number of factors. There is no single answer here, and it
could go either way depending on your priorities. Various factors bear on the
question, and ultimately it is up to you to weigh them against each other.
Reasonable people differ, and the tradeoffs are genuinely complex ones that
resist any simple resolution in either direction whatsoever.
"""


class RubricTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-grade-"))
        (self.tmp / "conclusions").mkdir()
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"]["conclusions"] = str(self.tmp / "conclusions")

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, body: str, **fm) -> Path:
        path = self.tmp / "conclusions" / name
        base = {"type": "conclusion", "loop": "L0001", "route": "mixed",
                "confidence": "medium", "created": "2026-08-02"}
        base.update(fm)
        dc.write_page(path, base, body)
        return path

    def test_good_conclusion_scores_high(self) -> None:
        r = G.grade(self._write("good.md", GOOD))
        self.assertGreaterEqual(r["score"], 0.9, r["failed"])
        self.assertEqual(r["claims"], 2)
        self.assertEqual(r["accepted"], 1)

    def test_fluent_hedge_is_caught(self) -> None:
        """The failure mode is a page that reads well and decides nothing."""
        r = G.grade(self._write("hedge.md", HEDGE))
        self.assertLess(r["score"], 0.6, "a pure hedge must not pass")
        self.assertIn("commits_to_an_answer", r["failed"])
        self.assertIn("not_pure_hedge", r["failed"])
        self.assertIn("every_claim_cited", r["failed"])

    def test_ungraded_claims_are_detected(self) -> None:
        """Conclusions written before evidence grading cite but do not grade.

        Citation proves a source said it, nothing more (rule 10), so an
        ungraded claim set must not score as a checkable one.
        """
        body = GOOD.replace("**[✓ accepted]** ", "").replace(
            "**[~ provisional]** ", "").replace(
            "## Evidence ledger\n\n✓ accepted: 1 · ~ provisional: 1\n\n", "")
        r = G.grade(self._write("ungraded.md", body))
        self.assertEqual(r["claims"], 0)
        self.assertIn("claims_are_graded", r["failed"])
        self.assertIn("has_evidence_ledger", r["failed"])

    def test_deciding_check_is_not_vacuous(self) -> None:
        """A check that everything passes measures nothing — and so does one
        that everything fails. The first version failed 7 of 9 real
        conclusions, including ones that plainly named their deciding test.
        """
        passes = G.grade(self._write("a.md", GOOD))
        # Same shape, same commitment, but names no test that would resolve it.
        # Built explicitly rather than by string surgery on GOOD: a replace
        # that silently misses (line wrapping) makes the negative case pass and
        # the assertion vacuous, which is the very thing under test here.
        no_settle = GOOD.split("## Synthesis")[0] + (
            "## Synthesis\n\n"
            "Do not buy hardware yet. Export the two reference tables to "
            "Parquet first and keep the existing disks in service, since the "
            "medallion flow means only the gold tier needs fast random "
            "access at all in this design.\n\n"
            "## Open sub-questions\n\n"
            "- Is USB 3.0 acceptable for the bronze tier?\n")
        fails = G.grade(self._write("b.md", no_settle))
        self.assertNotIn("names_what_would_settle_it", passes["failed"])
        self.assertIn("names_what_would_settle_it", fails["failed"])

    def test_confidence_must_be_a_real_grade(self) -> None:
        r = G.grade(self._write("c.md", GOOD, confidence="unknown"))
        self.assertIn("confidence_declared", r["failed"])




class VersionVsVarianceTest(RubricTest):
    """A superseded page and its replacement are versions, not samples.

    Reporting 73% -> 100% as 'variance' describes a fix landing as instability,
    and hides real variance underneath it.
    """

    def test_superseded_flag_is_read(self) -> None:
        old = G.grade(self._write(
            "old.md", GOOD,
            superseded_by="conclusions/2026-08-02--new"))
        new = G.grade(self._write("new.md", GOOD))
        self.assertTrue(old["superseded"])
        self.assertFalse(new["superseded"])

if __name__ == "__main__":
    unittest.main()
