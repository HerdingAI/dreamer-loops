#!/usr/bin/env python3
"""The solution spine (CLAUDE.md rule 5b)."""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import spine as SP  # noqa: E402
import vault as V  # noqa: E402

D = _dt.date.fromisoformat
OWNER_SAID = "I want no opaque database as the source of truth"
MARK = {"accepted": "✓ accepted", "provisional": "~ provisional",
        "contested": "! contested", "unsupported": "✗ unsupported"}


class SpineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-spine-"))
        names = ("loops", "conclusions", "concepts", "archive", "digests",
                 "meta", "vault", "goals")
        for n in names:
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({n: str(self.tmp / n) for n in names})
        dc.CFG["paths"]["sources"] = str(self.tmp / "vault" / "sources" / "transcripts")
        self._today = os.environ.get("DREAMER_TODAY")
        os.environ["DREAMER_TODAY"] = "2026-10-03"
        self._switch = mock.patch.dict(dc.CFG["solving"], {"enabled": True})
        self._switch.start()
        self.occ = self.transcript(
            "2026-09-01", "memory",
            f"## Human\n\n{OWNER_SAID}. I keep going back and forth.\n\n"
            "## Assistant\n\nYou should value raw speed above everything else.\n")
        self.loop = V.create_loop("File or database for agent memory?", self.occ,
                                  D("2026-09-01"))

    def tearDown(self) -> None:
        self._switch.stop()
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig)
        if self._today is None:
            os.environ.pop("DREAMER_TODAY", None)
        else:
            os.environ["DREAMER_TODAY"] = self._today
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, date: str, slug: str, body: str) -> str:
        rel = Path("sources/transcripts") / date[:4] / date[5:7] / f"{date}--{slug}.md"
        full = self.tmp / "vault" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(f"---\ntype: transcript\n---\n\n{body}", encoding="utf-8")
        return f"[[{rel.with_suffix('')}]]"

    def payload(self, ptype: str = "decision", **sections) -> dict:
        s = {
            "restated": "Whether agent memory lives in files or a database.",
            "owner_previously_concluded": [
                {"claim": "The owner rejected an opaque store.", "citation": self.occ,
                 "quote": OWNER_SAID, "support": "accepted"}],
            "define": {"current": "Memory is ad hoc.", "desired": "One durable store.",
                       "gap": "No store has been chosen.",
                       "why_it_matters": "Every session re-derives context."},
            "diagnosis": [{"claim": "The choice stalls on legibility.", "citation": self.occ,
                           "quote": OWNER_SAID, "support": "accepted", "basis": "known"}],
            "assumptions": ["Volume stays small."],
            "options": [{"name": "Markdown files", "description": "Plain pages."},
                        {"name": "SQLite", "description": "One database file."},
                        {"name": "Leave it", "description": "Keep the ad hoc notes."}],
            "evaluation": {
                "criteria": [{"criterion": "Readable by hand", "citation": self.occ,
                              "quote": OWNER_SAID}],
                "assessment": [
                    {"option": "Markdown files", "criterion": "Readable by hand",
                     "verdict": "meets", "note": "Opens in any editor."},
                    {"option": "SQLite", "criterion": "Readable by hand", "verdict": "fails"}],
                "premortem": "Files drift out of sync; the first sign is a stale index."},
            "recommended_option": "Markdown files",
            "synthesis": "Choose Markdown files and regenerate the index nightly.",
            "success_signal": "A month passes without re-deriving context by hand.",
            "open_sub_questions": ["Would volume past 10k pages change this? Measure it."],
        }
        s.update(sections)
        return {"route": "past-reasoning", "title": "Use Markdown files for agent memory",
                "confidence": "high", "problem_type": ptype, "sections": s}

    def check(self, payload: dict) -> tuple[dict | None, list[str]]:
        import apply_conclusion as AC
        problems: list[str] = []
        return SP.check(payload, problems, AC._claims, payload.get("route", ""),
                        occurrences=self.loop.occurrences,
                        is_derived=AC._is_derived), problems


class DepthTableTest(unittest.TestCase):
    def test_every_type_defines_every_stage(self) -> None:
        self.assertEqual(set(SP.DEPTH), set(SP.TYPES))
        for ptype in SP.TYPES:
            self.assertEqual(tuple(SP.DEPTH[ptype]), SP.STAGES, ptype)
            for depth in SP.DEPTH[ptype].values():
                self.assertIn(depth, ("full", "brief", None))

    def test_the_table_matches_rule_5b(self) -> None:
        self.assertEqual(SP.required("factual"), ["define", "recommend", "check"])
        self.assertEqual(SP.required("decision"),
                         ["define", "options", "evaluate", "recommend", "check"])
        self.assertEqual(SP.required("diagnostic"), list(SP.STAGES))
        self.assertEqual(SP.DEPTH["diagnostic"]["diagnose"], "full")
        self.assertEqual(SP.DEPTH["design"]["evaluate"], "full")
        self.assertEqual(SP.DEPTH["dilemma"]["evaluate"], "brief")
        self.assertEqual(SP.DEPTH["dilemma"]["check"], "full")

    def test_prompt_block_carries_the_table_and_the_option_minimum(self) -> None:
        block = SP.prompt_block()
        self.assertIn(SP.depth_table(), block)
        self.assertIn(f"at least {SP.min_options()}", block)
        self.assertIn('"problem_type"', block)
        self.assertIn("Step 1b", block)

    def test_a_missing_config_key_is_loud(self) -> None:
        with mock.patch.dict(dc.CFG, {"solving": {}}):
            with self.assertRaises(SystemExit):
                SP.min_options()


class CheckTest(SpineCase):
    def test_a_complete_decision_payload_is_clean(self) -> None:
        sp, problems = self.check(self.payload())
        self.assertEqual(problems, [])
        self.assertEqual((sp["type"], sp["missing"], sp["cap_low"], sp["provisional"]),
                         ("decision", [], False, False))
        self.assertEqual([c["criterion"] for c in sp["criteria"]], ["Readable by hand"])
        self.assertEqual(len(sp["assessment"]), 2)

    def test_no_type_is_legacy_and_silent(self) -> None:
        p = self.payload()
        del p["problem_type"]
        sp, problems = self.check(p)
        self.assertIsNone(sp)
        self.assertEqual(problems, [])

    def test_an_unknown_type_is_legacy_and_recorded(self) -> None:
        sp, problems = self.check(self.payload("riddle"))
        self.assertIsNone(sp)
        self.assertTrue(any("unknown problem_type" in p for p in problems))

    def test_a_missing_required_stage_caps_confidence(self) -> None:
        sp, problems = self.check(self.payload("diagnostic", diagnosis=[], assumptions=[]))
        self.assertEqual(sp["missing"], ["diagnose"])
        self.assertTrue(sp["cap_low"])
        self.assertTrue(any("'diagnose'" in p for p in problems))

    def test_an_uncited_diagnosis_claim_is_dropped(self) -> None:
        sp, problems = self.check(self.payload(
            "diagnostic", assumptions=[],
            diagnosis=[{"claim": "It is the cache.", "citation": "", "support": "accepted"}]))
        self.assertEqual(sp["diagnosis"], [])
        self.assertEqual(sp["missing"], ["diagnose"])
        self.assertTrue(any("uncited" in p for p in problems))

    def test_options_need_leave_it(self) -> None:
        sp, _ = self.check(self.payload(options=[
            {"name": "Markdown files", "description": ""},
            {"name": "SQLite", "description": ""}]))
        self.assertTrue(sp["cap_low"])

    def test_options_need_the_configured_minimum(self) -> None:
        sp, problems = self.check(self.payload(options=[
            {"name": "Markdown files", "description": ""},
            {"name": "Leave it", "description": ""}]))
        self.assertTrue(sp["cap_low"])
        self.assertTrue(any("option" in p for p in problems))

    def test_duplicate_options_count_once(self) -> None:
        sp, _ = self.check(self.payload(options=[
            {"name": "Markdown files"}, {"name": "markdown files"}, {"name": "Leave it"}]))
        self.assertEqual([o["name"] for o in sp["options"]], ["Markdown files", "Leave it"])
        self.assertTrue(sp["cap_low"])

    def test_assistant_turn_quote_is_not_a_criterion(self) -> None:
        sp, problems = self.check(self.payload(evaluation={
            "criteria": [{"criterion": "Raw speed", "citation": self.occ,
                          "quote": "You should value raw speed above everything else"}],
            "assessment": [], "premortem": "It fails slowly."}))
        self.assertEqual(sp["criteria"], [])
        self.assertTrue(sp["provisional"])
        self.assertTrue(sp["cap_low"])
        self.assertTrue(any("criterion[0] DROPPED" in p for p in problems))

    def test_an_invented_quote_is_not_a_criterion(self) -> None:
        sp, _ = self.check(self.payload(evaluation={
            "criteria": [{"criterion": "Cheap", "citation": self.occ,
                          "quote": "cost is all that matters to me"}],
            "assessment": [], "premortem": "x"}))
        self.assertEqual(sp["criteria"], [])

    def test_a_criterion_citing_a_missing_transcript_is_dropped(self) -> None:
        sp, _ = self.check(self.payload(evaluation={
            "criteria": [{"criterion": "Cheap", "quote": OWNER_SAID,
                          "citation": "[[sources/transcripts/2020/01/2020-01-01--nope]]"}],
            "assessment": [], "premortem": "x"}))
        self.assertEqual(sp["criteria"], [])

    def test_assessment_keeps_only_listed_options_and_surviving_criteria(self) -> None:
        p = self.payload()
        p["sections"]["evaluation"]["assessment"] += [
            {"option": "Postgres", "criterion": "Readable by hand", "verdict": "meets"},
            {"option": "SQLite", "criterion": "Fast", "verdict": "meets"},
            {"option": "SQLite", "criterion": "Readable by hand", "verdict": "superb"}]
        sp, _ = self.check(p)
        self.assertEqual(len(sp["assessment"]), 2)

    def test_recommendation_must_name_a_listed_option(self) -> None:
        for bad in ("Postgres", "", None, 7):
            sp, problems = self.check(self.payload(recommended_option=bad))
            self.assertTrue(sp["cap_low"], bad)
            self.assertEqual(sp["recommended"], "", bad)
            self.assertTrue(any("recommended_option" in p for p in problems), bad)

    def test_a_negated_mention_in_the_text_no_longer_counts(self) -> None:
        sp, _ = self.check(self.payload(recommended_option="",
                                        synthesis="Do not choose SQLite or Markdown files."))
        self.assertTrue(sp["cap_low"])

    def test_the_recommended_option_is_matched_to_its_listed_name(self) -> None:
        sp, problems = self.check(self.payload(recommended_option="markdown  FILES"))
        self.assertEqual((sp["recommended"], sp["cap_low"], problems), ("Markdown files", False, []))

    def test_a_full_evaluate_needs_a_premortem(self) -> None:
        p = self.payload()
        p["sections"]["evaluation"]["premortem"] = ""
        sp, problems = self.check(p)
        self.assertTrue(sp["cap_low"])
        self.assertTrue(any("premortem" in pr for pr in problems))

    def test_a_factual_loop_needs_only_three_stages(self) -> None:
        sp, problems = self.check(self.payload("factual"))
        self.assertEqual((sp["missing"], sp["cap_low"], sp["provisional"]), ([], False, False))
        self.assertEqual(problems, [])

    def test_a_dilemma_need_not_name_an_option(self) -> None:
        sp, _ = self.check(self.payload("dilemma", synthesis="Run a two-week trial of each."))
        self.assertFalse(sp["cap_low"])


class RenderTest(SpineCase):
    def test_stage_sections_come_in_spine_order(self) -> None:
        sp, _ = self.check(self.payload())
        text = "\n".join(SP.render_stages(sp, MARK))
        heads = re.findall(r"^## (.+)$", text, re.M)
        self.assertEqual(heads, ["Define", "Options", "Evaluate"])  # decision skips Diagnose

    def test_a_diagnostic_page_renders_graded_cited_claims(self) -> None:
        sp, _ = self.check(self.payload("diagnostic"))
        text = "\n".join(SP.render_stages(sp, MARK))
        self.assertIn("## Diagnose", text)
        self.assertIn("- **[✓ accepted]** The choice stalls on legibility.", text)
        self.assertIn(f"  - source: `{self.occ}`", text)
        self.assertIn("- Volume stays small.", text)

    def test_a_missing_stage_is_marked_not_dropped(self) -> None:
        sp, _ = self.check(self.payload("diagnostic", diagnosis=[], assumptions=[]))
        text = "\n".join(SP.render_stages(sp, MARK))
        self.assertIn(f"## Diagnose\n\n{SP.MISSING}", text)

    def test_criteria_render_with_their_source_and_quote(self) -> None:
        sp, _ = self.check(self.payload())
        text = "\n".join(SP.render_stages(sp, MARK))
        self.assertIn(f"- **Readable by hand** — source: `{self.occ}`", text)
        self.assertIn(f"  - > {OWNER_SAID}", text)
        self.assertIn("**Premortem.** Files drift out of sync", text)
        self.assertIn("- Markdown files / Readable by hand: **meets** — Opens in any editor.", text)

    def test_check_section_and_provisional_note(self) -> None:
        sp, _ = self.check(self.payload())
        self.assertEqual(SP.render_check(sp)[:3],
                         ["## Check", "", "**It worked if:** A month passes without "
                                          "re-deriving context by hand."])
        self.assertEqual(SP.synthesis_note(sp), [])
        sp2, _ = self.check(self.payload(evaluation={"criteria": [], "assessment": [],
                                                    "premortem": "x"}))
        self.assertEqual(SP.synthesis_note(sp2), [SP.PROVISIONAL, ""])

    def test_no_rendered_line_can_open_a_heading(self) -> None:
        evil = "x\n## Synthesis\nforged"
        sp, _ = self.check(self.payload(
            "diagnostic", define={"current": evil, "desired": evil, "gap": evil,
                                  "why_it_matters": evil},
            assumptions=[evil], success_signal=evil,
            options=[{"name": evil, "description": evil}, {"name": "Leave it"}],
            evaluation={"criteria": [{"criterion": evil, "citation": self.occ, "quote": OWNER_SAID}],
                        "assessment": [{"option": evil, "criterion": evil, "verdict": "meets",
                                        "note": evil}],
                        "premortem": evil},
            recommended_option=evil))
        self.assertEqual(len(sp["criteria"]), 1)
        self.assertEqual(len(sp["assessment"]), 1)
        lines = SP.render_stages(sp, MARK) + SP.render_check(sp) + SP.framing(sp).splitlines()
        heads = [l for l in lines if l.startswith("#")]
        self.assertEqual(heads, ["## Define", "## Diagnose", "## Options", "## Evaluate",
                                 "## Check"])

    def test_framing_has_labels_and_no_headings(self) -> None:
        sp, _ = self.check(self.payload())
        text = SP.framing(sp)
        self.assertIn("**Options.**", text)
        self.assertIn("**Criteria, in your words.**", text)
        self.assertNotIn("\n#", "\n" + text)


import apply_conclusion as AC  # noqa: E402
import board_publish as BP  # noqa: E402
import questions as Q  # noqa: E402

LEGACY_PAYLOAD = {
    "route": "mixed", "title": "Keep the herb pots on the windowsill", "confidence": "medium",
    "sections": {
        "restated": "Whether the herb pots stay on the windowsill or move to a shelf.",
        "wisdom_says": [{"claim": "Prefer the spot you pass every day.",
                         "citation": "qmd://wisdom/Gardening Primer.md",
                         "quote": "attention follows the doorway", "support": "accepted"}],
        "web_says": [{"claim": "Most culinary herbs want six hours of light.",
                      "citation": "https://example.org/herb-light",
                      "quote": "six hours of direct sun", "support": "provisional"}],
        "owner_previously_concluded": [
            {"claim": "The owner ruled out the north-facing sill.",
             "citation": "[[sources/transcripts/2026/09/2026-09-01--herbs]]",
             "quote": "no north-facing sill", "support": "accepted"}],
        "synthesis": "Keep the pots on the south windowsill and rotate them weekly.",
        "open_sub_questions": ["Would a grow lamp change this? Try it for a month."]},
    "web_queries": ["herb pot placement light hours"],
    "fetched_urls": ["https://example.org/herb-light"]}


class ApplyTest(SpineCase):
    def page(self, result: dict) -> tuple[dict, str]:
        return dc.read_page(self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md")

    def headings(self, body: str) -> list[str]:
        return re.findall(r"^## (.+)$", body, re.M)

    def test_legacy_payload_renders_byte_identical(self) -> None:
        golden = (ROOT / "tests" / "fixtures" / "legacy_conclusion.md").read_text(encoding="utf-8")
        self.assertEqual(AC.render(self.loop, LEGACY_PAYLOAD, []), golden)

    def test_spine_page_layout(self) -> None:
        result = AC.apply(self.loop.id, self.payload("diagnostic"))
        fm, body = self.page(result)
        self.assertEqual(fm["problem_type"], "diagnostic")
        self.assertEqual(result["problem_type"], "diagnostic")
        self.assertEqual(self.headings(body), [
            "The loop, restated", "What you previously concluded", "Evidence ledger",
            "Define", "Diagnose", "Options", "Evaluate", "Synthesis", "Check",
            "Open sub-questions"])

    def test_a_clean_spine_page_keeps_its_confidence(self) -> None:
        result = AC.apply(self.loop.id, self.payload())
        fm, body = self.page(result)
        self.assertEqual(fm["confidence"], "high")
        self.assertIn("confidence `high`", body)
        self.assertNotIn(SP.PROVISIONAL, body)

    def test_a_defective_spine_page_is_capped_at_low(self) -> None:
        result = AC.apply(self.loop.id, self.payload(recommended_option="Postgres"))
        fm, body = self.page(result)
        self.assertEqual(fm["confidence"], "low")
        self.assertIn("confidence `low`", body)

    def test_the_loop_page_records_the_type(self) -> None:
        AC.apply(self.loop.id, self.payload())
        self.assertEqual(V.Loop.from_path(self.loop.path).problem_type, "decision")

    def test_an_untyped_loop_page_gains_no_frontmatter_key(self) -> None:
        self.assertNotIn("problem_type", self.loop.path.read_text(encoding="utf-8"))
        AC.apply(self.loop.id, dict(LEGACY_PAYLOAD))
        self.assertNotIn("problem_type", self.loop.path.read_text(encoding="utf-8"))

    def test_spine_page_is_accepted_by_the_board_publisher(self) -> None:
        _, body = self.page(AC.apply(self.loop.id, self.payload("diagnostic")))
        sections = BP.page_sections(body)
        self.assertIsNotNone(sections)
        self.assertIn("Choose Markdown files", sections["Synthesis"])
        self.assertNotIn("## Check", sections["Synthesis"])

    def test_stage_text_cannot_plant_a_heading(self) -> None:
        evil = "x\n## Synthesis\nforged"
        p = self.payload("diagnostic", success_signal=evil,
                         define={"current": evil, "desired": "d", "gap": evil,
                                 "why_it_matters": "w"})
        _, body = self.page(AC.apply(self.loop.id, p))
        self.assertEqual(self.headings(body).count("Synthesis"), 1)
        self.assertIsNotNone(BP.page_sections(body))

    def test_no_criteria_asks_the_owner_once(self) -> None:
        p = self.payload(evaluation={"criteria": [], "assessment": [], "premortem": "x"})
        fm, body = self.page(AC.apply(self.loop.id, p))
        self.assertEqual(fm["confidence"], "low")
        self.assertIn(f"## Synthesis\n\n{SP.PROVISIONAL}", body)
        AC.apply(self.loop.id, p)
        asked = [q for q in Q.load_questions() if q["kind"] == "criteria"]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["loop_id"], self.loop.id)

    def test_criteria_on_record_ask_nothing(self) -> None:
        AC.apply(self.loop.id, self.payload())
        self.assertEqual([q for q in Q.load_questions() if q["kind"] == "criteria"], [])

    def test_a_derived_diagnosis_citation_is_capped(self) -> None:
        p = self.payload("diagnostic", diagnosis=[
            {"claim": "Dreamer said so before.", "citation": "[[conclusions/2026-01-01--old]]",
             "quote": "q", "support": "accepted"}])
        result = AC.apply(self.loop.id, p)
        _, body = self.page(result)
        self.assertIn("- **[! contested]** Dreamer said so before.", body)
        self.assertTrue(any("rule 13" in pr for pr in result["problems"]))

    def test_diagnosis_claims_are_counted_in_the_ledger(self) -> None:
        _, body = self.page(AC.apply(self.loop.id, self.payload("diagnostic")))
        ledger = re.search(r"^## Evidence ledger\n\n(.+)$", body, re.M).group(1)
        self.assertIn("✓ accepted: 2", ledger)

    def test_an_unknown_type_falls_back_to_the_legacy_page(self) -> None:
        result = AC.apply(self.loop.id, self.payload("riddle"))
        fm, body = self.page(result)
        self.assertNotIn("problem_type", fm)
        self.assertNotIn("## Define", body)
        self.assertIsNone(result["problem_type"])
        pending = dc.read_json(self.tmp / "digests" / "pending.json", default={})
        self.assertTrue(any("problem_type" in str(e.get("detail", ""))
                            for e in pending.get("events", [])))

    def test_the_digest_is_told_the_type_and_the_next_step(self) -> None:
        AC.apply(self.loop.id, self.payload())
        pending = dc.read_json(self.tmp / "digests" / "pending.json", default={})
        staged = pending["conclusions"][-1]
        self.assertEqual(staged["problem_type"], "decision")
        self.assertEqual(staged["next_step"],
                         "Choose Markdown files and regenerate the index nightly.")


class DecisionOnlyTest(SpineCase):
    def test_decision_only_gets_the_spine_shape_and_no_research(self) -> None:
        p = dict(self.payload(), route="decision-only")
        result = AC.apply(self.loop.id, p)
        self.assertEqual((result["status"], result["conclusion"]), ("decision-only", None))
        body = V.Loop.from_path(self.loop.path).body
        framing = body.split("## Decision framing", 1)[1]
        self.assertIn("**Options.** Markdown files — Plain pages.; SQLite", framing)
        self.assertIn("**Criteria, in your words.** Readable by hand", framing)
        self.assertEqual(re.findall(r"^## ", framing, re.M), [])
        self.assertEqual(list((self.tmp / "conclusions").glob("*.md")), [])

    def test_a_plain_framing_string_still_works(self) -> None:
        AC.apply(self.loop.id, {"route": "decision-only",
                                "decision_framing": "Only you can pick."})
        self.assertIn("Only you can pick.", V.Loop.from_path(self.loop.path).body)


class PromptTest(SpineCase):
    def prompt(self) -> str:
        import make_dream_prompt as MDP
        out = self.tmp / "prompt.md"
        argv = sys.argv
        sys.argv = ["make_dream_prompt", "--loop", self.loop.id, "--out", str(out)]
        try:
            self.assertEqual(MDP.main(), 0)
        finally:
            sys.argv = argv
        return out.read_text(encoding="utf-8")

    def test_switched_off_the_prompt_omits_the_spine_request(self) -> None:
        with mock.patch.dict(dc.CFG["solving"], {"enabled": False}):
            off = self.prompt()
        self.assertNotIn("Step 1b", off)
        with mock.patch.dict(dc.CFG["solving"], {"enabled": True}):
            on = self.prompt()
        self.assertEqual(off, on.replace("\n" + SP.prompt_block(), ""))

    def test_on_the_block_sits_before_the_loop_details(self) -> None:
        with mock.patch.dict(dc.CFG["solving"], {"enabled": True}):
            text = self.prompt()
        self.assertIn(SP.prompt_block(), text)
        self.assertLess(text.index("## Step 1b"), text.index("\n## The loop\n"))
        self.assertLess(text.index("## Step 3"), text.index("## Step 1b"))
        self.assertLess(text.index("\n## The loop\n"), text.index(f"`{self.loop.id}`"))


import digest as DG  # noqa: E402
import grade_conclusions as GC  # noqa: E402
import healthcheck as H  # noqa: E402

SPINE_CHECKS = {"spine_stages_complete", "criteria_owner_cited", "premortem_present",
                "recommendation_names_option"}


class GraderTest(SpineCase):
    def graded(self, payload: dict) -> dict:
        result = AC.apply(self.loop.id, payload)
        return GC.grade(self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md")

    def test_a_clean_spine_page_passes_all_four(self) -> None:
        self.assertEqual(SPINE_CHECKS & set(self.graded(self.payload())["failed"]), set())

    def test_a_missing_stage_fails_completeness(self) -> None:
        g = self.graded(self.payload("diagnostic", diagnosis=[], assumptions=[]))
        self.assertIn("spine_stages_complete", g["failed"])

    def test_no_owner_criteria_fails_the_criteria_check(self) -> None:
        g = self.graded(self.payload(evaluation={"criteria": [], "assessment": [],
                                                 "premortem": "x"}))
        self.assertIn("criteria_owner_cited", g["failed"])
        self.assertNotIn("premortem_present", g["failed"])

    def test_a_recommendation_naming_no_option_fails(self) -> None:
        g = self.graded(self.payload(recommended_option="Postgres"))
        self.assertIn("recommendation_names_option", g["failed"])

    def test_a_missing_premortem_fails(self) -> None:
        p = self.payload()
        p["sections"]["evaluation"]["premortem"] = ""
        self.assertIn("premortem_present", self.graded(p)["failed"])

    def test_a_factual_page_is_not_asked_for_criteria(self) -> None:
        g = self.graded(self.payload("factual"))
        self.assertEqual(SPINE_CHECKS & set(g["failed"]), set())

    def test_a_legacy_page_is_graded_exactly_as_before(self) -> None:
        dest = self.tmp / "conclusions" / "legacy.md"
        golden = (ROOT / "tests" / "fixtures" / "legacy_conclusion.md").read_text(encoding="utf-8")
        dc.write_page(dest, {"type": "conclusion", "loop": "L0001", "route": "mixed",
                             "confidence": "medium", "created": "2026-10-03"}, golden)
        self.assertEqual(SPINE_CHECKS & set(GC.grade(dest)["failed"]), set())
        self.assertEqual(GC.grade(dest)["score"],
                         round(1 - len(GC.grade(dest)["failed"]) / 12, 3))


class HealthTest(SpineCase):
    def test_registered_degraded_and_blocks_nothing(self) -> None:
        got = {name: (sev, blocks) for name, sev, blocks, _ in H.ASSERTIONS}
        self.assertEqual(got.get("spine-complete"), ("degraded", ()))

    def test_no_spine_pages_passes(self) -> None:
        ok, detail = H.assert_spine_complete()
        self.assertTrue(ok, detail)

    def test_a_written_spine_page_passes(self) -> None:
        AC.apply(self.loop.id, self.payload("diagnostic", diagnosis=[], assumptions=[]))
        ok, detail = H.assert_spine_complete()
        self.assertTrue(ok, detail)  # a stage honestly marked missing still has its heading

    def test_a_page_without_a_required_heading_fails(self) -> None:
        result = AC.apply(self.loop.id, self.payload())
        path = self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md"
        fm, body = dc.read_page(path)
        dc.write_page(path, fm, body.replace("## Options", "## Choices"))
        ok, detail = H.assert_spine_complete()
        self.assertFalse(ok)
        self.assertIn("options", detail)

    def test_an_unknown_type_on_a_page_fails(self) -> None:
        dc.write_page(self.tmp / "conclusions" / "x.md",
                      {"type": "conclusion", "problem_type": "riddle"}, "## Synthesis\n\nx\n")
        self.assertFalse(H.assert_spine_complete()[0])

    def test_a_superseded_page_is_not_judged(self) -> None:
        dc.write_page(self.tmp / "conclusions" / "x.md",
                      {"type": "conclusion", "problem_type": "riddle",
                       "superseded_by": "conclusions/y"}, "x\n")
        self.assertTrue(H.assert_spine_complete()[0])


class DigestLineTest(unittest.TestCase):
    def test_a_spine_conclusion_shows_its_type_and_next_step(self) -> None:
        lines = DG.conclusion_lines(
            {"path": "conclusions/a", "title": "T", "route": "mixed", "confidence": "low",
             "problem_type": "decision", "next_step": "Choose Markdown files."}, None)
        self.assertIn("type: `decision`", lines[0])
        self.assertEqual(lines[1], "  - next: Choose Markdown files.")

    def test_a_legacy_conclusion_line_is_unchanged(self) -> None:
        lines = DG.conclusion_lines(
            {"path": "conclusions/a", "title": "T", "route": "mixed", "confidence": "low"}, None)
        self.assertEqual(lines, ["- [[conclusions/a]] — **T** (route: `mixed`, confidence: low)"])


class ConstitutionTest(unittest.TestCase):
    def test_rule_5b_carries_the_depth_table_from_code(self) -> None:
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("### 5b. Problem type and the solution spine", text)
        self.assertIn(SP.depth_table(), text)
        for ptype in SP.TYPES:
            self.assertIn(f"- **`{ptype}`**", text)



class ReviewFixTest(SpineCase):
    """Regression cases: criteria provenance, prompt wording and escaping."""

    def criterion(self, citation: str, quote: str = OWNER_SAID) -> dict:
        return self.payload(evaluation={
            "criteria": [{"criterion": "Readable by hand", "citation": citation, "quote": quote}],
            "assessment": [], "premortem": "x"})

    def page(self, result: dict) -> tuple[dict, str]:
        return dc.read_page(self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md")

    # -- a criterion is the owner's: a transcript, a Human turn, a real quote --
    def test_a_criterion_citing_a_dreamer_page_is_dropped(self) -> None:
        derived = self.tmp / "vault" / "conclusions" / "old.md"
        derived.parent.mkdir(parents=True)
        derived.write_text(f"## Synthesis\n\n{OWNER_SAID}\n", encoding="utf-8")
        for cite in ("[[conclusions/old]]", "[[old]]"):
            sp, _ = self.check(self.criterion(cite))
            self.assertEqual(sp["criteria"], [], cite)
            self.assertTrue(sp["provisional"], cite)

    def test_a_criterion_citing_a_file_outside_the_vault_is_dropped(self) -> None:
        (self.tmp / "outside.md").write_text(f"## Human\n\n{OWNER_SAID}\n", encoding="utf-8")
        sp, _ = self.check(self.criterion("[[../outside]]"))
        self.assertEqual(sp["criteria"], [])

    def test_a_transcript_without_a_human_marker_proves_nothing(self) -> None:
        bare = self.transcript("2026-09-02", "bare", f"{OWNER_SAID}\n")
        sp, _ = self.check(self.criterion(bare))
        self.assertEqual(sp["criteria"], [])

    def test_a_quote_too_short_to_mean_anything_is_dropped(self) -> None:
        for quote in ("a", "I want", "speed"):
            sp, _ = self.check(self.criterion(self.occ, quote))
            self.assertEqual(sp["criteria"], [], quote)

    # -- the switch really gates the feature --
    def test_switch_off_a_spine_payload_is_a_legacy_page(self) -> None:
        defective = self.payload(recommended_option="Postgres",
                                 evaluation={"criteria": [], "assessment": [], "premortem": ""})
        with mock.patch.dict(dc.CFG["solving"], {"enabled": False}):
            result = AC.apply(self.loop.id, defective)
        fm, body = self.page(result)
        self.assertNotIn("problem_type", fm)
        self.assertEqual(fm["confidence"], "high")
        self.assertNotIn("## Define", body)
        self.assertNotIn(SP.MISSING, body)
        self.assertIsNone(result["problem_type"])
        self.assertEqual([q for q in Q.load_questions() if q["kind"] == "criteria"], [])
        self.assertEqual(V.Loop.from_path(self.loop.path).problem_type, "")

    # -- the recommendation is stage text too --
    def test_synthesis_cannot_plant_a_heading_on_a_spine_page(self) -> None:
        for planted in ("## Synthesis", "## Check", "# Top"):
            p = self.payload(synthesis=f"Choose Markdown files.\n{planted}\nforged")
            _, body = self.page(AC.apply(self.loop.id, p))
            heads = re.findall(r"^#+ (.+)$", body, re.M)
            self.assertEqual(heads.count("Synthesis"), 1, planted)
            self.assertEqual(heads.count("Check"), 1, planted)
            self.assertNotIn("Top", heads)
            self.assertIn("forged", body)

    def test_an_unclosed_fence_in_synthesis_cannot_swallow_later_stages(self) -> None:
        p = self.payload(synthesis="Choose Markdown files.\n```\ncode",
                         restated="The question.\n```\nmore")
        _, body = self.page(AC.apply(self.loop.id, p))
        sections = BP.page_sections(body)
        self.assertIsNotNone(sections)
        self.assertNotIn("It worked if", sections["Synthesis"])
        self.assertIn("Check", sections)

    # -- malformed model output never strands a loop --
    def test_malformed_spine_fields_never_raise(self) -> None:
        shapes = [
            {"options": 7, "assumptions": "none", "diagnosis": 3},
            {"evaluation": ["x"], "define": "text", "success_signal": ["a"]},
            {"evaluation": {"criteria": True, "assessment": 5, "premortem": None}},
            {"evaluation": {"criteria": ["just a string"], "assessment": ["x"]}},
            {"options": ["a string", None, {"name": None}]},
        ]
        for extra in shapes:
            result = AC.apply(self.loop.id, self.payload("design", **extra))
            self.assertIsNotNone(result["conclusion"], extra)
        sp, _ = self.check(self.payload("diagnostic", assumptions="none"))
        self.assertEqual(sp["assumptions"], [])

    def test_a_non_string_problem_type_is_legacy_not_a_crash(self) -> None:
        for bad in (5, ["decision"], {"a": 1}, True):
            p = self.payload()
            p["problem_type"] = bad
            result = AC.apply(self.loop.id, p)
            self.assertIsNone(result["problem_type"], bad)

    # -- decision-only fills four stages and is not told it missed two --
    def test_decision_only_is_not_asked_for_recommend_or_diagnose(self) -> None:
        for ptype in ("decision", "design", "dilemma"):
            p = dict(self.payload(ptype, synthesis="", diagnosis=[], assumptions=[]),
                     route="decision-only")
            result = AC.apply(self.loop.id, p)
            self.assertEqual([pr for pr in result["problems"] if "is missing" in pr], [], ptype)

    # -- a full Diagnose needs evidence, not only assumptions --
    def test_a_diagnostic_loop_needs_a_cited_cause(self) -> None:
        sp, _ = self.check(self.payload("diagnostic", diagnosis=[]))
        self.assertEqual(sp["missing"], ["diagnose"])
        sp, _ = self.check(self.payload("design", diagnosis=[]))  # brief: assumptions suffice
        self.assertEqual(sp["missing"], [])

    # -- the flat payload: less nesting for the model to get wrong --
    def flat(self) -> dict:
        p = self.payload()
        s = p["sections"]
        define, ev = s.pop("define"), s.pop("evaluation")
        s.update(current_state=define["current"], desired_state=define["desired"],
                 gap=define["gap"], why_it_matters=define["why_it_matters"],
                 criteria=ev["criteria"], assessment=ev["assessment"], premortem=ev["premortem"])
        self.assertIn("recommended_option", s)
        return p

    def test_the_flat_payload_is_equivalent_to_the_nested_one(self) -> None:
        nested, _ = self.check(self.payload())
        flat, problems = self.check(self.flat())
        self.assertEqual(problems, [])
        self.assertEqual(flat, nested)

    def test_the_prompt_asks_for_the_flat_payload(self) -> None:
        block = SP.prompt_block()
        for key in ('"current_state"', '"desired_state"', '"gap"', '"why_it_matters"',
                    '"criteria"', '"assessment"', '"premortem"', '"success_signal"'):
            self.assertIn(key, block)
        self.assertNotIn('"evaluation"', block)
        self.assertNotIn('"define"', block)
        self.assertNotIn("meets | partly | fails", block)
        self.assertIn('"verdict": "meets"', block)

    def test_the_prompt_puts_the_recommendation_first(self) -> None:
        # A synthesis that opens with a note about the route would make the
        # digest's "next:" line show the note, not the next step.
        block = " ".join(SP.prompt_block().split())
        self.assertIn("Open `synthesis` with the recommendation itself", block)

    def test_the_example_in_the_prompt_is_itself_valid(self) -> None:
        import json
        example = re.search(r"```json\n(.*?)\n```", SP.prompt_block(), re.S).group(1)
        fields = json.loads(example)
        fields["criteria"][0].update(citation=self.occ, quote=OWNER_SAID)
        fields["options"] += [{"name": "Other", "description": "x"}]
        self.assertEqual(fields["recommended_option"], "Short name")
        p = self.payload("design")
        p["sections"] = dict(restated="r", synthesis="Choose Short name.")
        p.update(fields)   # the example's fields sit at the top level
        sp, problems = self.check(p)
        self.assertEqual(problems, [])
        self.assertEqual(len(sp["assessment"]), 1)



class OwnerDecisionsTest(SpineCase):
    """Criteria come only from the loop's own conversations; the switch is on
    in the example config."""

    def page(self, result: dict) -> tuple[dict, str]:
        return dc.read_page(self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md")

    def with_criterion(self, citation: str, quote: str = OWNER_SAID, **over) -> dict:
        return self.payload(evaluation={
            "criteria": [{"criterion": "Readable by hand", "citation": citation, "quote": quote}],
            "assessment": [], "premortem": "x"}, **over)

    def test_the_switch_is_on(self) -> None:
        import yaml
        live = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        self.assertIs(live["solving"]["enabled"], True)

    # -- a criterion is about THIS choice --
    def test_a_criterion_from_another_conversation_is_dropped(self) -> None:
        elsewhere = self.transcript("2026-09-05", "garden",
                                    "## Human\n\nI prefer morning light to evening light.\n")
        result = AC.apply(self.loop.id, self.with_criterion(
            elsewhere, "I prefer morning light"))
        _, body = self.page(result)
        self.assertNotIn("morning light", body)
        self.assertIn(SP.PROVISIONAL, body)
        self.assertTrue(any("not one of this loop's conversations" in p
                            for p in result["problems"]))

    def test_an_anchor_or_alias_on_the_citation_still_resolves(self) -> None:
        stem = self.occ[2:-2]
        for cite in (f"[[{stem}#Human]]", f"[[{stem}|the memory chat]]", f"[[{stem}#Human|x]]"):
            sp, _ = self.check(self.with_criterion(cite))
            self.assertEqual(len(sp["criteria"]), 1, cite)

    def test_a_kept_criterion_is_cited_by_its_full_transcript_path(self) -> None:
        sp, _ = self.check(self.with_criterion("[[2026-09-01--memory]]"))
        self.assertEqual(sp["criteria"][0]["citation"], self.occ)
        g = GC.grade(self.tmp / "conclusions" / (Path(AC.apply(
            self.loop.id, self.with_criterion("[[2026-09-01--memory]]"))["conclusion"]).name + ".md"))
        self.assertNotIn("criteria_owner_cited", g["failed"])

    # -- minors --
    def test_leave_it_must_be_named_exactly(self) -> None:
        sp, _ = self.check(self.payload(options=[
            {"name": "Markdown files"}, {"name": "SQLite"}, {"name": "Leave it to the vendor"}]))
        self.assertTrue(sp["cap_low"])
        sp, _ = self.check(self.payload(options=[
            {"name": "Markdown files"}, {"name": "SQLite"}, {"name": "leave it."}]))
        self.assertFalse(sp["cap_low"])

    def test_the_cap_does_not_turn_an_undeclared_confidence_into_low(self) -> None:
        for conf in ("unknown", "", None, "certain"):
            p = self.payload(recommended_option="Postgres")
            p["confidence"] = conf
            fm, _ = self.page(AC.apply(self.loop.id, p))
            self.assertNotEqual(fm["confidence"], "low", conf)
        p = self.payload(recommended_option="Postgres")
        p["confidence"] = "medium"
        self.assertEqual(self.page(AC.apply(self.loop.id, p))[0]["confidence"], "low")

    def test_a_derived_diagnosis_claim_is_quarantined_not_left_in_diagnose(self) -> None:
        p = self.payload("design", diagnosis=[
            {"claim": "Dreamer said so before.", "citation": "[[conclusions/2026-01-01--old]]",
             "quote": "q", "support": "accepted"}])
        result = AC.apply(self.loop.id, p)
        _, body = self.page(result)
        diagnose = body.split("## Diagnose", 1)[1].split("\n## ", 1)[0]
        quarantine = body.split("## Prior conclusions (derived", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("Dreamer said so before.", diagnose)
        self.assertIn("- **[! contested]** Dreamer said so before.", quarantine)
        path = self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md"
        self.assertNotIn("no_derived_citations_as_evidence", GC.grade(path)["failed"])

    def test_a_derived_claim_alone_does_not_satisfy_a_full_diagnose(self) -> None:
        sp, _ = self.check(self.payload("diagnostic", assumptions=[], diagnosis=[
            {"claim": "Dreamer said so.", "citation": "[[loops/L0001]]", "support": "accepted"}]))
        self.assertEqual(sp["missing"], ["diagnose"])

    def test_a_later_legacy_conclusion_clears_the_recorded_type(self) -> None:
        AC.apply(self.loop.id, self.payload())
        self.assertEqual(V.Loop.from_path(self.loop.path).problem_type, "decision")
        AC.apply(self.loop.id, dict(LEGACY_PAYLOAD))
        self.assertEqual(V.Loop.from_path(self.loop.path).problem_type, "")

    def test_a_config_without_a_solving_block_means_off(self) -> None:
        saved = dc.CFG.pop("solving")
        try:
            self.assertFalse(SP.enabled())
        finally:
            dc.CFG["solving"] = saved

    def test_the_digest_is_told_about_defects_and_provisional_pages(self) -> None:
        AC.apply(self.loop.id, self.payload(
            recommended_option="Postgres",
            evaluation={"criteria": [], "assessment": [], "premortem": "x"},
            synthesis="word " * 200))
        staged = dc.read_json(self.tmp / "digests" / "pending.json", default={})["conclusions"][-1]
        self.assertTrue(staged["provisional"])
        self.assertEqual(len(staged["spine_defects"]), 2)
        self.assertLessEqual(len(staged["next_step"]), 240)
        lines = DG.conclusion_lines(staged, None)
        self.assertTrue(lines[1].startswith("  - next (provisional): word word"))
        self.assertTrue(lines[2].startswith("  - spine defects: "))
        self.assertIn("recommended_option", lines[2])

    def test_a_clean_page_reports_no_defects(self) -> None:
        AC.apply(self.loop.id, self.payload())
        staged = dc.read_json(self.tmp / "digests" / "pending.json", default={})["conclusions"][-1]
        self.assertEqual((staged["provisional"], staged["spine_defects"]), (False, []))
        self.assertEqual(len(DG.conclusion_lines(staged, None)), 2)

    def test_the_recommended_option_is_shown_and_graded(self) -> None:
        result = AC.apply(self.loop.id, self.payload())
        _, body = self.page(result)
        self.assertIn("**Recommended:** Markdown files", body.split("## Evaluate", 1)[1])
        path = self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md"
        self.assertNotIn("recommendation_names_option", GC.grade(path)["failed"])

    def test_the_prompt_states_the_new_rules(self) -> None:
        block = " ".join(SP.prompt_block().split())
        self.assertIn('"recommended_option"', block)
        self.assertIn("one of this loop's occurrences", block)



class LiveShapeTest(SpineCase):
    """A model may close `sections` early and write the stage fields at the
    top level; both shapes must be read."""

    STAGE_KEYS = ("current_state", "desired_state", "gap", "why_it_matters", "diagnosis",
                  "assumptions", "options", "criteria", "assessment", "premortem",
                  "recommended_option", "success_signal")

    def top_level(self) -> dict:
        p = self.payload()
        s = p["sections"]
        define, ev = s.pop("define"), s.pop("evaluation")
        s.update(current_state=define["current"], desired_state=define["desired"],
                 gap=define["gap"], why_it_matters=define["why_it_matters"], **ev)
        for key in self.STAGE_KEYS:
            p[key] = s.pop(key)
        return p

    def test_stage_fields_at_the_top_level_are_read(self) -> None:
        nested, _ = self.check(self.payload())
        top, problems = self.check(self.top_level())
        self.assertEqual(problems, [])
        self.assertEqual(top, nested)

    def test_the_type_is_fixed_before_research(self) -> None:
        # Once an answer is in hand every problem looks like one that had a
        # solution, so the type must be fixed from the title before research.
        block = " ".join(SP.prompt_block().split())
        self.assertIn("before you research", block)
        self.assertIn("do not change it afterwards", block)

    def test_the_prompt_asks_for_the_fields_at_the_top_level(self) -> None:
        block = " ".join(SP.prompt_block().split())
        self.assertIn('at the top level of the JSON object, next to `"sections"`', block)
        self.assertIn("not inside it", block)

    def test_a_reply_split_by_a_stray_brace_is_put_back_together(self) -> None:
        import json
        whole = self.top_level()
        whole.update(web_queries=["q one"], fetched_urls=["https://example.invalid/x"])
        head = {k: v for k, v in whole.items() if k not in ("web_queries", "fetched_urls")}
        raw = (json.dumps(head) + ',"web_queries":["q one"],'
               '"fetched_urls":["https://example.invalid/x"]}')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        payload, note = AC.salvage_split_reply(raw)
        self.assertEqual(payload, whole)
        self.assertIn("stray", note)

    def test_salvage_refuses_anything_it_does_not_understand(self) -> None:
        for raw in ("not json at all", '{"a": 1} trailing prose', '{"a": 1},{"b": 2}',
                    '{"a": 1}},"a": 2}', "", '[1, 2]}'):
            self.assertEqual(AC.salvage_split_reply(raw), (None, ""), raw)



class CriteriaAnswerTest(SpineCase):
    """A provisional conclusion waits on the owner. When they answer, the
    answer must come back as evidence and the loop must be researched again
    — otherwise the question is asked, answered, and nothing happens."""

    ANSWER = "it has to be readable by hand and cheap to back up"

    def provisional(self) -> dict:
        result = AC.apply(self.loop.id, self.payload(
            evaluation={"criteria": [], "assessment": [], "premortem": "x"}))
        # questions.py stamps `created_at` from the wall clock, not
        # DREAMER_TODAY. apply_criteria_answers only accepts a transcript
        # dated on/after that day, so with the real clock the fixtures'
        # 2026-10-05 conversations "pre-date" the question once the real date
        # passes it. Pin the stamp to the simulated day.
        questions = Q.load_questions()
        for q in questions:
            q["created_at"] = "2026-10-03T12:00:00"
        Q.save_questions(questions)
        return result

    def question(self) -> dict:
        return [q for q in Q.load_questions() if q["kind"] == "criteria"][0]

    def answer(self, quote: str | None = None) -> None:
        Q.answer_question(self.question()["id"], "Readable by hand, cheap to back up.",
                          owner_quote=quote or self.ANSWER, session_id="s9")

    def ingest_answer(self, date: str = "2026-10-05") -> str:
        return self.transcript(date, "answer",
                               f"## Human\n\nHonestly {self.ANSWER}, that is all.\n\n"
                               "## Assistant\n\nNoted.\n")

    def fresh(self) -> V.Loop:
        return V.Loop.from_path(self.loop.path)

    def test_a_provisional_conclusion_says_so_in_its_frontmatter(self) -> None:
        result = self.provisional()
        fm, _ = dc.read_page(self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md")
        self.assertIs(fm["provisional"], True)
        clean = AC.apply(self.loop.id, self.payload())
        fm, _ = dc.read_page(self.tmp / "conclusions" / f"{Path(clean['conclusion']).name}.md")
        self.assertNotIn("provisional", fm)

    def test_nothing_happens_until_the_answer_is_in_a_transcript(self) -> None:
        self.provisional()
        self.answer()
        self.assertEqual(Q.apply_criteria_answers(), {"applied": 0, "waiting": 1})
        self.assertEqual(self.fresh().status, "paused")
        self.assertEqual(self.fresh().reresearch_requested, "")

    def test_the_answer_reopens_the_loop_and_asks_for_research(self) -> None:
        self.provisional()
        self.answer()
        link = self.ingest_answer()
        self.assertEqual(Q.apply_criteria_answers(), {"applied": 1, "waiting": 0})
        loop = self.fresh()
        self.assertEqual(loop.status, "open")
        self.assertIn(link, loop.occurrences)
        self.assertEqual(loop.reresearch_requested, "criteria answered 2026-10-05")
        q = self.question()
        self.assertEqual(q["occurrence"], link)
        self.assertTrue(q["applied_at"])
        self.assertEqual(Q.apply_criteria_answers(), {"applied": 0, "waiting": 0})
        self.assertEqual(self.fresh().occurrences.count(link), 1)

    def test_the_request_beats_the_cooldown_and_only_the_request_does(self) -> None:
        self.provisional()
        ref = D("2026-10-05")
        blocked, reason = V.reresearch_gate(self.fresh(), ref)
        self.assertFalse(blocked)
        self.assertIn("cooldown", reason)
        self.answer()
        self.ingest_answer()
        Q.apply_criteria_answers()
        self.assertEqual(V.reresearch_gate(self.fresh(), ref), (True, ""))
        self.assertIn(self.loop.id, [l.id for l in V.select_for_research(ref=ref)])

    def test_the_answer_then_counts_as_a_criterion_and_clears_the_request(self) -> None:
        self.provisional()
        self.answer()
        link = self.ingest_answer()
        Q.apply_criteria_answers()
        result = AC.apply(self.loop.id, self.payload(evaluation={
            "criteria": [{"criterion": "Readable and cheap to back up", "citation": link,
                          "quote": self.ANSWER}],
            "assessment": [], "premortem": "x"}))
        fm, body = dc.read_page(self.tmp / "conclusions" / f"{Path(result['conclusion']).name}.md")
        self.assertNotIn("provisional", fm)
        self.assertNotIn(SP.PROVISIONAL, body)
        self.assertIn("- **Readable and cheap to back up** — source:", body)
        self.assertEqual(self.fresh().reresearch_requested, "")
        self.assertNotIn("reresearch_requested", self.loop.path.read_text(encoding="utf-8"))

    def test_a_served_conclusion_also_clears_the_request(self) -> None:
        self.provisional()
        self.answer()
        self.ingest_answer()
        Q.apply_criteria_answers()
        AC.apply(self.loop.id, {"action": "serve", "reason": "nothing new"})
        self.assertEqual(self.fresh().reresearch_requested, "")

    def test_the_prompt_tells_the_model_not_to_serve(self) -> None:
        import make_dream_prompt as MDP
        self.provisional()
        self.answer()
        self.ingest_answer()
        Q.apply_criteria_answers()
        out = self.tmp / "prompt.md"
        argv = sys.argv
        sys.argv = ["make_dream_prompt", "--loop", self.loop.id, "--out", str(out)]
        try:
            self.assertEqual(MDP.main(), 0)
        finally:
            sys.argv = argv
        text = out.read_text(encoding="utf-8")
        self.assertIn("criteria answered 2026-10-05", text)
        self.assertIn("Do not serve", text)

    def test_an_answer_is_never_matched_to_an_older_conversation(self) -> None:
        self.provisional()
        self.answer(quote=OWNER_SAID)   # words from the loop's own, older transcript
        self.assertEqual(Q.apply_criteria_answers(), {"applied": 0, "waiting": 1})
        self.assertEqual(self.fresh().reresearch_requested, "")

    def test_a_trivial_or_assistant_quote_attaches_nothing(self) -> None:
        self.provisional()
        self.ingest_answer()
        for quote in ("Noted.", "yes", "that"):
            self.answer(quote=quote)
            self.assertEqual(Q.apply_criteria_answers()["applied"], 0, quote)

    def test_other_questions_are_left_alone(self) -> None:
        other = Q.enqueue_question("drift", "Still circling this?", loop_id=self.loop.id)
        Q.answer_question(other["id"], "yes", owner_quote=self.ANSWER, session_id="s1")
        self.ingest_answer()
        self.assertEqual(Q.apply_criteria_answers(), {"applied": 0, "waiting": 0})

    def test_the_nightly_job_applies_criteria_answers(self) -> None:
        script = (ROOT / "bin" / "nightly-assess.sh").read_text(encoding="utf-8")
        self.assertIn("Q.apply_criteria_answers()", script)


if __name__ == "__main__":
    unittest.main()
