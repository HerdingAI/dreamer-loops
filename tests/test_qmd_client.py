#!/usr/bin/env python3
"""Unit tests for the warm qmd client. No real qmd runs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import qmd_client as QC  # noqa: E402

FAKE = [sys.executable, str(ROOT / "tests" / "fake_qmd_mcp.py")]
TIMEOUT = 0.8


class QmdClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "calls.jsonl"

    def client(self, mode="ok", **env):
        patch = mock.patch.dict(os.environ, {"FAKE_QMD_MODE": mode,
                                             "FAKE_QMD_LOG": str(self.log), **env})
        patch.start()
        self.addCleanup(patch.stop)
        return QC.QmdClient(argv=FAKE, timeout=TIMEOUT)

    def sent(self) -> list[dict]:
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    # parsing -------------------------------------------------------------
    def test_hits_parse_with_line_and_docid(self):
        with self.client() as c:
            hits, degraded = c.query("fire the test tile", "transcripts")
        self.assertFalse(degraded)
        self.assertEqual([h["docid"] for h in hits], ["#aaa111", "#bbb222"])
        self.assertEqual(set(hits[0]), {"docid", "file", "line", "title", "score", "snippet"})
        self.assertEqual(hits[0]["line"], 321)
        self.assertEqual(hits[0]["score"], 1.0)
        self.assertIn("fired the test tile", hits[0]["snippet"])

    def test_empty_result_is_not_degraded(self):
        with self.client() as c:
            self.assertEqual(c.query("anything", "frontier"), ([], False))

    def test_client_is_warm_across_calls(self):
        with self.client() as c:
            pid = c.pid
            c.query("one", "transcripts")
            c.query("two", "transcripts")
            self.assertEqual(c.pid, pid)
        self.assertEqual(len(self.sent()), 2)

    # what is sent --------------------------------------------------------
    def test_collection_filter_limit_and_rerank_off_are_passed(self):
        with self.client() as c:
            c.query("fire the test tile", "transcripts", limit=4)
        args = self.sent()[0]
        self.assertEqual(args["collections"], ["transcripts"])
        self.assertEqual(args["limit"], 4)
        self.assertIs(args["rerank"], False)
        self.assertEqual([s["type"] for s in args["searches"]], ["lex", "vec"])
        self.assertEqual(args["searches"][1]["query"], "fire the test tile")

    def test_lex_query_drops_operators_that_change_meaning(self):
        with self.client() as c:
            c.query('"firing" -free tiers', "transcripts")
        lex = self.sent()[0]["searches"][0]["query"]
        self.assertEqual(lex, "firing tiers")

    def test_multi_line_text_is_sent_as_one_line(self):
        # qmd refuses a structured search containing a newline; callers pass "title\nsummary".
        with self.client() as c:
            hits, degraded = c.query("fire the test tile\nwith a  small\tbatch", "transcripts")
        self.assertFalse(degraded)
        self.assertEqual(len(hits), 2)
        for s in self.sent()[0]["searches"]:
            self.assertEqual(s["query"], "fire the test tile with a small batch")

    def test_lex_only_sends_one_keyword_search_and_no_meaning_search(self):
        with self.client() as c:
            hits, degraded = c.query('Kiln Safety Code "sign-off"', "transcripts", limit=5, lex_only=True)
        self.assertFalse(degraded)
        self.assertEqual(len(hits), 2)
        args = self.sent()[0]
        self.assertEqual(args["searches"], [{"type": "lex", "query": "Kiln Safety Code sign-off"}])
        self.assertEqual(args["limit"], 5)

    def test_lex_only_with_no_usable_words_is_empty_without_asking(self):
        with self.client() as c:
            self.assertEqual(c.query('-"" -x', "transcripts", lex_only=True), ([], False))
        self.assertFalse(self.log.exists())  # the fake logs only tool calls: none was made

    def test_default_limit_comes_from_config(self):
        with self.client() as c:
            c.query("x", "transcripts")
        self.assertEqual(self.sent()[0]["limit"], dc.CFG["frontier"]["qmd_default_limit"])

    # degradation ---------------------------------------------------------
    def test_crashed_server_is_degraded(self):
        with self.client("crash") as c:
            self.assertEqual(c.query("x", "transcripts"), ([], True))

    def test_hung_server_is_degraded_within_timeout_and_killed(self):
        with self.client("hang") as c:
            t0 = time.monotonic()
            self.assertEqual(c.query("x", "transcripts"), ([], True))
            self.assertLess(time.monotonic() - t0, TIMEOUT + 1.5)
            proc = c._proc
            self.assertIsNotNone(proc.poll(), "hung server must be killed")
            # a broken client stays degraded and does not respawn
            self.assertEqual(c.query("y", "transcripts"), ([], True))

    def test_hung_initialize_is_degraded_and_killed(self):
        with self.client("hang_init") as c:
            proc = c._proc
            self.assertEqual(c.query("x", "transcripts"), ([], True))
            self.assertIsNotNone(proc.poll())

    def test_missing_binary_is_degraded_not_raised(self):
        with QC.QmdClient(argv=["/nonexistent/qmd-binary"], timeout=TIMEOUT) as c:
            self.assertEqual(c.query("x", "transcripts"), ([], True))

    def test_bad_json_error_and_iserror_are_degraded(self):
        for mode in ("badjson", "error", "iserror", "noshape"):
            with self.subTest(mode=mode), self.client(mode) as c:
                self.assertEqual(c.query("x", "transcripts"), ([], True))

    def test_hit_without_line_degrades_the_result(self):
        with self.client("noline") as c:
            self.assertEqual(c.query("x", "transcripts"), ([], True))

    def test_unregistered_collection_is_degraded_not_empty(self):
        # qmd answers an unknown collection with "no results"; that must never
        # read as "never discussed".
        with self.client(FAKE_QMD_COLLECTIONS="vault, transcripts") as c:
            self.assertEqual(c.query("x", "frontier"), ([], True))
        self.assertFalse(self.log.exists(), "no query should be sent")

    def test_process_is_reaped_on_exit(self):
        with self.client() as c:
            proc = c._proc
            c.query("x", "transcripts")
        self.assertIsNotNone(proc.poll())

    def test_query_outside_context_is_degraded(self):
        c = self.client()
        self.assertEqual(c.query("x", "transcripts"), ([], True))


class ParseTest(unittest.TestCase):
    def test_collections_line_parsed_from_instructions(self):
        text = "QMD...\n\nCollections (scope with `collection` parameter): vault, wisdom\nCall"
        self.assertEqual(QC.parse_collections(text), {"vault", "wisdom"})
        self.assertIsNone(QC.parse_collections("no such line"))


class DisplayPathTest(unittest.TestCase):
    """qmd reports a normalized display path, not the file's real name (a hit resolved
    against the raw path would read as 'not on disk')."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "transcripts"
        (self.base / "2026" / "01").mkdir(parents=True)

    def touch(self, rel: str) -> Path:
        path = self.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        return path

    def test_handelize_matches_qmd(self):
        # Cases copied from qmd 2.5.3 store.js handelize(): case is kept, runs of
        # anything but a letter, digit or "$" become one "-", the extension stays.
        self.assertEqual(QC.handelize("2026/01/2026-01-05--glaze-notes.md"),
                         "2026/01/2026-01-05-glaze-notes.md")
        self.assertEqual(QC.handelize("Some Book (2nd ed).md"), "Some-Book-2nd-ed.md")
        self.assertEqual(QC.handelize("L0042.md"), "L0042.md")
        self.assertEqual(QC.handelize("a b/c.d.md"), "a-b/c-d.md")
        self.assertEqual(QC.handelize("Café__notes.md"), "Café-notes.md")

    def test_resolves_a_normalized_name_to_the_real_file(self):
        real = self.touch("2026/01/2026-01-05--glaze-notes.md")
        self.assertEqual(QC.resolve_display_path(self.base, "2026/01/2026-01-05-glaze-notes.md"), real)

    def test_exact_path_wins(self):
        real = self.touch("2025/12/plain.md")
        self.assertEqual(QC.resolve_display_path(self.base, "2025/12/plain.md"), real)

    def test_normalized_directory_segment(self):
        real = self.touch("Studio Books/Kiln Notes.md")
        self.assertEqual(QC.resolve_display_path(self.base, "Studio-Books/Kiln-Notes.md"), real)

    def test_missing_or_ambiguous_is_none(self):
        self.assertIsNone(QC.resolve_display_path(self.base, "2025/12/gone.md"))
        self.touch("2025/12/a--b.md")
        self.touch("2025/12/a  b.md")
        self.assertIsNone(QC.resolve_display_path(self.base, "2025/12/a-b.md"))

    def test_never_leaves_the_base(self):
        outside = Path(self.tmp.name) / "secret.md"
        outside.write_text("x", encoding="utf-8")
        self.assertIsNone(QC.resolve_display_path(self.base, "../secret.md"))
        (self.base / "link").symlink_to(Path(self.tmp.name))
        self.assertIsNone(QC.resolve_display_path(self.base, "link/secret.md"))


if __name__ == "__main__":
    unittest.main()
