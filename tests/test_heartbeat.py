#!/usr/bin/env python3
"""Job heartbeats."""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import heartbeat as HB  # noqa: E402


class HeartbeatCase(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = Path(tempfile.mkdtemp(prefix="dreamer-hb-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.meta, ignore_errors=True)

    def data(self) -> dict:
        return json.loads((self.meta / "heartbeats.json").read_text(encoding="utf-8"))


class RecordTest(HeartbeatCase):
    def test_ok_run_sets_every_clock_but_produced(self) -> None:
        HB.record("nightly-extract", "2026-10-03T19:00:01", "ok",
                  finished="2026-10-03T19:00:08", meta=self.meta)
        j = self.data()["jobs"]["nightly-extract"]
        self.assertEqual(j["last_ok"], "2026-10-03T19:00:08")
        self.assertEqual(j["last_outcome"], "ok")
        self.assertEqual(j["last_started"], "2026-10-03T19:00:01")
        self.assertNotIn("last_produced", j)

    def test_produced_moves_last_produced(self) -> None:
        HB.record("j", "2026-10-03T19:00:01", "ok", produced=True,
                  finished="2026-10-03T19:00:08", meta=self.meta)
        self.assertEqual(self.data()["jobs"]["j"]["last_produced"], "2026-10-03T19:00:08")

    def test_failed_run_keeps_the_previous_last_ok(self) -> None:
        HB.record("j", "2026-10-02T19:00:00", "ok", finished="2026-10-02T19:00:05", meta=self.meta)
        HB.record("j", "2026-10-03T19:00:00", "failed", finished="2026-10-03T19:00:05", meta=self.meta)
        j = self.data()["jobs"]["j"]
        self.assertEqual(j["last_ok"], "2026-10-02T19:00:05")
        self.assertEqual(j["last_outcome"], "failed")

    def test_skipped_run_moves_no_clock(self) -> None:
        HB.record("j", "2026-10-02T19:00:00", "ok", finished="2026-10-02T19:00:05", meta=self.meta)
        HB.record("j", "2026-10-03T19:00:00", "skipped", finished="2026-10-03T19:00:01", meta=self.meta)
        j = self.data()["jobs"]["j"]
        self.assertEqual(j["last_ok"], "2026-10-02T19:00:05")
        self.assertEqual(j["last_outcome"], "ok")
        self.assertEqual(j["last_skipped"], "2026-10-03T19:00:01")
        self.assertEqual([r["outcome"] for r in self.data()["runs"]], ["ok", "skipped"])

    def test_unknown_outcome_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            HB.record("j", "2026-10-03T19:00:00", "maybe", meta=self.meta)

    def test_history_is_capped(self) -> None:
        with mock.patch.dict(HB.CFG["freshness"], {"run_history_keep": 3}):
            for i in range(5):
                HB.record("j", f"2026-10-03T19:00:0{i}", "ok",
                          finished=f"2026-10-03T19:00:1{i}", meta=self.meta)
        runs = self.data()["runs"]
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[-1]["finished"], "2026-10-03T19:00:14")

    def test_garbled_file_loads_empty(self) -> None:
        (self.meta / "heartbeats.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(HB.load(self.meta),
                         {"jobs": {}, "runs": [], "publish": {}, "publish_board": {}})
        (self.meta / "heartbeats.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(HB.load(self.meta),
                         {"jobs": {}, "runs": [], "publish": {}, "publish_board": {}})

    def test_concurrent_records_lose_nothing(self) -> None:
        procs = [subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "heartbeat.py"), "record",
             "--job", f"job{i}", "--started", "2026-10-03T19:00:00",
             "--outcome", "ok", "--meta", str(self.meta)]) for i in range(8)]
        for pr in procs:
            self.assertEqual(pr.wait(), 0)
        self.assertEqual(sorted(self.data()["jobs"]), [f"job{i}" for i in range(8)])
        self.assertEqual(len(self.data()["runs"]), 8)


class PublishRecordTest(HeartbeatCase):
    def test_unconfigured_then_ok_then_error(self) -> None:
        HB.record_publish(False, False, meta=self.meta)
        self.assertEqual(self.data()["publish"], {"configured": False})
        HB.record_publish(True, True, at="2026-10-03T12:12:00", meta=self.meta)
        self.assertEqual(self.data()["publish"],
                         {"configured": True, "last_ok": "2026-10-03T12:12:00"})
        HB.record_publish(True, False, error="OperationalError",
                          at="2026-10-03T13:12:00", meta=self.meta)
        pub = self.data()["publish"]
        self.assertEqual(pub["last_ok"], "2026-10-03T12:12:00")
        self.assertEqual(pub["last_error"], "OperationalError")
        self.assertEqual(pub["last_error_at"], "2026-10-03T13:12:00")

    def test_targets_are_independent(self) -> None:
        HB.record_publish(True, True, at="2026-10-03T12:12:00", meta=self.meta)
        HB.record_publish(True, False, error="OperationalError",
                          at="2026-10-03T12:13:00", meta=self.meta, target="publish_board")
        data = HB.load(self.meta)
        self.assertEqual(data["publish"], {"configured": True, "last_ok": "2026-10-03T12:12:00"})
        self.assertEqual(data["publish_board"],
                         {"configured": True, "last_error": "OperationalError",
                          "last_error_at": "2026-10-03T12:13:00"})
        HB.record_publish(True, True, at="2026-10-03T13:12:00", meta=self.meta,
                          target="publish_board")
        data = HB.load(self.meta)
        self.assertEqual(data["publish"]["last_ok"], "2026-10-03T12:12:00")
        self.assertEqual(data["publish_board"],
                         {"configured": True, "last_ok": "2026-10-03T13:12:00"})

    def test_a_missing_board_record_loads_as_empty(self) -> None:
        HB.record_publish(True, True, at="2026-10-03T12:12:00", meta=self.meta)
        self.assertEqual(HB.load(self.meta)["publish_board"], {})

    def test_an_unknown_target_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            HB.record_publish(True, True, meta=self.meta, target="publish_other")

    def test_unchanged_state_is_not_rewritten(self) -> None:
        HB.record_publish(False, False, meta=self.meta)
        before = (self.meta / "heartbeats.json").stat().st_mtime_ns
        HB.record_publish(False, False, meta=self.meta)
        self.assertEqual((self.meta / "heartbeats.json").stat().st_mtime_ns, before)


class ClockTest(unittest.TestCase):
    def test_dreamer_now_wins(self) -> None:
        with mock.patch.dict(os.environ, {"DREAMER_NOW": "2026-10-03T12:00:00",
                                          "DREAMER_TODAY": "2020-01-01"}):
            self.assertEqual(HB.clock(), _dt.datetime(2026, 10, 3, 12, 0, 0))

    def test_dreamer_today_sets_the_date_only(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "DREAMER_NOW"}
        env["DREAMER_TODAY"] = "2026-08-06"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(HB.clock().date(), _dt.date(2026, 8, 6))


class BashTrapTest(HeartbeatCase):
    """bin/_common.sh for real, with META pointed at the fixture."""

    def run_job(self, body: str) -> int:
        cmd = (f'JOB=testjob; source "{ROOT}/bin/_common.sh"; '
               f'META="{self.meta}"; LOGS="{self.meta}"; arm_heartbeat; {body}')
        env = dict(os.environ, DREAMER_PG_ENV="/nonexistent/postgres.env")
        return subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                              capture_output=True, text=True).returncode

    def test_clean_exit_records_ok_and_keeps_the_status(self) -> None:
        self.assertEqual(self.run_job("exit 0"), 0)
        j = self.data()["jobs"]["testjob"]
        self.assertEqual(j["last_outcome"], "ok")
        self.assertNotIn("last_produced", j)

    def test_nonzero_exit_records_failed_and_keeps_the_status(self) -> None:
        self.assertEqual(self.run_job("exit 3"), 3)
        self.assertEqual(self.data()["jobs"]["testjob"]["last_outcome"], "failed")

    def test_falling_off_the_end_records_ok(self) -> None:
        self.assertEqual(self.run_job("true"), 0)
        self.assertEqual(self.data()["jobs"]["testjob"]["last_outcome"], "ok")

    def test_mark_skipped_records_skipped(self) -> None:
        self.assertEqual(self.run_job("mark_skipped; exit 0"), 0)
        self.assertEqual(self.data()["runs"][-1]["outcome"], "skipped")
        self.assertNotIn("last_ok", self.data()["jobs"]["testjob"])

    def test_mark_produced_moves_last_produced(self) -> None:
        self.run_job("mark_produced; exit 0")
        self.assertIn("last_produced", self.data()["jobs"]["testjob"])

    def test_a_failed_exit_is_failed_even_when_marked_skipped(self) -> None:
        self.assertEqual(self.run_job("mark_skipped; exit 2"), 2)
        self.assertEqual(self.data()["jobs"]["testjob"]["last_outcome"], "failed")


class EveryJobArmsTest(unittest.TestCase):
    # verify.sh sources _common.sh for its helpers but is a manual check, not
    # a scheduled job: it has no schedule to be late against.
    EXEMPT = {"verify.sh", "_common.sh"}

    def test_every_job_that_sources_common_arms_the_heartbeat(self) -> None:
        missing = []
        for f in sorted((ROOT / "bin").glob("*.sh")):
            text = f.read_text(encoding="utf-8")
            sources = any(line.startswith("source ") and "_common.sh" in line
                          for line in text.splitlines())
            if f.name in self.EXEMPT or not sources:
                continue
            if "\narm_heartbeat\n" not in text:
                missing.append(f.name)
        self.assertEqual(missing, [], "these jobs ship without a heartbeat")


class WiringTest(HeartbeatCase):
    def test_the_trap_publishes_and_an_unconfigured_publisher_changes_nothing(self) -> None:
        cmd = (f'JOB=testjob; source "{ROOT}/bin/_common.sh"; META="{self.meta}"; '
               f'LOGS="{self.meta}"; arm_heartbeat; exit 4')
        env = dict(os.environ, DREAMER_PG_ENV="/nonexistent/postgres.env")
        rc = subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                            capture_output=True, text=True).returncode
        self.assertEqual(rc, 4)
        self.assertEqual(self.data()["publish"], {"configured": False})
        self.assertIn("publish: not configured",
                      (self.meta / "freshness-publish.log").read_text(encoding="utf-8"))

    def test_cron_publishes_hourly_after_the_watchdog(self) -> None:
        text = (ROOT / "bin" / "install-cron.sh").read_text(encoding="utf-8")
        line = [l for l in text.splitlines() if l.startswith("12 * * * *")][0]
        self.assertLess(line.index("healthcheck.py --watchdog"),
                        line.index("freshness_publish.py"))
        self.assertIn("logs/freshness-publish.log", line)

    def test_setup_script_creates_then_publishes(self) -> None:
        text = (ROOT / "bin" / "freshness-setup.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("--create-database"), text.index("--show"))

    def test_the_trap_runs_the_board_publisher_before_the_health_publisher(self) -> None:
        text = (ROOT / "bin" / "_common.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("scripts/board_publish.py"),
                        text.index("scripts/freshness_publish.py"))
        cmd = (f'JOB=testjob; source "{ROOT}/bin/_common.sh"; META="{self.meta}"; '
               f'LOGS="{self.meta}"; arm_heartbeat; exit 4')
        env = dict(os.environ, DREAMER_PG_ENV="/nonexistent/postgres.env")
        rc = subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                            capture_output=True, text=True).returncode
        self.assertEqual(rc, 4)
        self.assertEqual(self.data()["publish_board"], {"configured": False})
        self.assertIn("board: not configured",
                      (self.meta / "board-publish.log").read_text(encoding="utf-8"))

    def test_cron_publishes_the_board_between_the_watchdog_and_health(self) -> None:
        text = (ROOT / "bin" / "install-cron.sh").read_text(encoding="utf-8")
        line = [l for l in text.splitlines() if l.startswith("12 * * * *")][0]
        self.assertLess(line.index("healthcheck.py --watchdog"),
                        line.index("board_publish.py"))
        self.assertLess(line.index("board_publish.py"), line.index("freshness_publish.py"))
        self.assertIn("logs/board-publish.log", line)

    def test_setup_initialises_the_board_schema(self) -> None:
        text = (ROOT / "bin" / "freshness-setup.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("--create-database"), text.index("board_publish.py"))
        self.assertLess(text.index('board_publish.py" --init'),
                        text.index('freshness_publish.py" --init'))


class SignalTest(HeartbeatCase):
    """Bash runs the EXIT trap from its signal handler with $? still 0, so
    without a signal trap a killed job would be recorded as a clean run."""

    def test_a_terminated_job_is_recorded_failed_not_ok(self) -> None:
        cmd = (f'JOB=testjob; source "{ROOT}/bin/_common.sh"; META="{self.meta}"; '
               f'LOGS="{self.meta}"; arm_heartbeat; touch "{self.meta}/armed"; '
               f'sleep 1; exit 0')
        env = dict(os.environ, DREAMER_PG_ENV="/nonexistent/postgres.env")
        pr = subprocess.Popen(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not (self.meta / "armed").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pr.send_signal(signal.SIGTERM)
        self.assertEqual(pr.wait(timeout=20), 143)
        j = self.data()["jobs"]["testjob"]
        self.assertEqual(j["last_outcome"], "failed")
        self.assertNotIn("last_ok", j)


class ProducedTest(HeartbeatCase):
    """commit() for real, in a scratch repo: "produced" means vault CONTENT
    changed — not ledgers, not a regenerated digest."""

    def commit_change(self, relpath: str) -> dict:
        repo = self.meta / "repo"
        (repo / "vault").mkdir(parents=True)
        (repo / "scripts").symlink_to(ROOT / "scripts")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
        subprocess.run(git + ["init", "-q"], check=True)
        (repo / "logs").mkdir()
        (repo / "logs" / ".keep").write_text("", encoding="utf-8")
        (repo / "vault" / ".keep").write_text("", encoding="utf-8")
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
        cmd = (f'JOB=testjob; source "{ROOT}/bin/_common.sh"; ROOT="{repo}"; '
               f'META="{self.meta}"; LOGS="{self.meta}"; regen_dashboard() {{ :; }}; '
               f'arm_heartbeat; commit "test commit"; exit 0')
        env = dict(os.environ, DREAMER_PG_ENV="/nonexistent/postgres.env")
        done = subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("committed: test commit", done.stdout)
        return self.data()["jobs"]["testjob"]

    def test_a_loop_page_change_is_produced(self) -> None:
        self.assertIn("last_produced", self.commit_change("vault/loops/L0001.md"))

    def test_a_ledger_only_commit_is_not_produced(self) -> None:
        self.assertNotIn("last_produced",
                         self.commit_change("vault/.vault-meta/run-state.json"))

    def test_a_regenerated_digest_is_not_produced(self) -> None:
        self.assertNotIn("last_produced", self.commit_change("vault/digests/2026-40.md"))


class TrapSafetyTest(unittest.TestCase):
    def test_the_publisher_does_not_inherit_the_vault_lock(self) -> None:
        text = (ROOT / "bin" / "_common.sh").read_text(encoding="utf-8")
        line = [l for l in text.splitlines() if "freshness-publish.log" in l][0]
        self.assertIn("9>&-", line)

    def test_the_board_publisher_does_not_inherit_the_vault_lock(self) -> None:
        text = (ROOT / "bin" / "_common.sh").read_text(encoding="utf-8")
        line = [l for l in text.splitlines() if "board-publish.log" in l][0]
        self.assertIn("9>&-", line)


if __name__ == "__main__":
    unittest.main()
