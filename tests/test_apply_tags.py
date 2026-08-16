#!/usr/bin/env python3
"""Tag-backfill applier (U7) — scripts/apply_tags.py.

The one-time backfill retro-tags the ~107 pre-vocabulary loops. Unlike the
nightly extraction path (which degrades to "no tags" when no vocabulary
exists), a backfill without a frozen vocabulary is meaningless, so a missing
vocabulary is a HARD error here and no page may be modified.

The other invariants under test:
- valid vocabulary tags land in frontmatter and the page BODY stays
  byte-identical (frontmatter-only change),
- an already-tagged loop is never overwritten — zero diff on the file,
- an out-of-vocabulary tag is dropped with a logged reason and the rest of
  the batch proceeds (CLAUDE.md rule 4),
- an unknown loop id is skipped with a stat, not a crash,
- malformed JSON fails loudly before any page is touched.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dreamer_common as dc  # noqa: E402
import vault as V  # noqa: E402
import apply_tags as AT  # noqa: E402


class ApplyTagsBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-tag-backfill-"))
        for name in ("vault", "loops", "conclusions", "archive", "digests",
                     "sources", "meta"):
            (self.tmp / name).mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({
            "vault": str(self.tmp / "vault"),
            "loops": str(self.tmp / "loops"),
            "conclusions": str(self.tmp / "conclusions"),
            "archive": str(self.tmp / "archive"),
            "digests": str(self.tmp / "digests"),
            "sources": str(self.tmp / "sources"),
            "meta": str(self.tmp / "meta"),
        })

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig_paths)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def freeze_vocab(self, tags: list[str]) -> None:
        (self.tmp / "meta" / "tag-vocabulary.json").write_text(
            json.dumps({"frozen_on": "2026-08-01", "tags": tags}),
            encoding="utf-8")

    def make_loop(self, title: str, *, tags: list[str] | None = None,
                  theme: str = "free prose about the theme") -> "V.Loop":
        loop = V.create_loop(
            title, "[[sources/transcripts/2026/08/2026-08-05--x]]",
            dc.as_date("2026-08-05"), tags=tags or [])
        # A body with a section beyond the default, so byte-identity is a real
        # claim rather than trivially true of a regenerated default body.
        loop.body = V.default_body(loop) + f"\n## Theme\n\n{theme}\n"
        loop.save()
        return loop

    @staticmethod
    def body_of(path: Path) -> str:
        """The page body: everything after the closing frontmatter fence."""
        return path.read_text(encoding="utf-8").split("---\n", 2)[2]

    def payload(self, *entries: dict) -> dict:
        return {"loops": list(entries)}

    # -- tests -------------------------------------------------------------

    def test_valid_tags_land_and_body_is_byte_identical(self) -> None:
        self.freeze_vocab(["topic-a", "topic-b"])
        loop = self.make_loop("Does sleep tracking help?")
        body_before = self.body_of(loop.path)
        stats = AT.apply_tags(self.payload(
            {"id": loop.id, "tags": ["topic-a", "topic-b"]}))
        self.assertEqual(stats["tagged"], 1)
        fm, _ = dc.read_page(loop.path)
        self.assertEqual(fm["tags"], ["topic-a", "topic-b"])
        self.assertEqual(self.body_of(loop.path), body_before,
                         "tag backfill must be a frontmatter-only change")

    def test_already_tagged_loop_is_zero_diff(self) -> None:
        self.freeze_vocab(["topic-a", "topic-b"])
        loop = self.make_loop("Already handled?", tags=["topic-a"])
        raw_before = loop.path.read_bytes()
        stats = AT.apply_tags(self.payload(
            {"id": loop.id, "tags": ["topic-b"]}))
        self.assertEqual(stats["skipped_already_tagged"], 1)
        self.assertEqual(stats["tagged"], 0)
        self.assertEqual(loop.path.read_bytes(), raw_before,
                         "an already-tagged loop must show a zero diff")

    def test_out_of_vocab_tag_dropped_batch_proceeds(self) -> None:
        self.freeze_vocab(["topic-a", "topic-b"])
        a = self.make_loop("First loop?")
        b = self.make_loop("Second loop?")
        stats = AT.apply_tags(self.payload(
            {"id": a.id, "tags": ["topic-a", "made-up-tag"]},
            {"id": b.id, "tags": ["topic-b"]}))
        self.assertEqual(stats["tagged"], 2)
        self.assertEqual(len(stats["tags_dropped"]), 1)
        self.assertIn("made-up-tag", stats["tags_dropped"][0])
        fm, _ = dc.read_page(a.path)
        self.assertEqual(fm["tags"], ["topic-a"],
                         "invalid tag dropped, valid one kept")
        fm, _ = dc.read_page(b.path)
        self.assertEqual(fm["tags"], ["topic-b"])

    def test_unknown_loop_id_skipped_with_stat(self) -> None:
        self.freeze_vocab(["topic-a"])
        loop = self.make_loop("A real loop?")
        stats = AT.apply_tags(self.payload(
            {"id": "L9999", "tags": ["topic-a"]},
            {"id": loop.id, "tags": ["topic-a"]}))
        self.assertEqual(stats["skipped_unknown_id"], 1)
        self.assertEqual(stats["tagged"], 1)

    def test_all_tags_invalid_counts_empty_and_page_untouched(self) -> None:
        self.freeze_vocab(["topic-a"])
        loop = self.make_loop("Nothing fits?")
        raw_before = loop.path.read_bytes()
        stats = AT.apply_tags(self.payload(
            {"id": loop.id, "tags": ["invented"]}))
        self.assertEqual(stats["empty"], 1)
        self.assertEqual(stats["tagged"], 0)
        self.assertEqual(len(stats["tags_dropped"]), 1)
        self.assertEqual(loop.path.read_bytes(), raw_before)

    def test_zero_tags_is_a_valid_answer(self) -> None:
        self.freeze_vocab(["topic-a"])
        loop = self.make_loop("Genuinely untaggable?")
        raw_before = loop.path.read_bytes()
        stats = AT.apply_tags(self.payload({"id": loop.id, "tags": []}))
        self.assertEqual(stats["empty"], 1)
        self.assertEqual(loop.path.read_bytes(), raw_before)

    def test_missing_vocabulary_is_a_hard_error_no_pages_modified(self) -> None:
        # No freeze_vocab: the pre-freeze state that the nightly applier
        # tolerates is fatal for the backfill.
        loop = self.make_loop("Backfill without a vocabulary?")
        raw_before = loop.path.read_bytes()
        with self.assertRaises(SystemExit):
            AT.apply_tags(self.payload({"id": loop.id, "tags": ["topic-a"]}))
        self.assertEqual(loop.path.read_bytes(), raw_before)

    def test_malformed_json_fails_loudly_no_pages_modified(self) -> None:
        self.freeze_vocab(["topic-a"])
        loop = self.make_loop("Survives garbage input?")
        raw_before = loop.path.read_bytes()
        for garbage in ("", "no json here", '{"loops": [truncated',
                        '["a", "list", "not", "an", "object"]',
                        '{"wrong_key": []}'):
            with self.assertRaises(ValueError, msg=f"input: {garbage!r}"):
                AT.parse_payload(garbage)
        self.assertEqual(loop.path.read_bytes(), raw_before)

    def test_fenced_json_is_tolerated(self) -> None:
        # The house pattern: the contract says bare JSON, the model fences it
        # anyway (observed live on the first cc-ingest session).
        obj = AT.parse_payload(
            'Sure!\n```json\n{"loops": [{"id": "L0001", "tags": []}]}\n```')
        self.assertEqual(obj["loops"][0]["id"], "L0001")


if __name__ == "__main__":
    unittest.main()
