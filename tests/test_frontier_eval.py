#!/usr/bin/env python3
"""Tests for scripts/frontier_eval.py on a small synthetic fixture
(tests/fixtures/frontier/eval/). The live eval calls the real model and is never run
here: this validates the case files and the split, and runs the judges as stubs."""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import frontier_eval as E  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "frontier" / "eval"


class DryRunTest(unittest.TestCase):
    def test_fixture_cases_are_valid_and_split_deterministically(self):
        cases = E.load_cases(FIXTURE)
        self.assertEqual(E.validate(cases), [])
        self.assertEqual({h: sorted(c["id"] for c in cases[h]) for h in E.HALVES},
                         {"tune": ["assistant_only-a", "bad_lesson-a", "tactical-a"], "holdout": ["tactical-b"]})

    def test_code_gates_agree_with_the_seeded_lesson_labels(self):
        """The model half needs a live run; the code half is deterministic: the good
        controls clear every code gate and the injected / number cases fail one."""
        import frontier_lessons as FL
        import verify_passages as VP
        cases = {c["id"]: c for h in E.HALVES for c in E.load_cases(FIXTURE)[h]}
        for cid, c in cases.items():
            if c["call"] != "lesson-check":
                continue
            by_id = {s["id"]: VP.Source(id=s["id"], kind=s["kind"], title="t", text="") for s in c["sources"]}
            n_of = FL._number_sources(c["claims"], by_id)
            reasons = FL.gate_body(c["lesson"]["title"], c["lesson"]["body"], c["claims"],
                                   {n: by_id[sid].kind for sid, n in n_of.items()})
            if c["kind"] == "good_lesson" or c["flaw"] in ("uncited", "padding"):
                self.assertEqual(reasons, [], cid)   # only the model can see these flaws
            else:
                self.assertTrue(reasons, cid)

    def test_cli_dry_run_exits_zero_without_calling_anything(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = E.main(["--dry-run", "--eval-dir", str(FIXTURE)])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(out["cases"], {"tune": 3, "holdout": 1})

    def test_a_missing_case_directory_is_an_error_not_an_empty_pass(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = E.main(["--dry-run", "--eval-dir", str(ROOT / "tests" / "fixtures" / "no-such-eval-dir")])
        self.assertEqual(rc, 1)
        self.assertIn("--eval-dir", err.getvalue())

    def test_a_run_prints_a_pass_fail_table(self):
        """Judges are stubs: one case is judged as labelled, one is not."""
        def consequence(case):
            return {"verdict": "tactical" if case["id"] == "tactical-a" else "consequential",
                    "reason": "stub"}
        with mock.patch.object(E, "_judge_consequence", consequence), \
             mock.patch.object(E, "_judge_check", lambda case: {
                 "status": "ok", "reason": "", "owner_discussed": "none", "exposed": True}), \
             mock.patch.object(E, "_judge_lesson", lambda case: {
                 "verdict": "reject", "reasons": ["stub"]}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = E.main(["--half", "both", "--eval-dir", str(FIXTURE)])
        lines = buf.getvalue().splitlines()
        self.assertEqual(rc, 1)                      # tactical-b was judged consequential
        header = lines[0].split()
        self.assertEqual(header[:3], ["RESULT", "KIND", "CASE"])
        rows = {l.split()[2]: l.split()[0] for l in lines[1:] if l.split()[:1] in (["PASS"], ["FAIL"])}
        self.assertEqual(rows, {"assistant_only-a": "PASS", "bad_lesson-a": "PASS",
                                "tactical-a": "PASS", "tactical-b": "FAIL"})
        summary = json.loads(lines[-1])
        self.assertEqual((summary["cases"], summary["failed"]), (4, ["tactical-b"]))

    def test_the_seeded_bad_lesson_trips_a_code_gate_without_a_model(self):
        import frontier_lessons as FL
        case = next(c for c in E.load_cases(FIXTURE)["tune"] if c["id"] == "bad_lesson-a")
        reasons = FL.gate_body(case["lesson"]["title"], case["lesson"]["body"], case["claims"], {1: "web"})
        self.assertTrue(any("1999" in r for r in reasons), reasons)

    def test_validation_catches_bad_cases(self):
        tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for h in E.HALVES:
            (tmp / h).mkdir()
        good = json.loads((FIXTURE / "tune" / "assistant_only-a.json").read_text())
        (tmp / "tune" / "a.json").write_text(json.dumps({**good, "id": "a"}))
        (tmp / "holdout" / "bad.json").write_text(json.dumps({**good, "id": "bad", "kind": "nonsense"}))
        problems = E.validate(E.load_cases(tmp))
        self.assertTrue(any("nonsense" in p for p in problems), problems)

    def _two_known(self) -> tuple[Path, dict]:
        tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for h in E.HALVES:
            (tmp / h).mkdir()
        base = json.loads((FIXTURE / "tune" / "assistant_only-a.json").read_text())
        known = {**base, "kind": "known", "expect": {"owner_discussed_not": ["none"]}}
        del known["fixture"]
        (tmp / "tune" / "a.json").write_text(json.dumps({**known, "id": "a"}))
        return tmp, known

    def test_a_relabelled_case_keeps_the_half_it_was_born_in(self):
        """Relabelling after a run must never move a case between tune and holdout:
        split_kind keeps it in its original kind's split."""
        tmp, known = self._two_known()
        unowned = {**known, "id": "b", "kind": "unowned", "expect": {"owner_discussed_in": ["none"]}}
        (tmp / "holdout" / "b.json").write_text(json.dumps(
            {**unowned, "split_kind": "known", "relabel": "only assistant turns carry it"}))
        self.assertEqual(E.validate(E.load_cases(tmp)), [])
        (tmp / "holdout" / "b.json").write_text(json.dumps(unowned))  # no split_kind: b alone is position 0
        self.assertTrue(any("deterministic split" in p for p in E.validate(E.load_cases(tmp))))

    def test_a_relabel_records_why_and_an_unowned_label_is_none(self):
        tmp, known = self._two_known()
        (tmp / "holdout" / "b.json").write_text(json.dumps(
            {**known, "id": "b", "kind": "unowned", "expect": {"owner_discussed_in": ["none"]},
             "split_kind": "known"}))
        self.assertTrue(any("relabel" in p for p in E.validate(E.load_cases(tmp))))
        (tmp / "holdout" / "b.json").write_text(json.dumps(
            {**known, "id": "b", "kind": "unowned", "split_kind": "known", "relabel": "why"}))
        self.assertTrue(any("unowned" in p for p in E.validate(E.load_cases(tmp))))


if __name__ == "__main__":
    unittest.main()
