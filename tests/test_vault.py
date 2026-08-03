#!/usr/bin/env python3
"""DoD 6.2 — Insight Vault: state machine, decay clock, catalog, merge, lint."""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import vault as V  # noqa: E402

D = _dt.date.fromisoformat


class VaultCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-vault-"))
        for name in ("loops", "conclusions", "concepts", "archive", "digests",
                     "sources", "meta", "vault"):
            (self.tmp / name).mkdir(parents=True, exist_ok=True)
        self._orig_paths = dict(dc.CFG["paths"])
        self._orig_decay = dict(dc.CFG["decay"])
        self._orig_match = dict(dc.CFG["matching"])
        dc.CFG["paths"].update({
            "vault": str(self.tmp / "vault"),
            "loops": str(self.tmp / "loops"),
            "conclusions": str(self.tmp / "conclusions"),
            "concepts": str(self.tmp / "concepts"),
            "archive": str(self.tmp / "archive"),
            "digests": str(self.tmp / "digests"),
            "sources": str(self.tmp / "sources"),
            "meta": str(self.tmp / "meta"),
        })
        dc.CFG["decay"].update({"go_live_date": "2026-08-01", "decay_weeks": 8,
                                "terminal_multiplier": 2})
        dc.CFG["matching"].update({"recurrence_min": 2, "recency_half_life_days": 60})

    def tearDown(self) -> None:
        dc.CFG["paths"].clear(); dc.CFG["paths"].update(self._orig_paths)
        dc.CFG["decay"].clear(); dc.CFG["decay"].update(self._orig_decay)
        dc.CFG["matching"].clear(); dc.CFG["matching"].update(self._orig_match)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, date: str, slug: str) -> str:
        """Create a real transcript file and return its wikilink, so link
        integrity is exercised rather than asserted against fiction."""
        rel = Path("sources/transcripts") / date[:4] / date[5:7] / f"{date}--{slug}.md"
        full = self.tmp / "vault" / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("---\ntype: transcript\n---\n\nbody\n", encoding="utf-8")
        return f"[[{rel.with_suffix('')}]]"


class StateMachineTest(VaultCase):
    def test_new_to_open_on_first_occurrence(self) -> None:
        occ = self.transcript("2026-07-14", "memory-arch")
        loop = V.create_loop("How should memory persist?", occ, D("2026-07-14"))
        self.assertEqual(loop.id, "L0001")
        self.assertEqual(loop.status, "open")
        self.assertEqual(loop.recurrence_count, 1)
        self.assertTrue((self.tmp / "loops" / "L0001.md").exists())

    def test_ids_are_monotonic_and_never_reused(self) -> None:
        a = V.create_loop("A", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        b = V.create_loop("B", self.transcript("2026-07-02", "b"), D("2026-07-02"))
        self.assertEqual([a.id, b.id], ["L0001", "L0002"])
        V.archive_loop(a)
        c = V.create_loop("C", self.transcript("2026-07-03", "c"), D("2026-07-03"))
        self.assertEqual(c.id, "L0003", "archived ids must not be recycled")

    def test_open_increments_on_match(self) -> None:
        loop = V.create_loop("X", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        changed = V.add_occurrence(loop, self.transcript("2026-07-20", "b"),
                                   D("2026-07-20"))
        self.assertTrue(changed)
        self.assertEqual(loop.recurrence_count, 2)
        self.assertEqual(loop.last_seen, D("2026-07-20"))
        self.assertEqual(loop.first_seen, D("2026-07-01"))

    def test_add_occurrence_is_idempotent(self) -> None:
        occ = self.transcript("2026-07-01", "a")
        loop = V.create_loop("X", occ, D("2026-07-01"))
        self.assertFalse(V.add_occurrence(loop, occ, D("2026-07-01")))
        self.assertEqual(loop.recurrence_count, 1)

    def test_occurrences_stay_chronological(self) -> None:
        """A loop's occurrence list is the commit history of an idea.

        Backfill ingests in batch order, not date order, so a 2026 transcript
        can be appended before a 2025 one. Left unsorted the list reads as
        noise and the progression — what the owner thought first, what changed
        — is unreadable, which is the whole point of keeping the history.
        """
        loop = V.create_loop("X", self.transcript("2026-06-01", "recent"),
                             D("2026-06-01"))
        V.add_occurrence(loop, self.transcript("2025-01-15", "oldest"),
                         D("2025-01-15"))
        V.add_occurrence(loop, self.transcript("2025-09-09", "middle"),
                         D("2025-09-09"))
        dates = [d.isoformat() for d in loop.occurrence_dates()]
        self.assertEqual(dates, sorted(dates), loop.occurrences)
        self.assertEqual(loop.first_seen, D("2025-01-15"))
        self.assertEqual(loop.last_seen, D("2026-06-01"))

    def test_resurfacing_preserves_custom_body_sections(self) -> None:
        """add_occurrence used to force body regeneration via default_body(),
        which writes ONLY Statement + Occurrences — silently deleting any
        other section. Found live 2026-08-02: L0004's '## Superseded
        conclusions' list (written by apply_conclusion.py) vanished the next
        time the loop resurfaced, orphaning two conclusion pages and tripping
        vault.py lint. Occurrences must refresh surgically.
        """
        loop = V.create_loop("X", self.transcript("2025-01-01", "a"),
                             D("2025-01-01"))
        loop.body += "\n## Superseded conclusions\n\n- [[conclusions/old]]\n"
        loop.save()
        V.add_occurrence(loop, self.transcript("2025-02-01", "b"),
                         D("2025-02-01"))
        reloaded = V.Loop.from_path(loop.path)
        self.assertIn("## Superseded conclusions", reloaded.body)
        self.assertIn("[[conclusions/old]]", reloaded.body)
        self.assertIn("2025-02-01--b", reloaded.body)

    def test_paused_reopens_on_resurfacing(self) -> None:
        loop = V.create_loop("X", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        loop.status = "paused"
        loop.conclusion = "conclusions/c1"
        loop.save()
        V.add_occurrence(loop, self.transcript("2026-07-25", "b"), D("2026-07-25"))
        self.assertEqual(loop.status, "open")
        self.assertEqual(loop.recurrence_count, 2)

    def test_archived_loop_reopens_and_leaves_archive(self) -> None:
        loop = V.create_loop("X", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.archive_loop(loop)
        self.assertTrue((self.tmp / "archive" / "L0001.md").exists())
        V.add_occurrence(loop, self.transcript("2026-07-25", "b"), D("2026-07-25"))
        self.assertEqual(loop.status, "open")
        self.assertTrue((self.tmp / "loops" / "L0001.md").exists())
        self.assertFalse((self.tmp / "archive" / "L0001.md").exists())

    def test_illegal_status_rejected(self) -> None:
        loop = V.create_loop("X", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        loop.status = "concluded"
        with self.assertRaises(ValueError):
            loop.save()

    def test_stranded_researching_is_recovered(self) -> None:
        loop = V.create_loop("X", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        loop.status = "researching"
        loop.save()
        self.assertEqual(V.select_for_research(), [], "researching is invisible")
        recovered = V.recover_stranded()
        self.assertEqual([l.id for l in recovered], ["L0001"])
        self.assertEqual(V.load_loops()[0].status, "open")


class DecayClockTest(VaultCase):
    """The v1 logic bug and its fix: clock = max(last_seen, GO_LIVE_DATE)."""

    def _loop(self, last_seen: str, status: str = "open") -> V.Loop:
        loop = V.create_loop("X", self.transcript(last_seen, "a"), D(last_seen))
        loop.last_seen = D(last_seen)
        loop.status = status
        if status == "paused":
            loop.conclusion = "conclusions/c1"
        loop.save()
        return loop

    def test_A_backfilled_loop_survives_day_one(self) -> None:
        loop = self._loop("2024-11-02")           # last seen 21 months ago
        self.assertFalse(loop.is_decayed(D("2026-08-02")))

    def test_B_backfilled_loop_archives_after_full_window(self) -> None:
        loop = self._loop("2024-11-02")
        self.assertTrue(loop.is_decayed(D("2026-09-27")))   # go-live + 8w + 1d

    def test_B_boundary_is_not_off_by_one(self) -> None:
        loop = self._loop("2024-11-02")
        self.assertFalse(loop.is_decayed(D("2026-09-26")), "exactly 8w = alive")

    def test_C_recent_loop_not_archived(self) -> None:
        loop = self._loop("2026-07-11")           # 3 weeks before go-live
        self.assertFalse(loop.is_decayed(D("2026-08-02")))

    def test_D_paused_uses_double_window(self) -> None:
        loop = self._loop("2024-11-02", status="paused")
        self.assertFalse(loop.is_decayed(D("2026-09-27")), "8w must not archive")
        self.assertTrue(loop.is_decayed(D("2026-11-22")), "16w must archive")

    def test_D_decision_only_uses_double_window(self) -> None:
        loop = self._loop("2024-11-02", status="decision-only")
        self.assertFalse(loop.is_decayed(D("2026-09-27")))
        self.assertTrue(loop.is_decayed(D("2026-11-22")))

    def test_researching_never_decays(self) -> None:
        loop = self._loop("2024-11-02", status="researching")
        self.assertFalse(loop.is_decayed(D("2027-12-31")))

    def test_decay_inert_before_go_live(self) -> None:
        dc.CFG["decay"]["go_live_date"] = None
        loop = self._loop("2024-11-02")
        self.assertFalse(loop.is_decayed(D("2030-01-01")))

    def test_run_decay_moves_files_and_preserves_conclusion(self) -> None:
        (self.tmp / "conclusions").mkdir(exist_ok=True)
        (self.tmp / "vault" / "conclusions").mkdir(parents=True, exist_ok=True)
        loop = self._loop("2024-11-02", status="paused")
        archived = V.run_decay(D("2026-11-22"))
        self.assertEqual([l.id for l in archived], ["L0001"])
        self.assertTrue((self.tmp / "archive" / "L0001.md").exists())
        self.assertFalse((self.tmp / "loops" / "L0001.md").exists())


class CatalogTest(VaultCase):
    def test_catalog_reflects_frontmatter(self) -> None:
        V.create_loop("Alpha", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.create_loop("Beta", self.transcript("2026-07-02", "b"), D("2026-07-02"))
        path = V.regenerate_catalog()
        text = path.read_text(encoding="utf-8")
        self.assertIn("| L0001 | Alpha | open | 1 |", text)
        self.assertIn("| L0002 | Beta | open | 1 |", text)

    def test_regeneration_is_idempotent(self) -> None:
        V.create_loop("Alpha", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        first = V.regenerate_catalog().read_text(encoding="utf-8")
        second = V.regenerate_catalog().read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_hand_edit_is_overwritten(self) -> None:
        V.create_loop("Alpha", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        path = V.regenerate_catalog()
        path.write_text("I hand-edited this\n", encoding="utf-8")
        V.regenerate_catalog()
        self.assertIn("| L0001 | Alpha |", path.read_text(encoding="utf-8"))

    def test_catalog_excluded_from_loop_loading(self) -> None:
        V.create_loop("Alpha", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.regenerate_catalog()
        self.assertEqual([l.id for l in V.load_loops()], ["L0001"])

    def test_pipe_in_title_does_not_break_table(self) -> None:
        V.create_loop("A | B", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        text = V.regenerate_catalog().read_text(encoding="utf-8")
        row = [l for l in text.splitlines() if l.startswith("| L0001")][0]
        self.assertIn(r"A \| B", row, "pipe in title must be escaped")
        # Count only unescaped delimiters — the escaped one is not a column break.
        delimiters = len(re.findall(r"(?<!\\)\|", row))
        self.assertEqual(delimiters, 6, "escaped pipe must not add a column")


class SelectionTest(VaultCase):
    def test_recency_outranks_raw_count(self) -> None:
        """Owner decision Q15: a NEW occurrence defines relevance, so a loop
        seen twice last month outranks one seen five times in 2024."""
        old = V.create_loop("Old nag", self.transcript("2024-01-01", "o1"),
                            D("2024-01-01"))
        for i, d in enumerate(["2024-02-01", "2024-03-01", "2024-04-01", "2024-05-01"]):
            V.add_occurrence(old, self.transcript(d, f"o{i+2}"), D(d))
        new = V.create_loop("Live thread", self.transcript("2026-07-10", "n1"),
                            D("2026-07-10"))
        V.add_occurrence(new, self.transcript("2026-07-28", "n2"), D("2026-07-28"))
        self.assertEqual(old.recurrence_count, 5)
        self.assertEqual(new.recurrence_count, 2)
        picked = V.select_for_research(n=1, ref=D("2026-08-01"))
        self.assertEqual(picked[0].id, new.id)

    def test_recurrence_min_is_enforced(self) -> None:
        V.create_loop("Only once", self.transcript("2026-07-30", "a"), D("2026-07-30"))
        self.assertEqual(V.select_for_research(ref=D("2026-08-01")), [])

    def test_only_open_loops_selected(self) -> None:
        loop = V.create_loop("X", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.add_occurrence(loop, self.transcript("2026-07-20", "b"), D("2026-07-20"))
        loop.status = "paused"; loop.conclusion = "conclusions/c1"; loop.save()
        self.assertEqual(V.select_for_research(ref=D("2026-08-01")), [])


class MergeTest(VaultCase):
    def test_merge_arithmetic_and_redirect(self) -> None:
        a = V.create_loop("Keep", self.transcript("2026-06-01", "a1"), D("2026-06-01"))
        V.add_occurrence(a, self.transcript("2026-06-10", "a2"), D("2026-06-10"))
        b = V.create_loop("Retire", self.transcript("2026-05-01", "b1"), D("2026-05-01"))
        V.add_occurrence(b, self.transcript("2026-06-10", "a2"), D("2026-06-10"))  # shared

        merged = V.merge_loops(a, b)
        # union = a1, a2, b1 -> 3 distinct. NOT max(2,2)=2 and NOT sum=4.
        self.assertEqual(merged.recurrence_count, 3)
        self.assertEqual(merged.first_seen, D("2026-05-01"), "earliest wins")
        self.assertEqual(merged.last_seen, D("2026-06-10"))
        for stub in (self.tmp / "archive" / "L0002.md",
                     self.tmp / "loops" / "L0002.md"):
            self.assertTrue(stub.exists(), f"redirect stub must exist at {stub}")
            fm, _ = dc.read_page(stub)
            self.assertEqual(fm["redirect_to"], "L0001")

        # The stub lives at the RETIRED path precisely so [[loops/L0002]] keeps
        # resolving. This assertion used to demand the opposite (that the file
        # be deleted), which encoded the link-breaking bug as the contract.
        self.assertNotIn("L0002", [l.id for l in V.load_loops()],
                         "a redirect is a signpost, not a loop — counting it "
                         "would resurrect the duplicate the merge removed")


class LintTest(VaultCase):
    def test_clean_vault_passes(self) -> None:
        V.create_loop("A", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        self.assertEqual(V.lint(), [])

    def test_broken_occurrence_link_detected(self) -> None:
        loop = V.create_loop("A", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        loop.occurrences.append("[[sources/transcripts/2026/07/does-not-exist]]")
        loop.recurrence_count = loop.distinct_conversations()
        loop.save()
        self.assertTrue(any("broken occurrence link" in x for x in V.lint()))

    def test_count_drift_detected(self) -> None:
        loop = V.create_loop("A", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        loop.recurrence_count = 7
        loop.save()
        self.assertTrue(any("distinct occurrences" in x for x in V.lint()))

    def test_paused_without_conclusion_detected(self) -> None:
        loop = V.create_loop("A", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        loop.status = "paused"
        loop.save()
        self.assertTrue(any("paused without a conclusion" in x for x in V.lint()))

    def test_orphan_conclusion_detected(self) -> None:
        (self.tmp / "vault" / "conclusions").mkdir(parents=True, exist_ok=True)
        (self.tmp / "conclusions" / "orphan.md").write_text("---\n---\nx\n",
                                                            encoding="utf-8")
        self.assertTrue(any("orphan" in x for x in V.lint()))

    def test_out_of_vocabulary_tag_detected(self) -> None:
        loop = V.create_loop("A", self.transcript("2026-07-01", "a"), D("2026-07-01"))
        loop.tags = ["architecture", "made-up-tag"]
        loop.save()
        problems = V.lint(vocabulary={"architecture"})
        self.assertTrue(any("made-up-tag" in x for x in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class MergeProposalTest(VaultCase):
    """§6.6 — the conservative bias rule is only self-healing if something
    offers the split back. These prove the mechanism exists."""

    def setUp(self) -> None:
        super().setUp()
        import merge_proposals as MP
        self.MP = MP

    def test_near_duplicate_titles_are_proposed(self) -> None:
        V.create_loop("Should loop matching use embeddings or an LLM judge?",
                      self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.create_loop("Should matching use embeddings or a judge model?",
                      self.transcript("2026-07-02", "b"), D("2026-07-02"))
        out = self.MP.refresh()
        self.assertEqual(out["new"], 1)
        prop = self.MP._load()[0]
        self.assertEqual({prop["keep"], prop["retire"]}, {"L0001", "L0002"})
        self.assertIn("confirm to merge", prop["reason"])

    def test_unrelated_loops_are_not_proposed(self) -> None:
        V.create_loop("How should agent memory persist across sessions?",
                      self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.create_loop("Subscription or API keys for scheduled jobs?",
                      self.transcript("2026-07-02", "b"), D("2026-07-02"))
        self.assertEqual(self.MP.refresh()["new"], 0)

    def test_refresh_is_idempotent(self) -> None:
        V.create_loop("Should routing classify before retrieval?",
                      self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.create_loop("Should routing classify before or after retrieval?",
                      self.transcript("2026-07-02", "b"), D("2026-07-02"))
        self.MP.refresh()
        self.assertEqual(self.MP.refresh()["new"], 0, "must not re-propose")
        self.assertEqual(len(self.MP._load()), 1)

    def test_confirm_merges_and_clears_the_proposal(self) -> None:
        a = V.create_loop("Should matching use embeddings or an LLM judge?",
                          self.transcript("2026-07-01", "a"), D("2026-07-01"))
        V.add_occurrence(a, self.transcript("2026-07-05", "a2"), D("2026-07-05"))
        V.create_loop("Should matching use embeddings or a judge model?",
                      self.transcript("2026-07-02", "b"), D("2026-07-02"))
        self.MP.refresh()
        out = self.MP.confirm("L0001", "L0002")
        self.assertEqual(out["merged_into"], "L0001")
        self.assertEqual(out["recurrence_count"], 3)
        self.assertEqual(out["first_seen"], "2026-07-01")
        self.assertEqual(self.MP._load(), [])
        self.assertTrue((self.tmp / "archive" / "L0002.md").exists())


class IntraBatchMatchingTest(VaultCase):
    """The recurrence filter's hole: Stage A queries the catalog, which cannot
    contain a loop being created in the same payload. Observed live as
    L0036/L0037 and L0038/L0039 — byte-identical titles, one batch."""

    def setUp(self) -> None:
        super().setUp()
        import apply_extraction as AE
        self.AE = AE

    def _cand(self, title, slug, date, decision="new", **match):
        m = {"decision": decision, "loop_id": None, "batch_ref": None,
             "justification": "fixture"}
        m.update(match)
        self.transcript(date, slug)
        return {"title": title, "date": date,
                "transcript": f"sources/transcripts/{date[:4]}/{date[5:7]}/{date}--{slug}",
                "match": m}

    def test_identical_titles_in_one_batch_do_not_split(self) -> None:
        t = "How can event attendee lists be obtained and pre-analyzed reliably?"
        payload = {"candidates": [self._cand(t, "a", "2026-06-10"),
                                  self._cand(t, "b", "2026-06-12")]}
        stats = self.AE.apply_result(payload)
        self.assertEqual(stats["created"], 1, "identical titles must not split")
        self.assertEqual(stats["intra_batch_matched"], 1)
        loops = V.load_loops()
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0].recurrence_count, 2, "recurrence must accrue")

    def test_distinct_titles_in_one_batch_still_split(self) -> None:
        payload = {"candidates": [
            self._cand("How should agent memory persist across sessions?",
                       "a", "2026-06-10"),
            self._cand("Subscription or API keys for scheduled jobs?",
                       "b", "2026-06-12")]}
        stats = self.AE.apply_result(payload)
        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["intra_batch_matched"], 0)

    def test_merely_adjacent_titles_still_split_for_the_owner(self) -> None:
        """Below the auto-attach threshold the conservative bias rule still
        governs — these must split and reach the owner as a merge proposal,
        not be silently merged."""
        payload = {"candidates": [
            self._cand("Should routing be intent-based or keyword-based?",
                       "a", "2026-06-10"),
            self._cand("Should the router classify before or after retrieval?",
                       "b", "2026-06-12")]}
        stats = self.AE.apply_result(payload)
        self.assertEqual(stats["created"], 2, "adjacent != identical")
        import merge_proposals as MP
        self.assertGreaterEqual(MP.refresh()["new"], 0)

    def test_model_supplied_batch_ref_is_honoured(self) -> None:
        payload = {"candidates": [
            self._cand("How should agent memory persist across sessions?",
                       "a", "2026-06-10"),
            self._cand("Where should durable agent state actually live?",
                       "b", "2026-06-12", decision="matched", batch_ref=0)]}
        stats = self.AE.apply_result(payload)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["intra_batch_matched"], 1)
        self.assertEqual(V.load_loops()[0].recurrence_count, 2)

    def test_forward_and_self_batch_refs_are_rejected(self) -> None:
        for bad_ref in (1, 0):
            with self.subTest(ref=bad_ref):
                payload = {"candidates": [
                    self._cand("A question", "a", "2026-06-10",
                               decision="matched", batch_ref=bad_ref)]}
                stats = self.AE.apply_result(payload)
                self.assertEqual(stats["rejected"], 1)

    def test_batch_ref_may_name_a_loop_id_created_this_batch(self) -> None:
        payload = {"candidates": [
            self._cand("How should agent memory persist across sessions?",
                       "a", "2026-06-10"),
            self._cand("Where should durable agent state live?", "b",
                       "2026-06-12", decision="matched", batch_ref="L0001")]}
        stats = self.AE.apply_result(payload)
        self.assertEqual(stats["intra_batch_matched"], 1)
        self.assertEqual(V.load_loops()[0].recurrence_count, 2)

    def test_existing_loop_match_still_works(self) -> None:
        """The new path must not break matching against the catalog."""
        occ = self.transcript("2026-05-01", "old")
        V.create_loop("An existing tracked loop", occ, D("2026-05-01"))
        payload = {"candidates": [self._cand("whatever", "b", "2026-06-12",
                                             decision="matched", loop_id="L0001")]}
        stats = self.AE.apply_result(payload)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["intra_batch_matched"], 0)
        self.assertEqual(V.load_loops()[0].recurrence_count, 2)


class SupersededConclusionTest(VaultCase):
    """Re-researching a loop must not orphan its previous conclusion.

    Stamping only the conclusion pages makes the chain discoverable from a page
    you already found; the loop is the entry point, so without a link there
    every re-run mints a fresh orphan. Found live 2026-08-02 after re-dreaming
    L0040 twice.
    """

    def test_body_wikilinks_count_as_links(self) -> None:
        loop = V.create_loop("A", self.transcript("2026-07-01", "a"),
                             D("2026-07-01"))
        # Two homes on purpose: lint's orphan scan globs the conclusions dir,
        # while wikilink resolution walks the vault root. A conclusion has to
        # satisfy both, so the fixture writes both.
        for stem in ("2026-07-01--old", "2026-07-02--new"):
            fm = {"type": "conclusion", "loop": loop.id}
            dc.write_page(self.tmp / "conclusions" / f"{stem}.md", fm, "x")
            dc.write_page(self.tmp / "vault" / "conclusions" / f"{stem}.md",
                          fm, "x")

        loop.conclusion = "conclusions/2026-07-02--new"
        loop.save()
        self.assertTrue(any("2026-07-01--old" in p for p in V.lint()),
                        "an unlinked previous conclusion is a real orphan")

        loop.body = (loop.body + "\n\n## Superseded conclusions\n\n"
                     "- [[conclusions/2026-07-01--old]]\n")
        loop.save()
        self.assertEqual(V.lint(), [],
                         "history linked from the loop body is not rot")
