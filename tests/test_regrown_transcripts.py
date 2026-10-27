#!/usr/bin/env python3
"""A claude.ai chat that GROWS after it was extracted must be extracted again,
from its new turns only.

Before this, the converter rewrote a grown chat in place but extracted.json
still listed its path, so make_batch.select() never queued it and the new
turns were silently lost. Title changes moved the file as well, which broke
every occurrence wikilink pointing at the old path.

Everything runs against a temp vault; the real vault is never touched.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_extraction as AE  # noqa: E402
import convert_claude_export as cc  # noqa: E402
import dreamer_common as dc  # noqa: E402
import make_batch as mb  # noqa: E402
import questions as Q  # noqa: E402
import test_vault as TV  # noqa: E402
import vault as V  # noqa: E402
from test_converter import conversation  # noqa: E402

CREATED = "2026-07-14T10:00:00Z"
FOUR = [("human", "first"), ("assistant", "second"),
        ("human", "third"), ("assistant", "fourth")]
SIX = FOUR + [("human", "fifth"), ("assistant", "sixth")]
KEY = "sources/transcripts/2026/07/2026-07-14--a"


class RegrownCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-regrown-"))
        self.vault = self.tmp / "vault"
        self.out = self.vault / "sources" / "transcripts"
        self.meta = self.vault / ".vault-meta"
        for d in (self.out, self.meta):
            d.mkdir(parents=True)
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({"vault": str(self.vault), "sources": str(self.out),
                                "meta": str(self.meta)})
        self.n = 0

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def convert(self, msgs, *, name="A", updated="2026-07-14T11:00:00Z") -> dict:
        self.n += 1
        d = self.tmp / f"export{self.n}"
        d.mkdir()
        (d / "conversations.json").write_text(
            json.dumps([conversation("u1", name, CREATED, updated, msgs)]),
            encoding="utf-8")
        return cc.convert(d, self.out, None, False, True)

    def page(self, rel: str = "2026/07/2026-07-14--a.md"):
        return dc.parse_frontmatter((self.out / rel).read_text(encoding="utf-8"))

    def extracted(self) -> dict:
        return json.loads((self.meta / "extracted.json").read_text(encoding="utf-8"))

    def queued(self) -> list[str]:
        return [mb.rel_of(f) for f in mb.select(100)]


class GrownChatIsRequeuedTest(RegrownCase):
    def test_grown_chat_returns_to_queue_with_new_from_message(self) -> None:
        self.convert(FOUR[:2])
        mb.mark_extracted([KEY])
        self.assertEqual(self.queued(), [])
        self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        self.assertEqual(self.queued(), [KEY])
        fm, body = self.page()
        self.assertEqual(fm["new_from_message"], 2)
        self.assertEqual(fm["message_count"], 4)
        self.assertLess(body.index("NEW TURNS START HERE"), body.index("third"))
        self.assertGreater(body.index("NEW TURNS START HERE"), body.index("second"))

    def test_marker_is_not_a_turn_heading(self) -> None:
        self.convert(FOUR[:2])
        mb.mark_extracted([KEY])
        self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        _fm, body = self.page()
        self.assertEqual(len(re.findall(r"^## ", body, re.M)), 4)
        self.assertEqual(Q.extract_human_turns(body).count("NEW TURNS"), 0)

    def test_mark_extracted_records_message_count(self) -> None:
        self.convert(FOUR)
        mb.mark_extracted([KEY])
        data = self.extracted()
        self.assertEqual(data["done"], [KEY])
        self.assertEqual(data["extracted_through"], {KEY: 4})

    def test_metadata_only_update_stays_extracted(self) -> None:
        self.convert(FOUR)
        mb.mark_extracted([KEY])
        stats = self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        self.assertEqual(stats["replaced"], 1)
        self.assertEqual(self.queued(), [])
        fm, body = self.page()
        self.assertNotIn("new_from_message", fm)
        self.assertNotIn("NEW TURNS START HERE", body)

    def test_growing_twice_before_extraction_keeps_the_first_boundary(self) -> None:
        self.convert(FOUR[:2])
        mb.mark_extracted([KEY])
        self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        self.convert(SIX, updated="2026-07-14T13:00:00Z")
        fm, body = self.page()
        self.assertEqual(fm["new_from_message"], 2)
        self.assertEqual(body.count("NEW TURNS START HERE"), 1)
        self.assertLess(body.index("NEW TURNS START HERE"), body.index("third"))
        self.assertEqual(self.queued(), [KEY])

    def test_after_reextraction_the_next_growth_starts_from_the_new_boundary(self) -> None:
        self.convert(FOUR[:2])
        mb.mark_extracted([KEY])
        self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        mb.mark_extracted([KEY])
        self.assertEqual(self.extracted()["extracted_through"][KEY], 4)
        self.convert(SIX, updated="2026-07-14T13:00:00Z")
        self.assertEqual(self.page()[0]["new_from_message"], 4)

    def test_legacy_done_entry_falls_back_to_old_frontmatter_count(self) -> None:
        self.convert(FOUR[:2])
        (self.meta / "extracted.json").write_text(json.dumps({"done": [KEY]}),
                                                  encoding="utf-8")
        self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        self.assertEqual(self.queued(), [KEY])
        self.assertEqual(self.page()[0]["new_from_message"], 2)

    def test_never_extracted_chat_gets_no_marker(self) -> None:
        self.convert(FOUR[:2])
        self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        fm, body = self.page()
        self.assertNotIn("new_from_message", fm)
        self.assertNotIn("NEW TURNS START HERE", body)

    def test_new_messages_that_render_empty_do_not_requeue(self) -> None:
        self.convert(FOUR)
        mb.mark_extracted([KEY])
        self.convert(FOUR + [("human", "  ")], updated="2026-07-14T12:00:00Z")
        self.assertEqual(self.queued(), [])


class PathStabilityTest(RegrownCase):
    def test_title_change_keeps_the_path_and_the_done_entry(self) -> None:
        self.convert(FOUR[:2], name="A")
        mb.mark_extracted([KEY])
        self.convert(FOUR, name="Totally New Title", updated="2026-07-14T12:00:00Z")
        files = sorted(f.name for f in self.out.rglob("*.md"))
        self.assertEqual(files, ["2026-07-14--a.md"])
        fm, body = self.page()
        self.assertEqual(fm["title"], "Totally New Title")
        self.assertIn("# Totally New Title", body)
        self.assertIn(KEY, self.extracted()["done"] + self.queued())
        self.assertEqual(self.queued(), [KEY])

    def test_ledger_keeps_prior_path_and_stores_message_count(self) -> None:
        self.convert(FOUR[:2], name="A")
        self.convert(FOUR, name="B", updated="2026-07-14T12:00:00Z")
        entry = json.loads((self.meta / "ingested.json").read_text())["u1"]
        self.assertEqual(entry["path"], "2026/07/2026-07-14--a.md")
        self.assertEqual(entry["message_count"], 4)


class PromptAnnotationTest(RegrownCase):
    def test_continuation_is_tagged_and_plain_transcript_is_not(self) -> None:
        self.convert(FOUR[:2])
        mb.mark_extracted([KEY])
        self.convert(FOUR, updated="2026-07-14T12:00:00Z")
        prompt = mb.render_prompt(mb.select(10))
        self.assertIn("extract only from the marker", prompt)
        self.assertIn("were already extracted", prompt)
        self.assertIn("message 2", prompt)

        mb.mark_extracted([KEY])
        self.convert(FOUR, updated="2026-07-14T13:00:00Z")  # metadata only
        plain = mb.render_prompt([self.out / "2026/07/2026-07-14--a.md"])
        self.assertNotIn("extract only from the marker", plain)


class ExtractionPromptRuleTest(unittest.TestCase):
    def test_prompt_template_has_the_continuation_rule(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "skills" / "extract"
                / "PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("NEW TURNS START HERE", text)


class ContinuationOccurrenceTest(TV.VaultCase):
    """Re-extracting a grown chat may match a loop that already cites it."""

    def test_same_transcript_matched_again_adds_nothing(self) -> None:
        self.transcript("2026-07-14", "a")
        cand = {"title": "Should routing be intent-based or keyword-based?",
                "date": "2026-07-14",
                "transcript": "sources/transcripts/2026/07/2026-07-14--a",
                "match": {"decision": "new", "loop_id": None, "batch_ref": None,
                          "justification": "fixture"}}
        AE.apply_result({"candidates": [cand]})
        loop = V.load_loops()[0]
        self.assertEqual(loop.recurrence_count, 1)
        queue = dc.p("meta") / "fold-pending.json"
        before = queue.read_text() if queue.exists() else ""
        again = json.loads(json.dumps(cand))
        again["match"] = {"decision": "matched", "loop_id": loop.id,
                          "batch_ref": None, "justification": "continuation"}
        AE.apply_result({"candidates": [again]})
        loops = V.load_loops()
        self.assertEqual(len(loops), 1)
        self.assertEqual(len(loops[0].occurrences), 1)
        self.assertEqual(loops[0].recurrence_count, 1)
        self.assertEqual(queue.read_text() if queue.exists() else "", before,
                         "no second fold may be queued")


if __name__ == "__main__":
    unittest.main()
