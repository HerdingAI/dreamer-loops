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
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import apply_extraction as AE  # noqa: E402
import dreamer_common as dc  # noqa: E402
import vault as V  # noqa: E402


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


class ApplyTagsTest(unittest.TestCase):
    """U6 — extractor-emitted tags on NEW loops (CLAUDE.md rules 1 and 4).

    The applier is the enforcement point: valid vocabulary tags land in the
    new loop's frontmatter, out-of-vocabulary tags are dropped with a logged
    reason (never failing the candidate), no vocabulary file means no tags at
    all, and occurrence-appends never retro-tag an existing loop.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-apply-tags-"))
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

    def transcript(self, date: str, slug: str) -> str:
        rel = Path("sources/transcripts") / date[:4] / date[5:7] / f"{date}--{slug}.md"
        full = self.tmp / "vault" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("---\ntype: transcript\n---\n\nbody\n", encoding="utf-8")
        return str(rel.with_suffix(""))

    def candidate(self, title: str, transcript: str, *,
                  tags: list[str] | None = None, **match) -> dict:
        return {
            "title": title, "transcript": transcript, "date": "2026-08-05",
            "theme_note": "free prose theme",
            **({"tags": tags} if tags is not None else {}),
            "match": {"decision": "new", "loop_id": None, "batch_ref": None,
                      "considered": [], "justification": "test", **match},
        }

    def loop_by_title(self, title: str) -> "V.Loop":
        for loop in V.load_loops(include_archived=True):
            if loop.title == title:
                return loop
        raise AssertionError(f"no loop titled {title!r}")

    # -- tests -------------------------------------------------------------

    def test_valid_tags_land_in_frontmatter(self) -> None:
        self.freeze_vocab(["topic-a", "note-taking"])
        t = self.transcript("2026-08-05", "sleep")
        stats = AE.apply_result({"candidates": [
            self.candidate("Does sleep tracking help?", t,
                           tags=["topic-a", "note-taking"])]})
        self.assertEqual(stats["created"], 1)
        loop = self.loop_by_title("Does sleep tracking help?")
        self.assertEqual(loop.tags, ["topic-a", "note-taking"])
        # And they survive on disk, not only on the in-memory object.
        fm, _ = dc.read_page(loop.path)
        self.assertEqual(fm["tags"], ["topic-a", "note-taking"])

    def test_out_of_vocabulary_tag_dropped_batch_proceeds(self) -> None:
        self.freeze_vocab(["topic-a"])
        t1 = self.transcript("2026-08-05", "diet")
        t2 = self.transcript("2026-08-06", "other")
        stats = AE.apply_result({"candidates": [
            self.candidate("Is the diet sustainable?", t1,
                           tags=["topic-a", "sleep-tracking"]),
            self.candidate("A second unrelated loop question?", t2),
        ]})
        self.assertEqual(stats["created"], 2, stats["problems"])
        self.assertEqual(stats["rejected"], 0)
        loop = self.loop_by_title("Is the diet sustainable?")
        self.assertEqual(loop.tags, ["topic-a"],
                         "invalid tag must be dropped, valid one kept")
        # The drop is logged as a stat, not buried: the digest can report it.
        self.assertEqual(len(stats["tags_dropped"]), 1)
        self.assertIn("sleep-tracking", stats["tags_dropped"][0])

    def test_no_vocabulary_file_means_no_tags_at_all(self) -> None:
        # Rule 4 pre-freeze state: even vocabulary-sounding tags must not be
        # written when no vocabulary file exists.
        t = self.transcript("2026-08-05", "prefreeze")
        stats = AE.apply_result({"candidates": [
            self.candidate("A loop from before the freeze?", t,
                           tags=["topic-a"])]})
        self.assertEqual(stats["created"], 1, stats["problems"])
        loop = self.loop_by_title("A loop from before the freeze?")
        self.assertEqual(loop.tags, [])
        self.assertEqual(len(stats["tags_dropped"]), 1)

    def test_occurrence_append_never_retro_tags(self) -> None:
        self.freeze_vocab(["topic-a"])
        t1 = self.transcript("2026-08-01", "orig")
        existing = V.create_loop("Does fasting actually work?",
                                 f"[[{t1}]]", dc.as_date("2026-08-01"))
        self.assertEqual(existing.tags, [])
        t2 = self.transcript("2026-08-06", "again")
        stats = AE.apply_result({"candidates": [
            self.candidate("Does fasting actually work?", t2,
                           tags=["topic-a"],
                           decision="matched", loop_id=existing.id)]})
        self.assertEqual(stats["matched"], 1, stats["problems"])
        loop = self.loop_by_title("Does fasting actually work?")
        self.assertEqual(loop.tags, [],
                         "matched candidates must not retro-tag existing loops")

    def test_broken_propose_tags_degrades_to_no_tags(self) -> None:
        """h4: a broken propose_tags module must cost the tags, never the
        candidate — same degradation contract _load_vocabulary declares."""
        import contextlib
        import io
        self.freeze_vocab(["topic-a"])
        t = self.transcript("2026-08-05", "brokenmod")
        saved = sys.modules.get("propose_tags")
        sys.modules["propose_tags"] = None  # `import propose_tags` now raises
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                stats = AE.apply_result({"candidates": [
                    self.candidate("Broken tag module loop?", t,
                                   tags=["topic-a"])]})
        finally:
            if saved is not None:
                sys.modules["propose_tags"] = saved
            else:
                sys.modules.pop("propose_tags", None)
        self.assertEqual(stats["created"], 1, stats["problems"])
        self.assertEqual(stats["rejected"], 0,
                         "an import failure must not reject the candidate")
        loop = self.loop_by_title("Broken tag module loop?")
        self.assertEqual(loop.tags, [], "degrade to no-tags")
        self.assertIn("no tags", err.getvalue(),
                      "the degradation reason must be logged")

    def test_dry_run_writes_nothing(self) -> None:
        self.freeze_vocab(["topic-a"])
        t = self.transcript("2026-08-05", "dry")
        AE.apply_result({"candidates": [
            self.candidate("A dry-run loop?", t, tags=["topic-a"])]},
            dry_run=True)
        self.assertEqual(V.load_loops(include_archived=True), [])


class VocabularyInjectionTest(unittest.TestCase):
    """U6 — render_prompt() injects the frozen vocabulary into the extract
    prompt, and injects nothing before the freeze."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-vocab-inject-"))
        (self.tmp / "meta").mkdir(parents=True)
        (self.tmp / "vault").mkdir(parents=True)
        self._orig_paths = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({"meta": str(self.tmp / "meta"),
                                "vault": str(self.tmp / "vault")})
        import make_batch
        self.MB = make_batch

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig_paths)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _batch(self) -> list[Path]:
        f = (self.tmp / "vault" / "sources" / "transcripts" / "2026" / "08"
             / "2026-08-05--x.md")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("---\ntype: transcript\n---\n\nbody\n", encoding="utf-8")
        return [f]

    def test_frozen_vocabulary_is_injected(self) -> None:
        (self.tmp / "meta" / "tag-vocabulary.json").write_text(
            json.dumps({"frozen_on": "2026-08-01",
                        "tags": ["topic-a", "topic-b"]}), encoding="utf-8")
        prompt = self.MB.render_prompt(self._batch())
        self.assertIn("## Approved tag vocabulary", prompt)
        self.assertIn("`topic-a`", prompt)
        self.assertIn("`topic-b`", prompt)

    def test_no_vocabulary_no_section(self) -> None:
        prompt = self.MB.render_prompt(self._batch())
        self.assertNotIn("## Approved tag vocabulary", prompt)


if __name__ == "__main__":
    unittest.main()
