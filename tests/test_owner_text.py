#!/usr/bin/env python3
"""Unit tests for the owner-text reader. Fixtures are synthetic."""

from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import owner_text as OT  # noqa: E402

MAX = dc.CFG["frontier"]["owner_turn_max_chars"]

TRANSCRIPT = """# Session
meta line

## Human
Decision: mix the first glaze with fifty grams of frit per bucket.

Second paragraph of my words.

## Assistant
Here is the plan.

## The Plan Heading
This heading belongs to the assistant turn.

## Human (reconstructed)
The owner asked about soak schedules.

## Assistant (reconstructed)
A summary of soaking.
"""


class OwnerTextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text: str, name: str = "t.md") -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    # turns ---------------------------------------------------------------
    def test_assistant_heading_does_not_split_turn(self):
        ts = OT.turns(self.write(TRANSCRIPT))
        self.assertEqual([t["role"] for t in ts],
                         ["owner", "derived", "owner-reconstructed", "derived"])
        self.assertIn("## The Plan Heading", ts[1]["text"])
        self.assertIn("This heading belongs to the assistant turn.", ts[1]["text"])

    def test_turn_carries_line_span(self):
        ts = OT.turns(self.write(TRANSCRIPT))
        self.assertEqual(ts[0]["start_line"], 4)  # the "## Human" heading, 1-based

    def test_no_markers_yields_no_turns(self):
        self.assertEqual(OT.turns(self.write("just a note\n")), [])

    # owner_text ----------------------------------------------------------
    def test_owner_text_excludes_assistant_and_reconstructed_by_default(self):
        out = OT.owner_text(self.write(TRANSCRIPT))
        self.assertIn("fifty grams of frit per bucket", out)
        self.assertNotIn("Here is the plan", out)
        self.assertNotIn("soaking", out)

    def test_owner_text_can_include_reconstructed(self):
        out = OT.owner_text(self.write(TRANSCRIPT), include_reconstructed=True)
        self.assertIn("soak schedules", out)

    def test_skip_turns_drops_leading_turns_of_any_role(self):
        path = self.write(TRANSCRIPT)
        self.assertNotIn("fifty grams", OT.owner_text(path, skip_turns=1,
                                                         include_reconstructed=True))
        self.assertIn("soak schedules", OT.owner_text(path, skip_turns=1,
                                                          include_reconstructed=True))
        self.assertEqual(OT.owner_text(path, skip_turns=4, include_reconstructed=True), "")

    def test_fences_and_quoted_lines_removed(self):
        t = ("## Human\nmy words\n```\npasted code\n```\n> quoted email line\n"
             "more of my words\n```\nunterminated paste\n")
        out = OT.owner_text(self.write(t))
        self.assertIn("my words", out)
        self.assertIn("more of my words", out)
        self.assertNotIn("pasted code", out)
        self.assertNotIn("quoted email", out)
        self.assertNotIn("unterminated paste", out)

    def test_long_turn_keeps_head_and_tail_with_marker(self):
        body = "HEADWORDS " + "x" * (MAX * 3) + " TAILWORDS"
        out = OT.owner_text(self.write(f"## Human\n{body}\n"))
        self.assertIn("HEADWORDS", out)
        self.assertIn("TAILWORDS", out)
        self.assertIn(OT.CUT_MARKER, out)
        self.assertLess(len(out), MAX + 200)

    def test_short_turn_untouched(self):
        out = OT.owner_text(self.write("## Human\nshort\n"))
        self.assertNotIn(OT.CUT_MARKER, out)

    def test_new_turns_only_honours_divider(self):
        t = ("## Human\nold words\n\n## Assistant\nold reply\n\n"
             "> **[NEW TURNS START HERE — from message 4. Everything above "
             "was already extracted.]**\n\n## Human\nnew words\n")
        path = self.write(t)
        self.assertIn("old words", OT.owner_text(path))
        out = OT.owner_text(path, new_turns_only=True)
        self.assertIn("new words", out)
        self.assertNotIn("old words", out)

    def test_new_turns_only_without_divider_returns_everything(self):
        out = OT.owner_text(self.write("## Human\nall words\n"), new_turns_only=True)
        self.assertIn("all words", out)

    # find_owner_quote ----------------------------------------------------
    def test_quote_found_in_human_turn(self):
        hit = OT.find_owner_quote(self.write(TRANSCRIPT), "mix the first glaze with   fifty grams of frit per bucket")
        self.assertEqual(hit["reconstructed"], False)
        self.assertIn("Second paragraph", hit["turn"])

    def test_quote_not_found(self):
        self.assertIsNone(OT.find_owner_quote(self.write(TRANSCRIPT), "nothing like this"))

    def test_quote_only_in_assistant_turn_returns_none(self):
        self.assertIsNone(OT.find_owner_quote(self.write(TRANSCRIPT), "Here is the plan"))
        self.assertIsNone(OT.find_owner_quote(self.write(TRANSCRIPT), "A summary of soaking"))

    def test_quote_only_in_reconstructed_turn_is_flagged(self):
        hit = OT.find_owner_quote(self.write(TRANSCRIPT), "asked about soak schedules")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["reconstructed"])

    def test_quote_inside_pasted_material_is_not_owner_words(self):
        t = "## Human\nmine\n```\npasted glaze label text here\n```\n> quoted line text\n"
        path = self.write(t)
        self.assertIsNone(OT.find_owner_quote(path, "pasted glaze label text here"))
        self.assertIsNone(OT.find_owner_quote(path, "quoted line text"))

    def test_verbatim_match_preferred_over_reconstructed(self):
        t = ("## Human (reconstructed)\nwe talked about kiln shelves\n\n"
             "## Human\nI said kiln shelves matter\n")
        hit = OT.find_owner_quote(self.write(t), "kiln shelves")
        self.assertFalse(hit["reconstructed"])

    def long_turn(self):
        head = "HEADLINE Rule: at most three glaze tests per firing. "
        middle = "MIDDLE pasted without a fence so nobody can say these are my words at all. "
        tail = "TAILEND I will stop there and review again at the end of the quarter."
        # the cap keeps the first and last MAX/2 characters: the paste sits well inside the cut
        body = head + "filler " * 300 + middle + "padding " * 700 + tail
        return self.write(f"## Human\n{body}\n\n## Assistant\nok\n"), head, middle, tail

    def test_a_quote_from_the_cut_middle_of_a_long_turn_is_not_owner_words(self):
        # Owner words are trimmed AND capped; an unfenced paste hides in the middle
        path, head, middle, tail = self.long_turn()
        self.assertIsNone(OT.find_owner_quote(path, "MIDDLE pasted without a fence so nobody can say"))

    def test_a_quote_from_the_head_or_tail_of_a_long_turn_still_verifies(self):
        path, head, middle, tail = self.long_turn()
        for quote in ("at most three glaze tests per firing", "I will stop there and review again"):
            hit = OT.find_owner_quote(path, quote)
            self.assertIsNotNone(hit, quote)
            self.assertIn(OT.CUT_MARKER, hit["turn"])
            self.assertLessEqual(len(hit["turn"]), MAX + len(OT.CUT_MARKER) + 4)
            self.assertNotIn("MIDDLE", hit["turn"])

    def test_empty_quote_returns_none(self):
        self.assertIsNone(OT.find_owner_quote(self.write(TRANSCRIPT), "   "))

    # passages ------------------------------------------------------------
    def test_passage_centres_on_line_and_carries_role(self):
        path = self.write(TRANSCRIPT)
        lines = TRANSCRIPT.splitlines()
        n = next(i for i, l in enumerate(lines, 1) if l.startswith("Second paragraph"))
        out = OT.passages(path, [n], 10_000)
        self.assertTrue(out)
        centre = [p for p in out if "Second paragraph" in p["text"]]
        self.assertEqual(centre[0]["role"], "owner")
        self.assertEqual(centre[0]["line"], n)

    def test_passage_in_assistant_turn_is_derived(self):
        path = self.write(TRANSCRIPT)
        lines = TRANSCRIPT.splitlines()
        n = next(i for i, l in enumerate(lines, 1) if "belongs to the assistant" in l)
        out = OT.passages(path, [n], 10_000)
        self.assertEqual({p["role"] for p in out if "belongs to" in p["text"]}, {"derived"})

    def test_passage_in_reconstructed_turn_labelled(self):
        path = self.write(TRANSCRIPT)
        lines = TRANSCRIPT.splitlines()
        n = next(i for i, l in enumerate(lines, 1) if "owner asked about" in l)
        out = OT.passages(path, [n], 10_000)
        self.assertEqual(out[0]["role"] if "owner asked" in out[0]["text"] else None,
                         "owner-reconstructed")

    def test_passages_respect_limit_chars(self):
        paras = "\n\n".join(f"paragraph {i} " + "w" * 200 for i in range(20))
        path = self.write(f"## Human\n{paras}\n")
        out = OT.passages(path, [3, 20, 40], 500)
        self.assertLessEqual(sum(len(p["text"]) for p in out), 500)
        self.assertTrue(out)

    def test_a_hit_paragraph_too_long_for_the_budget_is_cut_around_the_terms(self):
        # The matched sentence sits thousands of characters into a long spoken paragraph;
        # a head cut would hide it.
        para = "clay and water " * 150 + "Studio requirements: it is going to be handbuilding. " + "z" * 900
        path = self.write(f"## Human\n{para}\n")
        out = OT.passages(path, [2], 500, terms=["Handbuilding", "versus", "outside", "wheel"])
        self.assertIn("going to be handbuilding", out[0]["text"])
        self.assertTrue(out[0]["text"].startswith(OT.ELLIPSIS), "the cut is marked")
        self.assertLessEqual(sum(len(p["text"]) for p in out), 500)

    def test_centre_paragraphs_win_over_neighbours_on_tight_budget(self):
        paras = ["alpha " * 5, "beta " * 5, "gamma " * 5, "delta " * 5, "epsilon " * 5]
        path = self.write("## Human\n" + "\n\n".join(paras) + "\n")
        lines = path.read_text().splitlines()
        n1 = next(i for i, l in enumerate(lines, 1) if l.startswith("alpha"))
        n2 = next(i for i, l in enumerate(lines, 1) if l.startswith("epsilon"))
        out = OT.passages(path, [n1, n2], 70)
        texts = " ".join(p["text"] for p in out)
        self.assertIn("alpha", texts)
        self.assertIn("epsilon", texts)

    def test_term_overlap_orders_leftover_neighbours(self):
        t = "## Human\n" + "\n\n".join(["noise one", "cheapneighbour", "CENTRE here", "soak ramp terms", "noise two"]) + "\n"
        path = self.write(t)
        lines = t.splitlines()
        n = next(i for i, l in enumerate(lines, 1) if l.startswith("CENTRE"))
        # budget fits the centre plus exactly one neighbour
        out = OT.passages(path, [n], len("CENTRE here") + len("soak ramp terms"),
                          terms=["soak"])
        texts = [p["text"] for p in out]
        self.assertIn("soak ramp terms", texts)
        self.assertNotIn("cheapneighbour", texts)

    def test_duplicate_hits_do_not_duplicate_passages(self):
        path = self.write("## Human\none\n\ntwo\n")
        out = OT.passages(path, [2, 2, 4], 10_000)
        self.assertEqual(len({p["line"] for p in out}), len(out))

    def test_out_of_range_lines_ignored(self):
        path = self.write("## Human\none\n")
        self.assertEqual(OT.passages(path, [0, 999], 1000), [])

    def test_line_in_preamble_is_derived(self):
        out = OT.passages(self.write(TRANSCRIPT), [2], 10_000)
        self.assertEqual(out[0]["role"], "derived")


TITLE = "Fire a test tile before the full load"
SUMMARY = "Fire one sample to confirm the glaze holds before stacking the kiln."


def chat(*turns: tuple[str, str]) -> str:
    return "# t\n\n" + "".join(f"## {role}\n\n{text}\n\n" for role, text in turns)


class OwnerPassagesTest(unittest.TestCase):
    """owner_passages: the owner's words a hit in an assistant turn would otherwise hide.
    qmd's best line usually falls in a long assistant reply, and passages() stays in
    that turn, so the check saw only derived text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(dc.CFG["frontier"], {
            "check_owner_chars": 2000, "check_owner_paragraphs": 3, "check_owner_paragraph_chars": 600,
            "check_owner_min_score": 2, "check_owner_title_weight": 2})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text: str) -> Path:
        path = Path(self.tmp.name) / "t.md"
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def line_of(path: Path, needle: str) -> int:
        return next(i for i, l in enumerate(path.read_text().splitlines(), 1) if needle in l)

    def test_the_question_an_assistant_hit_answers_is_shown(self):
        path = self.write(chat(("Human", "Would a quick sample firing tell me anything?"),
                               ("Assistant", "A test tile asks for a sample first.")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        self.assertEqual([(p["role"], p["text"]) for p in out],
                         [("owner", "Would a quick sample firing tell me anything?")])

    def test_a_matching_owner_paragraph_elsewhere_is_shown_and_noise_is_not(self):
        path = self.write(chat(("Human", "Notes about the herb planter."),
                               ("Assistant", "A test tile asks for a sample first."),
                               ("Human", "Unrelated: what about tomatoes?"),
                               ("Assistant", "Tomatoes like sun."),
                               ("Human", "Fire a test tile before the full load goes into the kiln.")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        texts = [p["text"] for p in out]
        self.assertIn("Fire a test tile before the full load goes into the kiln.", texts)
        self.assertIn("Notes about the herb planter.", texts, "the question before the hit")
        self.assertNotIn("Unrelated: what about tomatoes?", texts)
        self.assertEqual([p["line"] for p in out], sorted(p["line"] for p in out), "file order")

    def test_a_weak_match_is_not_shown(self):
        # one summary word ("glaze") scores 1, below check_owner_min_score
        path = self.write(chat(("Human", "Opening question."), ("Assistant", "A test tile."),
                               ("Human", "The glaze bucket on the shelf is empty.")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        self.assertNotIn("The glaze bucket on the shelf is empty.", [p["text"] for p in out])

    def test_pasted_material_is_never_owner_words(self):
        path = self.write(chat(("Human", "Opening question."), ("Assistant", "A test tile."),
                               ("Human", "```\nfire a test tile before loading\n```\n\n"
                                         "> fire a test tile before loading\n\nMy tomatoes.")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        self.assertFalse([p for p in out if "fire a test" in p["text"]])

    def test_a_long_paragraph_is_cut_around_the_match(self):
        filler = "lorem ipsum dolor sit amet " * 80
        path = self.write(chat(("Human", "Opening question."), ("Assistant", "A test tile."),
                               ("Human", f"{filler}I must fire a test tile before the full load goes in. {filler}")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        cut = next(p["text"] for p in out if "fire a test" in p["text"])
        self.assertIn("I must fire a test tile before the full load goes in.", cut)
        self.assertLessEqual(len(cut), 600 + 2 * len(OT.ELLIPSIS))
        self.assertTrue(cut.startswith(OT.ELLIPSIS) and cut.endswith(OT.ELLIPSIS))

    def test_a_long_paragraph_is_cut_where_the_candidates_words_cluster_not_at_the_first(self):
        # One title word early in a long spoken paragraph must not pull the window to the
        # head and cut off the owner's sentence on the concept, at the end. The window
        # goes where most of the candidate's words are.
        filler = "lorem ipsum dolor sit amet " * 40
        path = self.write(chat(("Human", "Opening question."), ("Assistant", "A test tile."),
                               ("Human", f"We should fire things. {filler}Before loading, I always "
                                         "fire a test tile and check the glaze holds.")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        cut = next(p["text"] for p in out if "lorem" in p["text"])
        self.assertIn("fire a test tile and check the glaze holds.", cut)

    def test_the_paragraph_count_and_budget_hold(self):
        many = [("Human", f"Fire a test tile before the full load, attempt {i}.") for i in range(8)]
        path = self.write(chat(("Human", "Opening question."), ("Assistant", "A test tile."), *many))
        with mock.patch.dict(dc.CFG["frontier"], {"check_owner_paragraphs": 2}):
            out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        self.assertEqual(len([p for p in out if "attempt" in p["text"]]), 2)
        with mock.patch.dict(dc.CFG["frontier"], {"check_owner_chars": 60}):
            out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        self.assertLessEqual(sum(len(p["text"]) for p in out), 60)

    def test_a_reconstructed_owner_turn_keeps_its_label(self):
        path = self.write(chat(("Human", "Opening question."), ("Assistant", "A test tile."),
                               ("Human (reconstructed)", "The owner wanted to fire a test tile before the full load.")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        self.assertIn(("owner-reconstructed", "The owner wanted to fire a test tile before the full load."),
                      [(p["role"], p["text"]) for p in out])

    def test_stopwords_alone_never_match(self):
        path = self.write(chat(("Human", "Opening question."), ("Assistant", "A test tile."),
                               ("Human", "And before that, there would be the other things.")))
        out = OT.owner_passages(path, [self.line_of(path, "test tile")], TITLE, SUMMARY)
        self.assertNotIn("And before that, there would be the other things.", [p["text"] for p in out])

    def test_a_file_without_turns_has_no_owner_passages(self):
        path = self.write("# Loop\n\nFire a test tile before the full load.\n")
        self.assertEqual(OT.owner_passages(path, [3], TITLE, SUMMARY), [])


if __name__ == "__main__":
    unittest.main()
