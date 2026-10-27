#!/usr/bin/env python3
"""Marking a batch extracted must mark THAT batch, not a re-selection.

The jobs used to mark with `make_batch.py --limit N --mark-batch`, which runs
select() again. The queue is oldest-first, and since a grown chat is returned
to the queue (unmark_extracted), a transcript that re-entered the queue while
the batch was being extracted sorts ahead of it: the re-selection marked it
extracted although the model never saw it, and its new turns were lost.

Everything runs against a temp vault; the real vault is never touched.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dreamer_common as dc  # noqa: E402
import make_batch as mb  # noqa: E402


def page(n: int) -> str:
    return f"---\ntype: transcript\nmessage_count: {n}\n---\n\n# t\n\n## Human\n\nhi\n"


class ExactBatchMarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-exactmark-"))
        self.vault = self.tmp / "vault"
        self.out = self.vault / "sources" / "transcripts"
        self.meta = self.vault / ".vault-meta"
        for d in (self.out / "2025" / "01", self.out / "2026" / "09", self.meta):
            d.mkdir(parents=True, exist_ok=True)
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({"vault": str(self.vault), "sources": str(self.out),
                                "meta": str(self.meta)})

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, n: int = 4) -> str:
        f = self.vault / f"{rel}.md"
        f.write_text(page(n), encoding="utf-8")
        return rel

    def run_cli(self, argv: list[str], stdin: str = "") -> str:
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["make_batch.py", *argv]), \
             mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(buf):
            self.assertEqual(mb.main(), 0)
        return buf.getvalue()

    def test_marks_exactly_the_selected_batch(self) -> None:
        new = self.write("sources/transcripts/2026/09/2026-09-14--new")
        selected = json.loads(self.run_cli(["--limit", "5"]))
        self.assertEqual(selected["batch"], [new])

        # While the batch is being extracted, an OLD chat grows and is
        # returned to the queue; it sorts ahead of the batch.
        old = self.write("sources/transcripts/2025/01/2025-01-02--old", n=9)

        self.run_cli(["--mark-json"], stdin=json.dumps(selected))
        done = mb.extracted_set()
        self.assertIn(new, done)
        self.assertNotIn(old, done, "a transcript the model never saw was marked")
        self.assertEqual([mb.rel_of(f) for f in mb.select(5)], [old])

    def test_records_the_boundary_for_each_marked_page(self) -> None:
        rel = self.write("sources/transcripts/2026/09/2026-09-14--new", n=7)
        self.run_cli(["--mark-json"], stdin=json.dumps({"batch": [rel], "count": 1}))
        self.assertEqual(mb.extracted_through().get(rel), 7)

    def test_rejects_input_without_a_batch_list(self) -> None:
        self.write("sources/transcripts/2026/09/2026-09-14--new")
        for bad in ("", "not json", json.dumps({"count": 1}), json.dumps({"batch": "x"})):
            with self.subTest(bad=bad):
                buf = io.StringIO()
                with mock.patch.object(sys, "argv", ["make_batch.py", "--mark-json"]), \
                     mock.patch.object(sys, "stdin", io.StringIO(bad)), redirect_stdout(buf):
                    self.assertNotEqual(mb.main(), 0)
                self.assertEqual(mb.extracted_set(), set())

    def test_jobs_no_longer_re_select_to_mark(self) -> None:
        bin_dir = Path(__file__).resolve().parent.parent / "bin"
        for job in ("nightly-extract.sh", "backfill.sh"):
            with self.subTest(job=job):
                text = (bin_dir / job).read_text(encoding="utf-8")
                self.assertNotIn("--mark-batch", text)
                self.assertIn("--mark-json", text)


if __name__ == "__main__":
    unittest.main()
