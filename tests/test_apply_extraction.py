#!/usr/bin/env python3
"""Extraction-output parsing, including the narrow malformed-JSON repair.

The repair exists because a real backfill batch (2026-08-02) cost $12.55 and
was discarded over a single missing brace. It is deliberately narrow, so these
tests pin the ceiling as hard as the floor: a repair that quietly widens into
"make anything parse" would start inventing candidates the model never emitted,
and every downstream loop page would inherit that fabrication with a citation
attached.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import apply_extraction as AE  # noqa: E402


class ExtractJsonTest(unittest.TestCase):
    def test_plain_object(self) -> None:
        self.assertEqual(AE._extract_json('{"candidates": []}'),
                         {"candidates": []})

    def test_fenced_object(self) -> None:
        raw = 'here you go:\n```json\n{"candidates": [], "skipped": []}\n```\n'
        self.assertEqual(AE._extract_json(raw),
                         {"candidates": [], "skipped": []})

    def test_preamble_before_object(self) -> None:
        self.assertEqual(AE._extract_json('Sure!\n{"candidates": []}'),
                         {"candidates": []})

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(AE._extract_json("no json here at all"))
        self.assertIsNone(AE._extract_json(""))

    def test_non_dict_rejected(self) -> None:
        self.assertIsNone(AE._extract_json('["a", "b"]'))


class BraceRepairTest(unittest.TestCase):
    """The exact shape observed live, plus the boundaries of the repair."""

    def test_repairs_the_live_failure_shape(self) -> None:
        # A candidate closed its nested `match` object but not itself, then
        # started the next candidate. This is the batch-10 defect verbatim.
        raw = ('{"candidates":['
               '{"title":"A","match":{"decision":"new"}'      # <- missing }
               ',{"title":"B","match":{"decision":"new"}}'
               ']}')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        obj = AE._extract_json(raw)
        self.assertIsNotNone(obj, "unambiguous missing brace was not repaired")
        self.assertEqual(len(obj["candidates"]), 2)
        self.assertEqual([c["title"] for c in obj["candidates"]], ["A", "B"])

    def test_repair_preserves_every_field(self) -> None:
        raw = ('{"candidates":['
               '{"title":"A","evidence":"quote","match":{"loop_id":null}'
               ',{"title":"B","evidence":"other","match":{"loop_id":"L0001"}}'
               '],"skipped":[{"topic":"t","reason":"resolved"}]}')
        obj = AE._extract_json(raw)
        first = obj["candidates"][0]
        self.assertEqual(first["evidence"], "quote")
        self.assertIsNone(first["match"]["loop_id"])
        self.assertEqual(obj["skipped"][0]["reason"], "resolved")

    def test_truncated_output_is_not_repaired(self) -> None:
        # A response cut off mid-flight is NOT unambiguous — repairing it would
        # silently accept a partial batch as a complete one.
        self.assertIsNone(AE._extract_json('{"candidates":[{"title":"A"'))

    def test_unescaped_quote_is_not_repaired(self) -> None:
        # Broken string escaping is a different defect with no unique minimal
        # fix; it must fail loudly rather than be guessed at.
        self.assertIsNone(
            AE._extract_json('{"candidates":[{"title":"he said "hi" ok"}]}'))

    def test_repair_is_bounded(self) -> None:
        # Pathological input must not spin: the loop is capped.
        raw = '{"a":1' + ',{"b":2' * (AE._MAX_BRACE_REPAIRS + 5) + '}'
        self.assertIsNone(AE._extract_json(raw))


if __name__ == "__main__":
    unittest.main()
