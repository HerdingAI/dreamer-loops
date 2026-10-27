#!/usr/bin/env python3
"""bin/frontier.sh in a throwaway copy of the repo, with every python step
replaced by a stub that logs its call and prints a scripted JSON status, plus the
acquire_lock wait (locks.cron_wait_seconds). Nothing reaches the real vault or the network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STUB = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(__file__).stem
args = sys.argv[1:]
key = name + (":" + args[0] if args else "")
with open(os.environ["STUB_ORDER"], "a") as fh:
    fh.write(key + "\\n")
spec = json.load(open(os.environ["STUB_SPEC"])).get(key, {})
if spec.get("rc"):
    sys.exit(spec["rc"])
DEFAULT = {"frontier_map": {"status": "done", "mapped": 1}, "frontier_gaps": {"goals": []},
           "frontier_lessons": {"results": [], "stopped": False}}
print(json.dumps(spec.get("out", DEFAULT.get(name, {"status": "ok"}))))
'''

STEPS = ("frontier_sync", "frontier_map", "frontier_gaps", "frontier_lessons", "frontier_publish")


def sh(cmd, cwd, env=None, **kw):
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-fsh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for d in ("bin", "scripts", "skills"):
            shutil.copytree(ROOT / d, self.repo / d, ignore=shutil.ignore_patterns("__pycache__"))
        (self.repo / "config.yaml").write_text((ROOT / "config.yaml").read_text())
        for sub in (".vault-meta", "loops", "goals", "frontier/items"):
            (self.repo / "vault" / sub).mkdir(parents=True)
        (self.repo / "logs").mkdir()
        (self.repo / ".gitignore").write_text("logs/.*\n__pycache__/\n")
        sh(["git", "init", "-q"], self.repo)
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], self.repo)
        stub = self.tmp / "stubbin"
        stub.mkdir()
        self.order = self.tmp / "order.log"
        (stub / "qmd").write_text(f'#!/bin/sh\necho "qmd:$1" >> "{self.order}"\nexit 0\n')
        (stub / "qmd").chmod(0o755)
        self.spec = self.tmp / "spec.json"
        self.spec.write_text("{}")
        self.env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "STUB_ORDER": str(self.order),
                    "STUB_SPEC": str(self.spec), "DREAMER_PG_ENV": str(self.tmp / "none.env"),
                    "DREAMER_TODAY": "2030-01-15", "HOME": str(self.tmp)}
        for k in ("DREAMER_LOCK_HELD", "DREAMER_FAKE_CLAUDE"):
            self.env.pop(k, None)


class FrontierShTest(Base):
    def setUp(self):
        super().setUp()
        for name in STEPS:
            (self.repo / "scripts" / f"{name}.py").write_text(STUB)
        # the health spine has its own tests; here it must neither add events nor unblock legs
        (self.repo / "scripts" / "healthcheck.py").write_text("#!/usr/bin/env python3\n")
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "stubs"], self.repo)

    def run_job(self, spec=None):
        self.spec.write_text(json.dumps(spec or {}))
        self.order.write_text("")
        return sh(["bash", str(self.repo / "bin" / "frontier.sh")], self.repo, self.env, timeout=120)

    def calls(self):
        return [l for l in self.order.read_text().splitlines() if not l.startswith("qmd:") or l == "qmd:update"]

    def events(self):
        path = self.repo / "vault/.vault-meta/run-state.json"
        return json.loads(path.read_text()).get("events", []) if path.exists() else []

    def test_steps_run_in_the_flowcharts_order(self):
        r = self.run_job()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), [
            "frontier_sync:pull", "qmd:update", "frontier_map:run-batch", "frontier_gaps:run",
            "frontier_lessons:run", "frontier_sync:push", "qmd:update", "frontier_publish"])
        self.assertEqual([e for e in self.events() if e["job"] == "frontier"], [])

    def test_a_failing_map_step_still_pulls_pushes_and_publishes_and_records_degraded(self):
        r = self.run_job({"frontier_map:run-batch": {"rc": 1}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        for want in ("frontier_sync:pull", "frontier_sync:push", "frontier_publish", "frontier_gaps:run",
                     "frontier_lessons:run"):
            self.assertIn(want, calls)
        details = [e["detail"] for e in self.events() if e["job"] == "frontier" and e["kind"] == "degraded"]
        self.assertEqual(len(details), 1, details)
        self.assertIn("map batch failed", details[0])

    def test_a_degraded_status_records_an_event_and_the_run_continues(self):
        r = self.run_job({"frontier_sync:push": {"out": {"status": "show unreachable"}}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        details = [e["detail"] for e in self.events() if e["job"] == "frontier"]
        self.assertEqual(details, ["frontier push: show unreachable"])
        self.assertIn("frontier_publish", self.calls())

    def test_conflicts_and_rejections_are_degraded_too(self):
        self.run_job({"frontier_sync:push": {"out": {"status": "ok", "conflicts_new": ["K7001-1"], "rejected": []}}})
        self.assertIn("1 conflict(s)", [e["detail"] for e in self.events()][0])

    def test_a_reused_tap_id_is_degraded(self):
        self.run_job({"frontier_sync:pull": {"out": {"status": "ok", "taps_reused": [
            {"id": 5, "lesson": "K7001-1", "reason": "the show reused tap id"}]}}})
        self.assertEqual([e["detail"] for e in self.events() if e["job"] == "frontier"],
                         ["frontier pull: 1 reused tap id(s)"])

    def test_a_routine_lesson_skip_is_quiet_and_a_cost_ceiling_skip_is_reported(self):
        """Skipping a lesson whose goal paused or whose item moved is the queue working, not a
        fault (one that ran out of attempts records its own event). Rule 9: a run that skips
        research for the cost ceiling says so in the digest."""
        self.run_job({"frontier_lessons:run": {"out": {"results": [
            {"key": "K7001", "status": "skipped", "reason": "goal is not active"},
            {"key": "K7002", "status": "rejected"}], "stopped": False, "skipped": None}}})
        self.assertEqual([e for e in self.events() if e["job"] == "frontier"], [])
        self.run_job({"frontier_lessons:run": {"out": {"results": [], "stopped": False,
                                                       "skipped": "cost ceiling"}}})
        details = [e["detail"] for e in self.events() if e["job"] == "frontier"]
        self.assertEqual(details, ["frontier lessons: research skipped (cost ceiling)"])

    def test_not_configured_is_logged_not_evented(self):
        r = self.run_job({"frontier_sync:pull": {"out": {"status": "not configured"}},
                          "frontier_sync:push": {"out": {"status": "not configured"}}})
        self.assertEqual(self.events(), [])
        self.assertIn("frontier_publish", self.calls())

    def test_a_disabled_show_is_quiet_and_the_run_completes(self):
        r = self.run_job({"frontier_sync:pull": {"out": {"status": "disabled"}},
                          "frontier_sync:push": {"out": {"status": "disabled", "known": "skipped"}}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([e for e in self.events() if e["job"] == "frontier"], [])
        self.assertIn("frontier_publish", self.calls())

    def test_the_real_sync_with_an_empty_show_url_reads_disabled_end_to_end(self):
        """Not a stub: frontier_sync.py itself, with the shipped (empty) show_url. No token file
        exists, and none is needed."""
        # frontier_sync imports modules the other tests stub out, so the real scripts run from a
        # sibling directory and only the sync entry point is a shim onto them
        shutil.copytree(ROOT / "scripts", self.repo / "real_scripts", ignore=shutil.ignore_patterns("__pycache__"))
        (self.repo / "scripts" / "frontier_sync.py").write_text(
            "import runpy, sys\nfrom pathlib import Path\n"
            "runpy.run_path(str(Path(__file__).resolve().parents[1] / 'real_scripts' / 'frontier_sync.py'),"
            " run_name='__main__')\n")
        r = self.run_job()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count('"status": "disabled"'), 2, r.stdout)
        self.assertEqual([e for e in self.events() if e["job"] == "frontier"], [])

    def test_a_fresh_clone_with_no_frontier_collection_reports_degraded_and_exits_cleanly(self):
        """No `frontier` qmd collection built yet: qmd cannot refresh it. The map batch is
        skipped, the event says so, and the run still finishes with rc 0."""
        (self.tmp / "stubbin" / "qmd").write_text('#!/bin/sh\necho "Collection not found: frontier" >&2\nexit 1\n')
        r = self.run_job()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("frontier_map:run-batch", self.calls())
        details = [e["detail"] for e in self.events() if e["kind"] == "degraded"]
        self.assertTrue(any("frontier" in d for d in details), details)

    def test_a_usage_limit_stops_model_steps_but_not_push_or_publish(self):
        r = self.run_job({"frontier_map:run-batch": {"out": {"status": "stopped"}}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.calls()
        self.assertNotIn("frontier_gaps:run", calls)
        self.assertNotIn("frontier_lessons:run", calls)
        self.assertIn("frontier_sync:push", calls)
        self.assertIn("frontier_publish", calls)
        self.assertEqual([e for e in self.events() if e["job"] == "frontier"], [])
        self.assertIn("usage limit", r.stdout)

    def test_a_usage_limit_in_lessons_still_pushes(self):
        self.run_job({"frontier_lessons:run": {"out": {"results": [], "stopped": True}}})
        self.assertEqual(self.calls()[-3:], ["frontier_sync:push", "qmd:update", "frontier_publish"])

    def test_a_busy_wiki_lock_defers_that_step_and_the_rest_run(self):
        r = self.run_job({"frontier_sync:pull": {"rc": 2}})
        self.assertEqual(r.returncode, 0)
        self.assertIn("deferred", r.stdout)
        self.assertEqual(self.events(), [])
        self.assertIn("frontier_map:run-batch", self.calls())

    def test_a_failed_index_refresh_skips_the_map_batch_only(self):
        (self.tmp / "stubbin" / "qmd").write_text('#!/bin/sh\n[ "$1" = update ] && exit 1\nexit 0\n')
        self.run_job()
        calls = self.calls()
        self.assertNotIn("frontier_map:run-batch", calls)
        self.assertIn("frontier_sync:push", calls)

    def test_the_frontier_lock_serialises_runs(self):
        import fcntl
        lock = open(self.repo / "vault/.vault-meta/frontier.lock", "a")
        self.addCleanup(lock.close)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        r = self.run_job()
        self.assertEqual(r.returncode, 0)
        self.assertIn("another frontier run holds", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_blocked_frontier_leg_skips_cleanly(self):
        (self.repo / "vault/.vault-meta/run-state.json").write_text(
            json.dumps({"health": {"blocked_legs": ["frontier"]}}))
        r = self.run_job()
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("frontier_sync:pull", self.calls())

    def test_cron_chains_the_frontier_job_after_the_assessor_with_a_semicolon(self):
        text = (ROOT / "bin" / "install-cron.sh").read_text()
        line = [l for l in text.splitlines() if l.startswith("0 19 * * *")][0]
        self.assertLess(line.index("nightly-extract.sh"), line.index("nightly-assess.sh"))
        self.assertLess(line.index("nightly-assess.sh"), line.index("frontier.sh"))
        tail = line[line.index("nightly-assess.sh"):line.index("frontier.sh")]
        self.assertIn("; flock", tail)       # a failed assessor must not stop it
        self.assertNotIn("&&", line)
        self.assertIn("logs/frontier.log", line)

    def test_the_cron_block_has_no_command_substitution(self):
        # The heredoc is unquoted so $ROOT and $HOME expand; a backtick or $( in a
        # comment there runs as a command at install time (and would run at install).
        text = (ROOT / "bin" / "install-cron.sh").read_text()
        block = text[text.index("<<EOF"):text.index("\nEOF", text.index("<<EOF"))]
        self.assertNotIn("`", block)
        self.assertNotIn("$(", block)


class AcquireLockWaitTest(Base):
    def holder(self, seconds):
        code = ("import fcntl,sys,time\nfh=open(sys.argv[1],'a')\nfcntl.flock(fh,fcntl.LOCK_EX)\n"
                "print('held',flush=True)\ntime.sleep(float(sys.argv[2]))\n")
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.repo / "vault/.vault-meta/wiki.lock"),
                                 str(seconds)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def set_wait(self, value):
        cfg = self.repo / "config.yaml"
        cfg.write_text(cfg.read_text().replace("cron_wait_seconds: 120", f"cron_wait_seconds: {value}"))

    def job(self):
        cmd = ('JOB=probe; source bin/_common.sh; acquire_lock; '
               '[[ "${DREAMER_LOCK_HELD:-}" == 1 ]] && echo GOT_LOCK')
        started = time.monotonic()
        r = sh(["bash", "-c", cmd], self.repo, self.env, timeout=60)
        return r, time.monotonic() - started

    def test_a_job_started_inside_a_short_hold_waits_and_runs(self):
        self.set_wait(8)
        self.holder(1.2)
        r, took = self.job()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("GOT_LOCK", r.stdout)
        self.assertGreater(took, 0.8)

    def test_a_job_blocked_past_the_wait_skips_cleanly(self):
        self.set_wait(1)
        self.holder(8)
        r, took = self.job()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("GOT_LOCK", r.stdout)
        self.assertIn("locked by another job", r.stdout)
        self.assertGreater(took, 0.8)

    def test_a_missing_key_is_fatal_not_a_silent_no_wait(self):
        cfg = self.repo / "config.yaml"
        cfg.write_text(cfg.read_text().replace("cron_wait_seconds: 120", "# gone"))
        r, _ = self.job()
        self.assertEqual(r.returncode, 1)
        self.assertIn("locks.cron_wait_seconds", r.stdout)
        self.assertNotIn("GOT_LOCK", r.stdout)


class CommitLockedWaitTest(Base):
    def run_commit(self):
        cmd = ('JOB=probe; source bin/_common.sh; commit() { echo COMMITTED; }; '
               'commit_locked "msg"; echo "rc=$?"')
        return sh(["bash", "-c", cmd], self.repo, self.env, timeout=60)

    def test_a_missing_wait_key_is_a_logged_failure_not_a_silent_20_seconds(self):
        cfg = self.repo / "config.yaml"
        cfg.write_text(cfg.read_text().replace("vault_lock_wait_seconds:", "gone_lock_wait_seconds:"))
        r = self.run_commit()
        self.assertIn("frontier.vault_lock_wait_seconds", r.stdout)
        self.assertIn("rc=1", r.stdout)
        self.assertNotIn("COMMITTED", r.stdout)

    def test_a_configured_wait_commits(self):
        r = self.run_commit()
        self.assertIn("COMMITTED", r.stdout)
        self.assertIn("rc=0", r.stdout)


if __name__ == "__main__":
    unittest.main()
