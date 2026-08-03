#!/usr/bin/env python3
"""DoD 6.9 — the digest must carry run-level events, and must not pad."""

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
import digest as G  # noqa: E402
import vault as V  # noqa: E402

D = _dt.date.fromisoformat


class DigestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-digest-"))
        for n in ("loops", "conclusions", "concepts", "archive", "digests",
                  "sources", "meta", "vault"):
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
        self._orig = dict(dc.CFG["paths"])
        self._decay = dict(dc.CFG["decay"])
        dc.CFG["paths"].update({k: str(self.tmp / v) for k, v in {
            "vault": "vault", "loops": "loops", "conclusions": "conclusions",
            "concepts": "concepts", "archive": "archive", "digests": "digests",
            "sources": "sources", "meta": "meta"}.items()})
        dc.CFG["decay"]["go_live_date"] = "2026-08-01"

    def tearDown(self) -> None:
        dc.CFG["paths"].clear(); dc.CFG["paths"].update(self._orig)
        dc.CFG["decay"].clear(); dc.CFG["decay"].update(self._decay)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, date: str, slug: str) -> str:
        rel = Path("sources/transcripts") / date[:4] / date[5:7] / f"{date}--{slug}.md"
        full = self.tmp / "vault" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("---\ntype: transcript\n---\n\nx\n", encoding="utf-8")
        return f"[[{rel.with_suffix('')}]]"

    def write_events(self, events: list[dict]) -> None:
        (self.tmp / "meta" / "run-state.json").write_text(
            json.dumps({"events": events}), encoding="utf-8")


class RunEventsTest(DigestCase):
    def test_deferral_event_reaches_the_digest(self) -> None:
        self.write_events([{"at": "2026-08-09T02:00:00", "job": "nightly-extract",
                            "kind": "deferral",
                            "detail": "deferred (exit 1); work resumes next run"}])
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertIn("What happened this week", text)
        self.assertIn("deferral", text)
        self.assertIn("work resumes next run", text)

    def test_recovery_event_reaches_the_digest(self) -> None:
        self.write_events([{"at": "2026-08-09T03:00:00", "job": "weekly-dream",
                            "kind": "recovery",
                            "detail": "1 loop(s) left in 'researching' were reset"}])
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertIn("recovery", text)
        self.assertIn("reset", text)

    def test_events_are_cleared_so_they_do_not_repeat(self) -> None:
        self.write_events([{"at": "x", "kind": "deferral", "detail": "one time"}])
        first = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertIn("one time", first)
        second = G.build(ref=D("2026-08-16")).read_text(encoding="utf-8")
        self.assertNotIn("one time", second, "a stale event must not re-report")

    def test_no_events_means_no_section(self) -> None:
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertNotIn("What happened this week", text)

    def test_events_appear_above_the_first_section(self) -> None:
        """Same class of information as the freshness banner: the system did
        not do what a quiet digest would imply."""
        self.write_events([{"at": "x", "kind": "deferral", "detail": "d"}])
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertLess(text.index("What happened this week"),
                        text.index("## "), "events must precede all sections")


class PaddingTest(DigestCase):
    def test_empty_sections_are_omitted_not_padded(self) -> None:
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertNotIn("## Decisions awaiting you", text)
        self.assertNotIn("## Archiving soon", text)
        self.assertIn("Nothing to report under:", text)
        self.assertIn("Decisions awaiting you", text, "must still be accounted for")

    def test_populated_sections_are_kept(self) -> None:
        loop = V.create_loop("A recurring question?",
                             self.transcript("2026-08-05", "a"), D("2026-08-05"))
        V.add_occurrence(loop, self.transcript("2026-08-07", "b"), D("2026-08-07"))
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertIn("## Growing loops", text)
        self.assertIn("A recurring question?", text)

    def test_first_section_is_never_empty(self) -> None:
        """G5: the reader's first content must not be a non-event."""
        loop = V.create_loop("A recurring question?",
                             self.transcript("2026-08-05", "a"), D("2026-08-05"))
        V.add_occurrence(loop, self.transcript("2026-08-07", "b"), D("2026-08-07"))
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        first = text[text.index("## "):]
        body = first.split("\n", 2)[2].strip()
        self.assertTrue(body and not body.startswith("_No"),
                        f"first section opened empty: {body[:60]!r}")


class RouteStatsTest(DigestCase):
    def test_per_route_counts_appear_in_run_stats(self) -> None:
        """§6.5 requires this specifically to catch decision-only becoming an
        escape hatch and wisdom producing nothing."""
        a = V.create_loop("Q1?", self.transcript("2026-08-05", "a"), D("2026-08-05"))
        a.route = "wisdom"; a.status = "paused"; a.conclusion = "conclusions/c"; a.save()
        b = V.create_loop("Q2?", self.transcript("2026-08-06", "b"), D("2026-08-06"))
        b.route = "decision-only"; b.status = "decision-only"; b.save()
        text = G.build(ref=D("2026-08-09")).read_text(encoding="utf-8")
        self.assertIn("routes:", text)
        self.assertIn("wisdom=1", text)
        self.assertIn("decision-only=1", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
