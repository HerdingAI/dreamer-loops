#!/usr/bin/env python3
"""Freshness verdict rules, at a fixed clock."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import freshness as F  # noqa: E402

NOW = _dt.datetime(2026, 10, 3, 12, 0, 0)
NIGHTLY = ("ingest-cc", "nightly-extract", "nightly-assess", "night-cycle", "weekly-dream")


def ago(hours: float) -> str:
    return (NOW - _dt.timedelta(hours=hours)).isoformat(timespec="seconds")


class FreshCase(unittest.TestCase):
    """A complete temp vault with every judged axis fresh."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-fresh-"))
        vault = self.tmp / "vault"
        paths = {
            "root": self.tmp, "vault": vault, "meta": vault / ".vault-meta",
            "sources": vault / "sources" / "transcripts", "loops": vault / "loops",
            "goals": vault / "goals", "conclusions": vault / "conclusions",
            "digests": vault / "digests", "archive": vault / "archive",
            "logs": self.tmp / "logs",
        }
        for d in paths.values():
            d.mkdir(parents=True, exist_ok=True)
        self.meta = paths["meta"]
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({k: str(v) for k, v in paths.items()})
        self._orig_now = os.environ.get("DREAMER_NOW")
        os.environ["DREAMER_NOW"] = NOW.isoformat()
        self.seed_fresh()

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig)
        if self._orig_now is None:
            os.environ.pop("DREAMER_NOW", None)
        else:
            os.environ["DREAMER_NOW"] = self._orig_now
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, obj) -> None:
        (self.meta / name).write_text(json.dumps(obj), encoding="utf-8")

    def job(self, name: str, ok_hours_ago: float | None, outcome: str = "ok",
            produced_hours_ago: float | None = None) -> dict:
        j = {"last_outcome": outcome}
        if ok_hours_ago is not None:
            j["last_ok"] = ago(ok_hours_ago)
        if produced_hours_ago is not None:
            j["last_produced"] = ago(produced_hours_ago)
        return j

    def seed_fresh(self) -> None:
        jobs = {n: self.job(n, 1) for n in NIGHTLY}
        jobs["decay-archive"] = self.job("decay-archive", 24)
        self.write("heartbeats.json", {"jobs": jobs, "runs": [], "publish": {}})
        self.write("run-state.json", {"health": {"checked_at": ago(1)},
                                      "last_reindex": {"at": ago(1)}})
        self.write("ingested.json", {"c1": {"updated_at": ago(24), "path": "x", "date": "2026-10-02"}})
        self.write("cc-ingested.json", {"s1": {"status": "ingested",
                                               "mtime": (NOW - _dt.timedelta(hours=5)).timestamp()}})
        self.write("extracted.json", {"done": []})
        self.write("fold-pending.json", [])

    def set_job(self, name: str, **kw) -> None:
        data = json.loads((self.meta / "heartbeats.json").read_text(encoding="utf-8"))
        data["jobs"][name] = self.job(name, **kw)
        self.write("heartbeats.json", data)

    def axis(self, axis_id: str) -> dict:
        return {a["axis"]: a for a in F.collect(NOW)["axes"]}[axis_id]

    def transcript(self, name: str, waiting_hours: float) -> Path:
        f = Path(dc.CFG["paths"]["sources"]) / "2026" / "10" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# t\n\n**Human:** hello\n", encoding="utf-8")
        ts = (NOW - _dt.timedelta(hours=waiting_hours)).timestamp()
        os.utime(f, (ts, ts))
        return f


class ShapeTest(FreshCase):
    def test_a_fresh_vault_is_ok_overall(self) -> None:
        snap = F.collect(NOW)
        failing = [(a["axis"], a["reason"]) for a in snap["axes"] if a["status"] in F.FAILING]
        self.assertEqual(failing, [])
        self.assertEqual(snap["overall"], "ok")
        self.assertEqual(snap["stale_count"], 0)
        self.assertEqual(snap["axis_count"], len(snap["axes"]))
        self.assertEqual(snap["generated_at"], "2026-10-03T12:00:00")

    def test_every_spec_axis_is_present_with_the_full_key_set(self) -> None:
        snap = F.collect(NOW)
        want = {"source.claude-export", "source.cc-sessions", "source.owner-writes",
                "job.ingest-cc", "job.nightly-extract", "job.nightly-assess",
                "job.night-cycle", "job.weekly-dream", "job.decay-archive", "job.healthcheck",
                "converge.extraction", "converge.assessment", "converge.folds",
                "converge.search-index", "output.conclusion", "output.digest",
                "publish.postgres", "publish.board"}
        self.assertEqual({a["axis"] for a in snap["axes"]}, want)
        keys = {"axis", "kind", "label", "status", "last_ran", "last_produced",
                "last_arrival", "last_outcome", "threshold_hours", "backlog", "reason"}
        for a in snap["axes"]:
            self.assertEqual(set(a), keys, a["axis"])

    def test_no_threshold_literal_in_the_module(self) -> None:
        src = (ROOT / "scripts" / "freshness.py").read_text(encoding="utf-8")
        for value in ("28", "10", "16", "30"):
            self.assertIsNone(re.search(rf"(?<![\w.]){value}(?![\w.])", src),
                              f"threshold literal {value} in freshness.py")

    def test_a_missing_config_key_is_loud(self) -> None:
        with self.assertRaises(SystemExit):
            F.cfg("freshness", "no_such_key")


class JobRuleTest(FreshCase):
    def test_just_inside_the_window_is_ok(self) -> None:
        self.set_job("nightly-extract", ok_hours_ago=27.5)
        self.assertEqual(self.axis("job.nightly-extract")["status"], "ok")

    def test_past_the_window_is_stale_with_a_numeric_reason(self) -> None:
        self.set_job("nightly-extract", ok_hours_ago=31.2)
        a = self.axis("job.nightly-extract")
        self.assertEqual(a["status"], "stale")
        self.assertEqual(a["reason"], "last ok run 31.2h ago (max 28h)")
        self.assertEqual(a["threshold_hours"], 28.0)

    def test_a_failed_last_run_is_stale_even_when_recent(self) -> None:
        self.set_job("nightly-extract", ok_hours_ago=20, outcome="failed")
        a = self.axis("job.nightly-extract")
        self.assertEqual(a["status"], "stale")
        self.assertTrue(a["reason"].startswith("last run failed"))

    def test_a_job_with_no_record_is_never(self) -> None:
        self.set_job("nightly-extract", ok_hours_ago=None)
        self.assertEqual(self.axis("job.nightly-extract")["status"], "never")

    def test_weekly_job_uses_the_weekly_window(self) -> None:
        self.set_job("decay-archive", ok_hours_ago=7 * 24)
        self.assertEqual(self.axis("job.decay-archive")["status"], "ok")
        self.set_job("decay-archive", ok_hours_ago=9 * 24)
        self.assertEqual(self.axis("job.decay-archive")["status"], "stale")

    def test_healthcheck_axis_reads_checked_at(self) -> None:
        self.write("run-state.json", {"health": {"checked_at": ago(19)},
                                      "last_reindex": {"at": ago(1)}})
        self.assertEqual(self.axis("job.healthcheck")["status"], "stale")

    def test_last_produced_is_reported_not_judged(self) -> None:
        self.set_job("night-cycle", ok_hours_ago=1, produced_hours_ago=24 * 30)
        a = self.axis("job.night-cycle")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(a["last_produced"], ago(24 * 30))

    def test_future_timestamp_is_ok_not_negative(self) -> None:
        self.set_job("nightly-extract", ok_hours_ago=-5)
        a = self.axis("job.nightly-extract")
        self.assertEqual(a["status"], "ok")
        self.assertNotIn("-", a["reason"])

    def test_corrupt_heartbeats_reads_never(self) -> None:
        (self.meta / "heartbeats.json").write_text("{trunc", encoding="utf-8")
        snap = F.collect(NOW)
        by = {a["axis"]: a for a in snap["axes"]}
        self.assertEqual(by["job.nightly-extract"]["status"], "never")
        self.assertEqual(snap["overall"], "stale")


class SourceRuleTest(FreshCase):
    def test_quiet_claude_export_is_stale(self) -> None:
        self.write("ingested.json", {"c1": {"updated_at": ago(50 * 24)}})
        a = self.axis("source.claude-export")
        self.assertEqual(a["status"], "stale")
        self.assertEqual(a["last_arrival"], ago(50 * 24))

    def test_utc_ledger_stamp_is_compared_in_local_time(self) -> None:
        local = NOW - _dt.timedelta(hours=1)
        utc = local.astimezone().astimezone(_dt.timezone.utc)
        self.write("ingested.json", {"c1": {"updated_at": utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}})
        a = self.axis("source.claude-export")
        self.assertEqual(a["last_arrival"], local.isoformat(timespec="seconds"))
        self.assertEqual(a["status"], "ok")

    def test_missing_ledger_is_never(self) -> None:
        (self.meta / "ingested.json").unlink()
        a = self.axis("source.claude-export")
        self.assertEqual((a["status"], a["reason"]), ("never", "ledger missing"))

    def test_cc_sessions_counts_only_ingested_entries(self) -> None:
        old = (NOW - _dt.timedelta(days=9)).timestamp()
        new = (NOW - _dt.timedelta(hours=2)).timestamp()
        self.write("cc-ingested.json", {"a": {"status": "ingested", "mtime": old},
                                        "b": {"status": "rejected", "mtime": new}})
        self.assertEqual(self.axis("source.cc-sessions")["status"], "stale")

    def test_owner_writes_is_reported_never_stale(self) -> None:
        self.write("owner-writes.json", [{"at": ago(24 * 400)}])
        a = self.axis("source.owner-writes")
        self.assertEqual(a["status"], "info")
        self.assertEqual(a["last_arrival"], ago(24 * 400))


class ConvergenceRuleTest(FreshCase):
    def test_empty_backlog_is_ok(self) -> None:
        a = self.axis("converge.extraction")
        self.assertEqual((a["status"], a["backlog"]), ("ok", 0))

    def test_a_transcript_waiting_too_long_is_stale(self) -> None:
        self.transcript("2026-10-01--a.md", waiting_hours=72)
        self.transcript("2026-10-03--b.md", waiting_hours=2)
        a = self.axis("converge.extraction")
        self.assertEqual((a["status"], a["backlog"]), ("stale", 2))

    def test_a_recent_backlog_is_ok(self) -> None:
        self.transcript("2026-10-03--b.md", waiting_hours=2)
        self.assertEqual(self.axis("converge.extraction")["status"], "ok")

    def test_an_extracted_transcript_is_not_backlog(self) -> None:
        self.transcript("2026-10-01--a.md", waiting_hours=72)
        self.write("extracted.json", {"done": ["sources/transcripts/2026/10/2026-10-01--a"]})
        self.assertEqual(self.axis("converge.extraction")["backlog"], 0)

    def test_assessment_is_ok_without_an_active_goal(self) -> None:
        self.transcript("2026-10-01--a.md", waiting_hours=72)
        a = self.axis("converge.assessment")
        self.assertEqual((a["status"], a["reason"]), ("ok", "no active goal"))

    def test_old_fold_is_stale(self) -> None:
        self.write("fold-pending.json", [{"enqueued_at": ago(8 * 24)}])
        self.assertEqual(self.axis("converge.folds")["status"], "stale")

    def test_old_index_is_stale(self) -> None:
        self.write("run-state.json", {"health": {"checked_at": ago(1)},
                                      "last_reindex": {"at": ago(40)}})
        self.assertEqual(self.axis("converge.search-index")["status"], "stale")


class PublishAxisTest(FreshCase):
    def set_publish(self, pub: dict) -> None:
        data = json.loads((self.meta / "heartbeats.json").read_text(encoding="utf-8"))
        data["publish"] = pub
        self.write("heartbeats.json", data)

    def test_unconfigured_reads_disabled_not_stale(self) -> None:
        # An opt-in publisher that is off is reported, never judged: status
        # stays in the published set and the reason says why.
        a = self.axis("publish.postgres")
        self.assertEqual((a["status"], a["reason"]), ("info", "disabled"))
        self.assertNotIn(a["status"], F.FAILING)

    def test_configured_and_recent_is_ok(self) -> None:
        self.set_publish({"configured": True, "last_ok": ago(1)})
        self.assertEqual(self.axis("publish.postgres")["status"], "ok")

    def test_configured_and_old_is_stale(self) -> None:
        self.set_publish({"configured": True, "last_ok": ago(5)})
        self.assertEqual(self.axis("publish.postgres")["status"], "stale")


class BoardPublishAxisTest(FreshCase):
    def set_board(self, pub: dict) -> None:
        data = json.loads((self.meta / "heartbeats.json").read_text(encoding="utf-8"))
        data["publish_board"] = pub
        self.write("heartbeats.json", data)

    def test_absent_record_is_info_disabled(self) -> None:
        a = self.axis("publish.board")
        self.assertEqual((a["status"], a["reason"]), ("info", "disabled"))

    def test_unconfigured_is_info_disabled(self) -> None:
        self.set_board({"configured": False})
        a = self.axis("publish.board")
        self.assertEqual((a["status"], a["reason"]), ("info", "disabled"))

    def test_recent_success_is_ok(self) -> None:
        self.set_board({"configured": True, "last_ok": ago(1)})
        self.assertEqual(self.axis("publish.board")["status"], "ok")

    def test_old_success_is_stale(self) -> None:
        self.set_board({"configured": True, "last_ok": ago(5)})
        self.assertEqual(self.axis("publish.board")["status"], "stale")

    def test_the_postgres_axis_ignores_the_board_record(self) -> None:
        self.set_board({"configured": True, "last_ok": ago(1)})
        a = self.axis("publish.postgres")
        self.assertEqual((a["status"], a["reason"]), ("info", "disabled"))

    def test_a_disabled_board_does_not_make_the_vault_stale(self) -> None:
        self.assertEqual(F.collect(NOW)["overall"], "ok")


import healthcheck as H  # noqa: E402


class AssertionTest(FreshCase):
    def setUp(self) -> None:
        super().setUp()
        H._STATE_CACHE.clear()

    def test_registered_degraded_and_never_blocking(self) -> None:
        got = {name: (sev, blocks) for name, sev, blocks, _ in H.ASSERTIONS}
        for name in ("freshness-jobs", "freshness-sources", "freshness-converged"):
            self.assertEqual(got.get(name), ("degraded", ()), name)

    def test_all_pass_on_a_fresh_vault(self) -> None:
        for fn in (H.assert_freshness_jobs, H.assert_freshness_sources,
                   H.assert_freshness_converged):
            ok, detail = fn()
            self.assertTrue(ok, detail)

    def test_a_stale_job_fails_jobs_only(self) -> None:
        self.set_job("nightly-extract", ok_hours_ago=40)
        ok, detail = H.assert_freshness_jobs()
        self.assertFalse(ok)
        self.assertIn("job.nightly-extract", detail)
        H._STATE_CACHE.clear()
        self.assertTrue(H.assert_freshness_sources()[0])

    def test_a_quiet_source_fails_sources(self) -> None:
        self.write("ingested.json", {"c1": {"updated_at": ago(50 * 24)}})
        ok, detail = H.assert_freshness_sources()
        self.assertFalse(ok)
        self.assertIn("source.claude-export", detail)

    def test_a_waiting_transcript_fails_converged(self) -> None:
        self.transcript("2026-10-01--a.md", waiting_hours=72)
        ok, detail = H.assert_freshness_converged()
        self.assertFalse(ok)
        self.assertIn("converge.extraction", detail)

    def test_the_healthcheck_axis_is_not_asserted_by_jobs(self) -> None:
        # The watchdog owns it; asserting it here would have the healthcheck
        # report on its own staleness from inside a run that just refreshed it.
        self.write("run-state.json", {"health": {"checked_at": ago(40)},
                                      "last_reindex": {"at": ago(1)}})
        self.assertTrue(H.assert_freshness_jobs()[0])

    def test_folds_and_index_are_not_asserted_twice(self) -> None:
        self.write("fold-pending.json", [{"enqueued_at": ago(9 * 24)}])
        self.assertTrue(H.assert_freshness_converged()[0])

    def test_assertions_do_not_write(self) -> None:
        before = {f.name: f.read_bytes() for f in self.meta.iterdir()}
        for fn in (H.assert_freshness_jobs, H.assert_freshness_sources,
                   H.assert_freshness_converged):
            fn()
        self.assertEqual({f.name: f.read_bytes() for f in self.meta.iterdir()}, before)


class ReviewFixTest(FreshCase):
    def setUp(self) -> None:
        super().setUp()
        H._STATE_CACHE.clear()

    def test_a_failing_dream_leg_is_stale_though_the_wrapper_exits_clean(self) -> None:
        self.set_job("weekly-dream", ok_hours_ago=2, outcome="failed")
        self.assertEqual(self.axis("job.night-cycle")["status"], "ok")
        self.assertEqual(self.axis("job.weekly-dream")["status"], "stale")
        ok, detail = H.assert_freshness_jobs()
        self.assertFalse(ok)
        self.assertIn("job.weekly-dream", detail)

    def test_assertion_detail_is_stable_as_the_axis_ages(self) -> None:
        # write_health dedups events on the exact detail string; an age in it
        # would stack one digest line per healthcheck run.
        self.set_job("nightly-extract", ok_hours_ago=40)
        first = H.assert_freshness_jobs()[1]
        H._STATE_CACHE.clear()
        self.set_job("nightly-extract", ok_hours_ago=47.3)
        self.assertEqual(H.assert_freshness_jobs()[1], first)


if __name__ == "__main__":
    unittest.main()
