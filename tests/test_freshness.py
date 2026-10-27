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
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import freshness as F  # noqa: E402

NOW = _dt.datetime(2026, 10, 3, 12, 0, 0)
NIGHTLY = ("sync-claude-ai", "ingest-cc", "nightly-extract", "nightly-assess", "night-cycle", "weekly-dream",
           "frontier")


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
            "digests": vault / "digests", "archive": vault / "archive", "frontier": vault / "frontier",
            "logs": self.tmp / "logs",
        }
        for d in paths.values():
            d.mkdir(parents=True, exist_ok=True)
        self.meta = paths["meta"]
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({k: str(v) for k, v in paths.items()})
        self._orig_now = os.environ.get("DREAMER_NOW")
        os.environ["DREAMER_NOW"] = NOW.isoformat()
        # The claude.ai axes are judged only once the sync is set up (an ID is
        # configured); the example config ships it empty.
        self._orig_sync = dict(dc.CFG.get("claude_ai_sync") or {})
        dc.CFG.setdefault("claude_ai_sync", {})["extension_id"] = "a" * 32
        self.seed_fresh()

    def tearDown(self) -> None:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._orig)
        dc.CFG["claude_ai_sync"] = self._orig_sync
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
        self.sync_state("ok", last_ok=1)

    def sync_state(self, status: str, last_ok: float | None, last_attempt: float = 1,
                   **extra) -> None:
        self.write("claude-ai-sync.json", {
            "last_attempt": ago(last_attempt),
            "last_ok": ago(last_ok) if last_ok is not None else None,
            "status": status, "detail": "SECRET-DETAIL /home/x", "cursor": ago(5),
            "fetched": 3, "converted": 2, **extra})

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
        want = {"source.claude-export", "source.claude-ai", "job.sync-claude-ai", "source.cc-sessions", "source.owner-writes",
                "job.ingest-cc", "job.nightly-extract", "job.nightly-assess",
                "job.night-cycle", "job.weekly-dream", "job.decay-archive", "job.healthcheck",
                "converge.extraction", "converge.assessment", "converge.folds",
                "converge.search-index", "output.conclusion", "output.digest",
                "publish.postgres", "publish.board", "job.frontier", "converge.frontier-map",
                "converge.premortem", "publish.frontier", "publish.lesson-sync"}
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


class FrontierAxesTest(FreshCase):
    """The knowledge-frontier axes. Each reads ok when fresh and stale when old."""

    def heartbeats(self) -> dict:
        return json.loads((self.meta / "heartbeats.json").read_text(encoding="utf-8"))

    def goal(self, gid: str = "G0001", status: str = "active", created: str = "2026-10-02") -> None:
        d = Path(dc.CFG["paths"]["goals"])
        (d / f"{gid}.md").write_text(
            f"---\ntype: goal\nid: {gid}\nstatus: {status}\ntitle: Goal {gid}\ncreated: {created}\n"
            f"plan_revision: 1\ntarget_date: 2027-06-30\nfinish_line: Done.\nwhy: Because.\n---\n\n# g\n\n"
            f"## Plan (revision 1)\n\n- [ ] Step\n", encoding="utf-8")

    def premortem(self, gid: str, days_ago: float) -> None:
        import frontier_gaps as FG
        date = (NOW - _dt.timedelta(days=days_ago)).date().isoformat()
        FG.write_record(gid, {"date": date, "plan_revision": 1, "causes": [], "candidates": []})

    def map_ledger(self, done: list[Path]) -> None:
        import make_batch as MB
        self.write(dc.CFG["frontier"]["map_ledger_file"],
                   {"version": 1, "done": {MB.rel_of(f): {"message_count": 1, "turns": 1} for f in done},
                    "deferred": {}, "skipped": {}})

    # -- job.frontier --------------------------------------------------------
    def test_job_frontier_is_ok_when_it_ran_last_night(self) -> None:
        self.assertEqual(self.axis("job.frontier")["status"], "ok")

    def test_job_frontier_is_stale_after_a_missed_night(self) -> None:
        self.set_job("frontier", ok_hours_ago=40)
        self.assertEqual(self.axis("job.frontier")["status"], "stale")

    def test_job_frontier_is_stale_when_its_last_run_failed(self) -> None:
        self.set_job("frontier", ok_hours_ago=1, outcome="failed")
        self.assertEqual(self.axis("job.frontier")["status"], "stale")

    # -- converge.frontier-map ----------------------------------------------
    def test_map_backlog_is_info_before_the_map_exists(self) -> None:
        self.transcript("2026-09-01--a.md", waiting_hours=24 * 30)
        a = self.axis("converge.frontier-map")          # no ledger: the backfill has not begun
        self.assertEqual(a["status"], "info")

    def test_map_backlog_is_ok_when_everything_is_mapped(self) -> None:
        f = self.transcript("2026-10-01--a.md", waiting_hours=24 * 20)
        self.map_ledger([f])
        a = self.axis("converge.frontier-map")
        self.assertEqual((a["status"], a["backlog"]), ("ok", 0))

    def test_map_backlog_is_ok_for_a_recent_unmapped_transcript(self) -> None:
        f = self.transcript("2026-10-01--a.md", waiting_hours=24 * 20)
        self.map_ledger([f])
        self.transcript("2026-10-03--b.md", waiting_hours=12)
        a = self.axis("converge.frontier-map")
        self.assertEqual((a["status"], a["backlog"]), ("ok", 1))

    def test_map_backlog_is_stale_when_a_transcript_waits_too_long(self) -> None:
        f = self.transcript("2026-10-01--a.md", waiting_hours=24 * 20)
        self.map_ledger([f])
        self.transcript("2026-09-28--b.md", waiting_hours=24 * 5)
        a = self.axis("converge.frontier-map")
        self.assertEqual((a["status"], a["backlog"]), ("stale", 1))

    def test_a_transcript_the_map_gave_up_on_is_not_backlog(self) -> None:
        import make_batch as MB
        f = self.transcript("2026-09-28--b.md", waiting_hours=24 * 5)
        self.write(dc.CFG["frontier"]["map_ledger_file"],
                   {"version": 1, "done": {}, "deferred": {},
                    "skipped": {MB.rel_of(f): {"reason": "parse", "attempts": 3}}})
        self.assertEqual(self.axis("converge.frontier-map")["status"], "ok")

    def test_a_chat_that_grew_since_it_was_mapped_is_backlog_again(self) -> None:
        f = self.transcript("2026-09-28--b.md", waiting_hours=24 * 5)
        f.write_text("---\nmessage_count: 9\n---\n\n**Human:** hello\n", encoding="utf-8")
        ts = (NOW - _dt.timedelta(hours=24 * 5)).timestamp()
        os.utime(f, (ts, ts))
        self.map_ledger([f])                      # mapped at message_count 1
        a = self.axis("converge.frontier-map")
        self.assertEqual((a["status"], a["backlog"]), ("stale", 1))

    # -- converge.premortem --------------------------------------------------
    def test_premortem_is_ok_without_an_active_goal(self) -> None:
        self.goal(status="paused", created="2026-01-01")
        a = self.axis("converge.premortem")
        self.assertEqual((a["status"], a["backlog"]), ("ok", 0))

    def test_premortem_is_ok_inside_the_window(self) -> None:
        self.goal(created="2026-08-01")
        self.premortem("G0001", days_ago=6)
        self.assertEqual(self.axis("converge.premortem")["status"], "ok")

    def test_premortem_is_ok_inside_the_slack(self) -> None:
        self.goal(created="2026-08-01")
        self.premortem("G0001", days_ago=9)           # due (7d) but inside window + slack (10d)
        self.assertEqual(self.axis("converge.premortem")["status"], "ok")

    def test_premortem_is_stale_past_window_plus_slack(self) -> None:
        self.goal(created="2026-08-01")
        self.premortem("G0001", days_ago=12)
        a = self.axis("converge.premortem")
        self.assertEqual((a["status"], a["backlog"]), ("stale", 1))

    def test_a_goal_that_never_had_a_premortem_goes_stale_from_its_creation(self) -> None:
        self.goal(created="2026-09-01")
        self.assertEqual(self.axis("converge.premortem")["status"], "stale")
        self.goal(created="2026-10-02")
        self.assertEqual(self.axis("converge.premortem")["status"], "ok")

    def test_only_the_neglected_goal_is_stale(self) -> None:
        self.goal("G0001", created="2026-08-01"); self.premortem("G0001", days_ago=2)
        self.goal("G0002", created="2026-08-01"); self.premortem("G0002", days_ago=30)
        a = self.axis("converge.premortem")
        self.assertEqual((a["status"], a["backlog"]), ("stale", 1))

    # -- publish.frontier ----------------------------------------------------
    def test_publish_frontier_is_off_when_disabled(self) -> None:
        a = self.axis("publish.frontier")
        self.assertEqual((a["status"], a["reason"]), ("info", "disabled"))
        data = self.heartbeats(); data["publish_frontier"] = {"configured": False}
        self.write("heartbeats.json", data)
        self.assertEqual(self.axis("publish.frontier")["status"], "info")

    def test_publish_frontier_is_ok_when_recent_and_stale_when_old(self) -> None:
        data = self.heartbeats(); data["publish_frontier"] = {"configured": True, "last_ok": ago(1)}
        self.write("heartbeats.json", data)
        self.assertEqual(self.axis("publish.frontier")["status"], "ok")
        data["publish_frontier"] = {"configured": True, "last_ok": ago(5)}
        self.write("heartbeats.json", data)
        self.assertEqual(self.axis("publish.frontier")["status"], "stale")

    # -- publish.lesson-sync ------------------------------------------------
    def test_lesson_sync_is_info_disabled_while_the_show_url_is_empty(self) -> None:
        self.assertEqual(dc.CFG["frontier"]["show_url"], "")
        a = self.axis("publish.lesson-sync")
        self.assertEqual((a["status"], a["reason"]), ("info", "disabled"))
        # state on disk does not matter while the feature is off
        self.write(dc.CFG["frontier"]["sync_state_file"], {"last_push_at": ago(500)})
        self.assertEqual(self.axis("publish.lesson-sync")["status"], "info")

    def test_lesson_sync_is_judged_once_the_show_url_is_set(self) -> None:
        orig = dc.CFG["frontier"]["show_url"]
        self.addCleanup(dc.CFG["frontier"].__setitem__, "show_url", orig)
        dc.CFG["frontier"]["show_url"] = "https://show.example.org"
        self.assertEqual(self.axis("publish.lesson-sync")["status"], "never")
        self.write(dc.CFG["frontier"]["sync_state_file"], {"last_pull_at": ago(2), "last_push_at": ago(2)})
        self.assertEqual(self.axis("publish.lesson-sync")["status"], "ok")
        self.write(dc.CFG["frontier"]["sync_state_file"], {"last_pull_at": ago(60), "last_push_at": ago(60)})
        self.assertEqual(self.axis("publish.lesson-sync")["status"], "stale")

    def test_the_postgres_axis_ignores_the_frontier_record(self) -> None:
        data = self.heartbeats(); data["publish_frontier"] = {"configured": True, "last_ok": ago(1)}
        self.write("heartbeats.json", data)
        self.assertEqual(self.axis("publish.postgres")["status"], "info")

    def test_a_disabled_frontier_and_no_map_leave_the_vault_ok(self) -> None:
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

    def test_a_stalled_frontier_fails_jobs_and_converged_but_blocks_nothing(self) -> None:
        self.set_job("frontier", ok_hours_ago=40)
        ok, detail = H.assert_freshness_jobs()
        self.assertFalse(ok)
        self.assertIn("job.frontier", detail)
        H._STATE_CACHE.clear()
        # a goal never given a pre-mortem since it was created long ago
        d = Path(dc.CFG["paths"]["goals"])
        (d / "G0001.md").write_text(
            "---\ntype: goal\nid: G0001\nstatus: active\ntitle: g\ncreated: 2026-08-01\n"
            "plan_revision: 1\ntarget_date: 2027-06-30\nfinish_line: Done.\nwhy: Because.\n---\n\n# g\n\n"
            "## Plan (revision 1)\n\n- [ ] Step\n", encoding="utf-8")
        ok, detail = H.assert_freshness_converged()
        self.assertFalse(ok)
        self.assertIn("converge.premortem", detail)
        sev = {n: (s_, b) for n, s_, b, _ in H.ASSERTIONS}
        self.assertEqual(sev["freshness-converged"], ("degraded", ()))

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


class ClaudeAiDisabledTest(FreshCase):
    """With no extension ID configured the sync is not set up: every claude.ai
    axis reports info/disabled, never stale, whatever state files exist."""

    AXES = ("source.claude-ai", "job.sync-claude-ai")

    def disable(self, value) -> None:
        dc.CFG["claude_ai_sync"]["extension_id"] = value

    def test_enabled_control_is_judged(self) -> None:
        self.assertEqual(self.axis("source.claude-ai")["status"], "ok")
        self.assertEqual(self.axis("job.sync-claude-ai")["status"], "ok")

    def test_empty_id_reads_info_disabled_on_every_claude_ai_axis(self) -> None:
        for value in ("", None):
            self.disable(value)
            for name in self.AXES:
                a = self.axis(name)
                self.assertEqual((a["status"], a["reason"]), ("info", "disabled"), (name, value))
                self.assertNotIn(a["status"], F.FAILING)

    def test_disabled_ignores_a_missing_state_file_and_a_missing_heartbeat(self) -> None:
        self.disable("")
        (self.meta / "claude-ai-sync.json").unlink()
        self.set_job("sync-claude-ai", ok_hours_ago=500)
        for name in self.AXES:
            self.assertEqual(self.axis(name)["status"], "info", name)
        self.assertEqual(F.collect(NOW)["overall"], "ok")

    def test_no_claude_ai_sync_block_at_all_is_disabled_too(self) -> None:
        dc.CFG.pop("claude_ai_sync")
        for name in self.AXES:
            self.assertEqual(self.axis(name)["reason"], "disabled", name)


class ClaudeAiSyncTest(FreshCase):
    """source.claude-ai is judged from the sync job's state file. Each case
    has a positive control (ok) so a bad fixture failing means something."""

    def setUp(self) -> None:
        super().setUp()
        H._STATE_CACHE.clear()

    def test_ok_recent_is_ok_and_shows_cursor_and_count(self) -> None:
        a = self.axis("source.claude-ai")
        self.assertEqual(a["status"], "ok")
        self.assertIn("cursor", a["reason"])
        self.assertIn("fetched 3", a["reason"])
        self.assertEqual(a["last_arrival"], ago(5))
        self.assertTrue(H.assert_freshness_sources()[0])

    def test_reason_shows_the_cursor_in_utc_not_naive_local(self) -> None:
        # The extension writes the cursor as UTC "...Z". Shown as naive local
        # time it would read hours off with no zone.
        self.sync_state("ok", last_ok=2, cursor="2026-10-02T08:20:10.250000Z")
        try:
            with mock.patch.dict(os.environ, {"TZ": "Asia/Kolkata"}):
                time.tzset()
                a = self.axis("source.claude-ai")
        finally:
            time.tzset()  # back to the restored environment's zone
        self.assertIn("cursor 2026-10-02T08:20:10Z", a["reason"])

    # -- login expiry (the extension reports the sessionKey cookie's expiry,
    # never its value): warn before the monthly re-login breaks the sync.
    def expiry(self, days: float) -> str:
        return (NOW + _dt.timedelta(days=days)).isoformat(timespec="seconds")

    def test_login_far_from_expiry_is_ok_and_shows_the_date(self) -> None:
        self.sync_state("ok", last_ok=1, login_expires_at=self.expiry(20))
        a = self.axis("source.claude-ai")
        self.assertEqual(a["status"], "ok")
        self.assertIn("login until 2026-10-23", a["reason"])

    def test_login_inside_the_warning_window_is_stale_before_anything_breaks(self) -> None:
        self.sync_state("ok", last_ok=1, login_expires_at=self.expiry(2))
        a = self.axis("source.claude-ai")
        self.assertIn(a["status"], F.FAILING)
        self.assertIn("claude.ai login expires in 2.0 days", a["reason"])
        self.assertIn("log out and back in", a["reason"])

    def test_expired_login_is_stale(self) -> None:
        self.sync_state("ok", last_ok=1, login_expires_at=self.expiry(-0.1))
        a = self.axis("source.claude-ai")
        self.assertIn(a["status"], F.FAILING)
        self.assertIn("claude.ai login expired", a["reason"])

    def test_no_login_cookie_reported_is_stale(self) -> None:
        # The extension reports null when Chrome holds no claude.ai login:
        # that is caught on the hourly tick, before the next due run.
        self.sync_state("ok", last_ok=1, login_expires_at=None)
        a = self.axis("source.claude-ai")
        self.assertIn(a["status"], F.FAILING)
        self.assertIn("no claude.ai login in Chrome", a["reason"])

    def test_state_without_the_field_keeps_the_old_verdict(self) -> None:
        # An older extension never sends it: no warning, no failure.
        self.sync_state("ok", last_ok=1)
        a = self.axis("source.claude-ai")
        self.assertEqual(a["status"], "ok")
        self.assertNotIn("login", a["reason"])

    def test_warning_window_comes_from_config(self) -> None:
        self.sync_state("ok", last_ok=1, login_expires_at=self.expiry(2))
        with mock.patch.dict(dc.CFG["freshness"], {"claude_ai_login_warn_days": 1}):
            self.assertEqual(self.axis("source.claude-ai")["status"], "ok")

    def test_a_failed_run_still_leads_with_its_own_problem(self) -> None:
        self.sync_state("needs_login", last_ok=2, login_expires_at=self.expiry(2))
        a = self.axis("source.claude-ai")
        self.assertIn(a["status"], F.FAILING)
        self.assertTrue(a["reason"].startswith("needs re-login"), a["reason"])

    def test_needs_login_is_not_ok_even_though_last_ok_is_recent(self) -> None:
        self.sync_state("needs_login", last_ok=2, last_attempt=1)
        a = self.axis("source.claude-ai")
        self.assertIn(a["status"], F.FAILING)
        self.assertIn("needs re-login", a["reason"])
        ok, detail = H.assert_freshness_sources()
        self.assertFalse(ok)
        self.assertIn("source.claude-ai", detail)

    def test_every_failure_status_is_not_ok(self) -> None:
        for status in ("blocked", "schema_changed", "error", "made-up"):
            self.sync_state(status, last_ok=2)
            a = self.axis("source.claude-ai")
            self.assertIn(a["status"], F.FAILING, status)
            self.assertNotIn("SECRET-DETAIL", a["reason"])

    def test_ok_but_last_ok_older_than_the_window_is_stale(self) -> None:
        self.sync_state("ok", last_ok=40)
        self.assertEqual(self.axis("source.claude-ai")["status"], "stale")
        self.assertFalse(H.assert_freshness_sources()[0])

    def test_just_inside_the_window_is_ok(self) -> None:
        self.sync_state("ok", last_ok=35)
        self.assertEqual(self.axis("source.claude-ai")["status"], "ok")

    def test_missing_file_is_never(self) -> None:
        (self.meta / "claude-ai-sync.json").unlink()
        self.assertEqual(self.axis("source.claude-ai")["status"], "never")
        self.assertFalse(H.assert_freshness_sources()[0])

    def test_ok_with_no_last_ok_is_never(self) -> None:
        self.sync_state("ok", last_ok=None)
        self.assertEqual(self.axis("source.claude-ai")["status"], "never")

    def test_garbage_is_not_ok_and_does_not_crash(self) -> None:
        for raw in ("{not json", "[]", "null", ""):
            (self.meta / "claude-ai-sync.json").write_text(raw, encoding="utf-8")
            a = self.axis("source.claude-ai")
            self.assertIn(a["status"], F.FAILING, raw)
            H._STATE_CACHE.clear()
            self.assertFalse(H.assert_freshness_sources()[0], raw)
        self.write("claude-ai-sync.json", {"status": "ok", "last_ok": "garbage",
                                           "fetched": "x", "cursor": 5})
        self.assertIn(self.axis("source.claude-ai")["status"], F.FAILING)

    def test_job_axis_follows_the_heartbeat(self) -> None:
        self.assertEqual(self.axis("job.sync-claude-ai")["status"], "ok")
        self.set_job("sync-claude-ai", ok_hours_ago=40)
        self.assertEqual(self.axis("job.sync-claude-ai")["status"], "stale")

    def test_threshold_comes_from_config(self) -> None:
        self.assertEqual(self.axis("source.claude-ai")["threshold_hours"],
                         float(F.cfg("freshness", "claude_ai_sync_max_age_hours")))


if __name__ == "__main__":
    unittest.main()
