#!/usr/bin/env python3
"""U2 — health spine skeleton; U3 — the launch assertion set.

Covers: assertion severity semantics (info / degraded / blocking), the blocked-
leg record, event-channel wiring, exception isolation between assertions, the
watchdog staleness check, run-state write safety under the vault lock, the
bash-level `leg_blocked` helper, the `qmd status` parsers, and the six launch
assertions (embeddings-current, index-fresh, collections-covered,
vocabulary-applied, cost-records-sane, gates-current).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
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
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import healthcheck as H  # noqa: E402


class HealthCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-health-"))
        for n in ("meta", "logs"):
            (self.tmp / n).mkdir(parents=True, exist_ok=True)
        self._orig = dict(dc.CFG["paths"])
        dc.CFG["paths"].update({"meta": str(self.tmp / "meta"),
                                "logs": str(self.tmp / "logs")})
        self.state_path = self.tmp / "meta" / "run-state.json"

    def tearDown(self) -> None:
        dc.CFG["paths"].clear(); dc.CFG["paths"].update(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_state(self, d: dict) -> None:
        self.state_path.write_text(json.dumps(d), encoding="utf-8")


class AssertionSemanticsTest(HealthCase):
    def test_blocking_failure_blocks_the_leg_and_events(self) -> None:
        reg = [("qmd-reachable", "blocking", ("research",),
                lambda: (False, "qmd not on PATH"))]
        record = H.run(reg)
        self.assertEqual(record["blocked_legs"], ["research"])
        st = self.state()
        self.assertEqual(st["health"]["blocked_legs"], ["research"])
        events = st.get("events") or []
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "degraded")
        self.assertIn("qmd-reachable", events[0]["detail"])
        self.assertIn("research", events[0]["detail"])

    def test_degraded_failure_events_but_blocks_nothing(self) -> None:
        reg = [("recent-commit", "degraded", (),
                lambda: (False, "no job commit in 3 days"))]
        record = H.run(reg)
        self.assertEqual(record["blocked_legs"], [])
        events = self.state().get("events") or []
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "degraded")
        self.assertIn("recent-commit", events[0]["detail"])

    def test_info_result_is_recorded_with_no_event(self) -> None:
        reg = [("disk-free", "info", (), lambda: (False, "87% used"))]
        record = H.run(reg)
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertEqual(row["detail"], "87% used")
        st = self.state()
        self.assertIn("health", st)
        self.assertFalse(st.get("events"), "info must never reach the event channel")

    def test_exception_reports_failure_and_later_assertions_still_run(self) -> None:
        def boom():
            raise RuntimeError("state file unreachable")
        reg = [("broken", "degraded", (), boom),
               ("after", "info", (), lambda: True)]
        record = H.run(reg)
        rows = record["assertions"]
        self.assertEqual(len(rows), 2, "a broken assertion must not suppress the rest")
        self.assertFalse(rows[0]["ok"])
        self.assertIn("state file unreachable", rows[0]["detail"])
        self.assertTrue(rows[1]["ok"])

    def test_main_json_prints_the_record(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = H.main(["--json"])
        self.assertEqual(rc, 0)
        rec = json.loads(buf.getvalue())
        self.assertIn("checked_at", rec)
        self.assertIn("blocked_legs", rec)
        self.assertIn("assertions", rec)


class WriteSafetyTest(HealthCase):
    def test_healthy_run_touches_only_the_health_key(self) -> None:
        before = {"costs": [{"at": "x", "job": "j", "cost": 1.0}],
                  "events": [{"at": "x", "job": "j", "kind": "deferral", "detail": "d"}],
                  "deferrals": [{"at": "x", "exit_code": 1, "job": "j"}],
                  "skip_research_next_run": False}
        self.write_state(before)
        H.run([("fine", "blocking", ("research",), lambda: True)])
        st = self.state()
        health = st.pop("health")
        self.assertEqual(st, before, "healthcheck must not mutate costs/events/deferrals")
        self.assertEqual(health["blocked_legs"], [])

    def test_a_write_landing_before_the_lock_is_not_dropped(self) -> None:
        """What this proves: healthcheck reads run-state INSIDE the locked
        section, so a cost recorded by a job that released wiki.lock just
        before healthcheck acquired it is re-read and preserved. Writers that
        used to skip the lock entirely (ingest-cc.sh's record_* calls were the
        live counterexample) are covered by the racing test below — the
        record_* helpers now take the same lock."""
        self.write_state({"costs": []})
        orig = H.vault_lock
        state_path = self.state_path

        @contextlib.contextmanager
        def inject_then_yield(**kw):
            with orig(**kw):
                st = json.loads(state_path.read_text(encoding="utf-8"))
                st.setdefault("costs", []).append(
                    {"at": "race", "job": "other", "cost": 2.5})
                state_path.write_text(json.dumps(st), encoding="utf-8")
                yield

        H.vault_lock = inject_then_yield
        try:
            H.run([])
        finally:
            H.vault_lock = orig
        st = self.state()
        self.assertEqual([c["job"] for c in st["costs"]], ["other"],
                         "the concurrently recorded cost was dropped")
        self.assertIn("health", st)

    def test_unlocked_record_cost_racing_write_health_loses_nothing(self) -> None:
        """FIX: unlocked record_* writers raced the locked healthcheck writer
        (ingest-cc.sh was the live counterexample — it holds no lock). The
        race is provoked deterministically: while write_health sits between
        its read and its replace (inside the lock), an unlocked-context
        record_cost is launched. It must WAIT for the lock and land after,
        so neither the cost nor the health record is lost."""
        self.write_state({"costs": []})
        costfile = self.tmp / "cost.txt"
        costfile.write_text("2.5", encoding="utf-8")
        cmd = (f'source "{ROOT}/bin/_common.sh"; META="{self.tmp}/meta"; '
               f'record_cost "{costfile}"')
        env = {k: v for k, v in os.environ.items() if k != "DREAMER_LOCK_HELD"}
        env["JOB"] = "racer"
        procs: list[subprocess.Popen] = []
        real = dc.read_json_strict  # the read write_health merges from
        fired: list[int] = []

        def racing_read(path, default=None):
            st = real(path, default=default)
            if not fired:  # only the write_health read triggers the race
                fired.append(1)
                procs.append(subprocess.Popen(
                    ["bash", "-c", cmd], cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                time.sleep(2.0)  # let the racer reach (and wait on) the lock
            return st

        H.read_json_strict = racing_read
        try:
            H.run([])
        finally:
            H.read_json_strict = real
        self.assertEqual(len(procs), 1, "the race was never provoked")
        out, err = procs[0].communicate(timeout=15)
        self.assertEqual(procs[0].returncode, 0, err.decode())
        st = self.state()
        self.assertIn("health", st, "the health record was lost in the race")
        self.assertEqual([c["cost"] for c in st.get("costs") or []], [2.5],
                         "the unlocked record_cost write was dropped")

    def test_record_call_from_a_lock_holding_job_does_not_deadlock(self) -> None:
        """Jobs call record_* while holding wiki.lock on fd 9 (acquire_lock).
        flock is per open-file-description, so a child python re-flocking the
        same path would block against its own parent. acquire_lock therefore
        exports DREAMER_LOCK_HELD=1 and the helpers skip their own flock when
        it is set — the parent's held lock already serializes the write."""
        self.write_state({})
        cmd = (f'source "{ROOT}/bin/_common.sh"; META="{self.tmp}/meta"; '
               f'LOCKFILE="$META/wiki.lock"; acquire_lock; '
               f'[[ "${{DREAMER_LOCK_HELD:-}}" == "1" ]] || exit 90; '
               f'record_event probe "written while the parent holds the lock"')
        env = dict(os.environ, JOB="locked-job")
        env.pop("DREAMER_LOCK_HELD", None)
        proc = subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=15)
        self.assertNotEqual(proc.returncode, 90,
                            "acquire_lock must export DREAMER_LOCK_HELD=1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = self.state().get("events") or []
        self.assertEqual([e["kind"] for e in events], ["probe"],
                         "record_event under a held parent lock lost the event")
        self.assertEqual(events[0]["job"], "locked-job")


class CorruptStateTest(HealthCase):
    def test_write_health_refuses_when_run_state_is_unparseable(self) -> None:
        """run-state.json exists but is not JSON: healthcheck must exit
        non-zero WITHOUT writing — defaulting to {} would wipe every cost,
        event, and deferral the file carried."""
        garbage = "{not json"
        self.state_path.write_text(garbage, encoding="utf-8")
        saved = H.ASSERTIONS[:]
        H.ASSERTIONS[:] = [("fine", "info", (), lambda: True)]
        try:
            rc = H.main([])
        finally:
            H.ASSERTIONS[:] = saved
        self.assertEqual(rc, 1, "corrupt state must be a loud non-zero")
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), garbage,
                         "healthcheck must never overwrite unparseable state")


class EventDedupTest(HealthCase):
    """FIX: healthcheck runs several times a night (night-cycle runs it twice
    per cycle) but the digest that drains events is weekly — an undeduped
    failing assertion stacked identical lines, mirroring watchdog()'s dedup."""

    def test_repeated_identical_failures_append_one_event(self) -> None:
        reg = [("stale-index", "degraded", (), lambda: (False, "same detail"))]
        H.run(reg)
        H.run(reg)
        H.run(reg)
        events = self.state().get("events") or []
        self.assertEqual(len(events), 1,
                         "repeated evaluations must not stack duplicates")

    def test_a_different_detail_is_a_new_event(self) -> None:
        H.run([("stale-index", "degraded", (), lambda: (False, "detail A"))])
        H.run([("stale-index", "degraded", (), lambda: (False, "detail B"))])
        details = [e["detail"] for e in self.state().get("events") or []]
        self.assertEqual(len(details), 2, details)
        self.assertNotEqual(details[0], details[1])


class WatchdogTest(HealthCase):
    def _stale_state(self) -> None:
        old = (_dt.datetime.now() - _dt.timedelta(hours=999)) \
            .isoformat(timespec="seconds")
        self.write_state({"health": {"checked_at": old,
                                     "blocked_legs": [], "assertions": []}})

    def test_stale_health_fires_event_and_log_line(self) -> None:
        self._stale_state()
        self.assertEqual(H.watchdog(), 0)
        events = [e for e in self.state().get("events", [])
                  if str(e.get("detail", "")).startswith("watchdog:")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "degraded")
        logf = self.tmp / "logs" / "watchdog.log"
        self.assertTrue(logf.exists(), "watchdog must log outside the pipeline")
        self.assertIn("watchdog:", logf.read_text(encoding="utf-8"))

    def test_never_checked_counts_as_stale(self) -> None:
        self.write_state({})
        H.watchdog()
        events = self.state().get("events") or []
        self.assertTrue(any("never" in e["detail"] for e in events))

    def test_fresh_health_is_quiet(self) -> None:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        self.write_state({"health": {"checked_at": now,
                                     "blocked_legs": [], "assertions": []}})
        self.assertEqual(H.watchdog(), 0)
        self.assertFalse(self.state().get("events"))
        self.assertFalse((self.tmp / "logs" / "watchdog.log").exists())

    def test_repeat_firings_do_not_stack_events(self) -> None:
        self._stale_state()
        H.watchdog()
        H.watchdog()
        events = [e for e in self.state().get("events", [])
                  if str(e.get("detail", "")).startswith("watchdog:")]
        self.assertEqual(len(events), 1, "hourly cron must not stack duplicates")
        # ...but the out-of-band log keeps every firing.
        text = (self.tmp / "logs" / "watchdog.log").read_text(encoding="utf-8")
        self.assertEqual(text.count("watchdog:"), 2)

    def test_watchdog_never_runs_the_assertion_registry(self) -> None:
        called: list[int] = []
        H.ASSERTIONS.append(("spy", "info", (), lambda: called.append(1) or True))
        try:
            self._stale_state()
            H.watchdog()
        finally:
            H.ASSERTIONS[:] = [a for a in H.ASSERTIONS if a[0] != "spy"]
        self.assertEqual(called, [], "the watchdog must be cheap and independent")


class ConfigTest(HealthCase):
    def test_config_declares_the_window(self) -> None:
        self.assertIsInstance(
            (dc.CFG.get("health") or {}).get("checked_max_age_hours"), int,
            "config.yaml must carry health.checked_max_age_hours")

    def test_missing_health_key_fails_loudly_by_name(self) -> None:
        saved = dc.CFG.pop("health", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                H.health_cfg("checked_max_age_hours")
            self.assertIn("health.checked_max_age_hours", str(cm.exception))
        finally:
            if saved is not None:
                dc.CFG["health"] = saved


class BashHelperTest(HealthCase):
    """bin/_common.sh leg_blocked, run for real with META pointed at the
    fixture. Sourcing _common.sh only sets up variables (no lock is taken)."""

    def _leg_blocked(self, leg: str) -> int:
        cmd = (f'source "{ROOT}/bin/_common.sh"; '
               f'META="{self.tmp}/meta"; leg_blocked {leg}')
        return subprocess.run(["bash", "-c", cmd], cwd=str(ROOT),
                              capture_output=True, text=True).returncode

    def test_returns_0_when_the_leg_is_blocked(self) -> None:
        self.write_state({"health": {"checked_at": "2026-08-15T00:00:00",
                                     "blocked_legs": ["research"],
                                     "assertions": []}})
        self.assertEqual(self._leg_blocked("research"), 0)

    def test_returns_1_when_not_blocked_or_no_record(self) -> None:
        self.write_state({"health": {"checked_at": "2026-08-15T00:00:00",
                                     "blocked_legs": [], "assertions": []}})
        self.assertEqual(self._leg_blocked("research"), 1)
        self.state_path.unlink()
        self.assertEqual(self._leg_blocked("research"), 1)


# --------------------------------------------------------------------------
# U3 — launch assertions
# --------------------------------------------------------------------------

# Synthetic sample matching the real `qmd status` line SHAPES the parsers
# depend on; every identifying value (path, size, counts) is invented (models /
# examples / tips sections trimmed — the parsers must not need them).
QMD_STATUS_SAMPLE = """\
QMD Status

Index: /home/user/dreamer/.qmd/index.sqlite
Size:  12.3 MB

Documents
  Total:    100 files indexed
  Vectors:  5000 embedded
  Pending:  7 need embedding (run 'qmd embed')
  Updated:  36m ago

AST Chunking
  Status:   active
  Languages: typescript, tsx, javascript, python, go, rust

Collections
  wisdom (qmd://wisdom/)
    Pattern:  **/*.md
    Files:    42 (updated 14d ago)
  vault (qmd://vault/)
    Pattern:  **/*.md
    Files:    30 (updated 36m ago)
  transcripts (qmd://transcripts/)
    Pattern:  **/*.md
    Files:    20 (updated 6h ago)
  conclusions (qmd://conclusions/)
    Pattern:  **/*.md
    Files:    8 (updated 36m ago)

Examples
  # List files in a collection
  qmd ls wisdom
"""

# Same shape with zero backlog and every collection fresh.
QMD_STATUS_FRESH = QMD_STATUS_SAMPLE \
    .replace("  Pending:  7 need embedding (run 'qmd embed')\n", "") \
    .replace("updated 14d ago", "updated 2h ago") \
    .replace("updated 6h ago", "updated 2h ago")

# Same shape with a backlog far over health.embed_pending_max.
QMD_STATUS_BACKLOG = QMD_STATUS_SAMPLE.replace(
    "  Pending:  7 need embedding (run 'qmd embed')\n",
    "  Pending:  9001 need embedding (run 'qmd embed')\n")

QMD_STATUS_MANGLED = "qmd 3.0.0 — status output has been redesigned\nOK\n"

# Fresh everywhere, but the configured `wisdom` collection has vanished from
# the report entirely — the hardening case for assert_index_fresh.
QMD_STATUS_MISSING_WISDOM = QMD_STATUS_FRESH.replace(
    """  wisdom (qmd://wisdom/)
    Pattern:  **/*.md
    Files:    42 (updated 2h ago)
""", "")


def _row(name: str) -> tuple:
    for row in H.ASSERTIONS:
        if row[0] == name:
            return row
    raise AssertionError(f"assertion {name!r} is not registered")


class LaunchCase(HealthCase):
    """HealthCase plus a loops dir and a stubbed qmd supplier."""

    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "loops").mkdir()
        dc.CFG["paths"]["loops"] = str(self.tmp / "loops")
        self._orig_qmd = H.qmd_status_text
        H._QMD_CACHE.clear()

    def tearDown(self) -> None:
        H.qmd_status_text = self._orig_qmd
        H._QMD_CACHE.clear()
        super().tearDown()

    def stub_qmd(self, text: str) -> None:
        H.qmd_status_text = lambda: text


class RegistryShapeTest(LaunchCase):
    def test_the_six_launch_assertions_and_their_severities(self) -> None:
        got = {name: (sev, blocks) for name, sev, blocks, _ in H.ASSERTIONS}
        want = {"embeddings-current": ("degraded", ()),
                "index-fresh": ("blocking", ("research",)),
                "collections-covered": ("degraded", ()),
                "vocabulary-applied": ("degraded", ()),
                "cost-records-sane": ("degraded", ()),
                "gates-current": ("info", ())}
        for name, shape in want.items():
            self.assertEqual(got.get(name), shape, name)


class QmdParserTest(LaunchCase):
    def test_pending_count_extracted(self) -> None:
        self.assertEqual(H.parse_pending_embeds(QMD_STATUS_SAMPLE), 7)

    def test_no_pending_line_with_documents_section_means_zero(self) -> None:
        self.assertEqual(H.parse_pending_embeds(QMD_STATUS_FRESH), 0)

    def test_collection_ages_in_hours(self) -> None:
        ages = H.parse_collection_ages(QMD_STATUS_SAMPLE)
        self.assertEqual(set(ages), {"wisdom", "vault", "transcripts",
                                     "conclusions"})
        self.assertAlmostEqual(ages["wisdom"], 14 * 24.0)
        self.assertAlmostEqual(ages["vault"], 0.6)
        self.assertAlmostEqual(ages["transcripts"], 6.0)

    def test_mangled_output_raises_never_passes(self) -> None:
        for fn in (H.parse_pending_embeds, H.parse_collection_ages):
            with self.assertRaises(ValueError) as cm:
                fn(QMD_STATUS_MANGLED)
            self.assertIn("qmd status output not understood", str(cm.exception))


class EmbeddingsCurrentTest(LaunchCase):
    def test_backlog_over_threshold_fails_degraded(self) -> None:
        self.stub_qmd(QMD_STATUS_BACKLOG)  # 9001 pending vs max 50
        record = H.run([_row("embeddings-current")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("9001", row["detail"])
        self.assertEqual(record["blocked_legs"], [])
        events = self.state().get("events") or []
        self.assertEqual([e["kind"] for e in events], ["degraded"])

    def test_zero_backlog_passes(self) -> None:
        self.stub_qmd(QMD_STATUS_FRESH)
        record = H.run([_row("embeddings-current")])
        self.assertTrue(record["assertions"][0]["ok"])
        self.assertFalse(self.state().get("events"))


class IndexFreshTest(LaunchCase):
    def test_stale_collection_blocks_research(self) -> None:
        self.stub_qmd(QMD_STATUS_SAMPLE)  # wisdom at 14d vs 30h window
        record = H.run([_row("index-fresh")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("wisdom", row["detail"])
        self.assertEqual(record["blocked_legs"], ["research"])

    def test_fresh_collections_pass_and_block_nothing(self) -> None:
        self.stub_qmd(QMD_STATUS_FRESH)
        record = H.run([_row("index-fresh")])
        self.assertTrue(record["assertions"][0]["ok"])
        self.assertEqual(record["blocked_legs"], [])

    # -- last_reindex rescue: qmd's per-collection age tracks content change,
    # not last scan, so a healthy STATIC collection (wisdom at "14d ago") would
    # otherwise permanently block research. A recent successful reindex that
    # covered the collection proves the index was scanned, so it counts fresh.

    def _reindex_at(self, at: str, collections: list[str]) -> None:
        self.write_state({"last_reindex": {"collections": collections,
                                           "at": at}})

    def test_stale_qmd_age_covered_by_recent_reindex_passes(self) -> None:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        self._reindex_at(now, ["vault", "transcripts", "conclusions", "wisdom"])
        self.stub_qmd(QMD_STATUS_SAMPLE)  # wisdom at 14d vs 30h window
        record = H.run([_row("index-fresh")])
        row = record["assertions"][0]
        self.assertTrue(row["ok"], row["detail"])
        self.assertEqual(record["blocked_legs"], [])

    def test_stale_by_both_qmd_age_and_reindex_fails_blocking(self) -> None:
        old = (_dt.datetime.now() - _dt.timedelta(hours=999)) \
            .isoformat(timespec="seconds")
        self._reindex_at(old, ["vault", "transcripts", "conclusions", "wisdom"])
        self.stub_qmd(QMD_STATUS_SAMPLE)
        record = H.run([_row("index-fresh")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("wisdom", row["detail"])
        self.assertEqual(record["blocked_legs"], ["research"])

    def test_recent_reindex_missing_the_stale_collection_does_not_rescue(
            self) -> None:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        self._reindex_at(now, ["vault", "transcripts", "conclusions"])
        self.stub_qmd(QMD_STATUS_SAMPLE)
        record = H.run([_row("index-fresh")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("wisdom", row["detail"])
        self.assertEqual(record["blocked_legs"], ["research"])

    def test_unparseable_reindex_timestamp_does_not_rescue(self) -> None:
        self._reindex_at("not-a-timestamp",
                         ["vault", "transcripts", "conclusions", "wisdom"])
        self.stub_qmd(QMD_STATUS_SAMPLE)
        record = H.run([_row("index-fresh")])
        self.assertFalse(record["assertions"][0]["ok"])
        self.assertEqual(record["blocked_legs"], ["research"])

    def test_verdict_comes_from_status_text_not_file_mtimes(self) -> None:
        # The parser is a pure function over the captured text: same text,
        # same answer, no filesystem access at all (SQLite WAL keeps the
        # index file's mtime perpetually stale-looking).
        self.assertEqual(H.parse_collection_ages(QMD_STATUS_SAMPLE),
                         H.parse_collection_ages(QMD_STATUS_SAMPLE))

    # -- hardening: a configured collection qmd stops reporting must not
    # silently drop out of the staleness check.

    def test_configured_collection_absent_from_status_blocks(self) -> None:
        self.stub_qmd(QMD_STATUS_MISSING_WISDOM)
        record = H.run([_row("index-fresh")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("wisdom", row["detail"])
        self.assertEqual(record["blocked_legs"], ["research"])

    def test_just_now_age_form_parses_and_passes(self) -> None:
        text = QMD_STATUS_FRESH.replace("updated 2h ago", "updated just now")
        ages = H.parse_collection_ages(text)
        self.assertEqual(set(ages), {"wisdom", "vault", "transcripts",
                                     "conclusions"})
        self.assertEqual(ages["wisdom"], 0.0)
        self.assertEqual(ages["transcripts"], 0.0)
        self.stub_qmd(text)
        record = H.run([_row("index-fresh")])
        row = record["assertions"][0]
        self.assertTrue(row["ok"], row["detail"])
        self.assertEqual(record["blocked_legs"], [])


class QmdUnreachableTest(LaunchCase):
    def test_qmd_absent_fails_both_with_detail_and_rest_still_run(self) -> None:
        def raise_missing():
            raise RuntimeError("qmd not on PATH")
        H.qmd_status_text = raise_missing
        reg = [_row("embeddings-current"), _row("index-fresh"),
               ("after", "info", (), lambda: True)]
        record = H.run(reg)
        rows = record["assertions"]
        self.assertFalse(rows[0]["ok"])
        self.assertIn("qmd not on PATH", rows[0]["detail"])
        self.assertFalse(rows[1]["ok"])
        self.assertIn("qmd not on PATH", rows[1]["detail"])
        self.assertTrue(rows[2]["ok"], "later assertions must still run")

    def test_unparseable_output_fails_with_the_contract_detail(self) -> None:
        self.stub_qmd(QMD_STATUS_MANGLED)
        record = H.run([_row("embeddings-current"), _row("index-fresh")])
        for row in record["assertions"]:
            self.assertFalse(row["ok"], row["name"])
            self.assertIn("qmd status output not understood", row["detail"])


class CollectionsCoveredTest(LaunchCase):
    def test_partial_reindex_fails_naming_the_missing_collection(self) -> None:
        self.write_state({"last_reindex": {
            "collections": ["vault", "transcripts", "conclusions"],
            "at": "2026-08-15T02:00:00"}})
        record = H.run([_row("collections-covered")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("wisdom", row["detail"])

    def test_full_reindex_passes(self) -> None:
        self.write_state({"last_reindex": {
            "collections": ["vault", "transcripts", "conclusions", "wisdom"],
            "at": "2026-08-15T02:00:00"}})
        record = H.run([_row("collections-covered")])
        self.assertTrue(record["assertions"][0]["ok"])

    def test_absent_record_fails(self) -> None:
        self.write_state({})
        record = H.run([_row("collections-covered")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertEqual(row["detail"], "no last_reindex record")


class VocabularyAppliedTest(LaunchCase):
    def _loop(self, name: str, created: str, tags: list) -> None:
        dc.write_page(self.tmp / "loops" / f"{name}.md",
                      {"type": "loop", "id": name, "status": "open",
                       "created": created, "tags": tags}, f"# {name}\n")

    def _vocab(self) -> None:
        (self.tmp / "meta" / "tag-vocabulary.json").write_text(
            json.dumps({"tags": ["architecture"]}), encoding="utf-8")

    @staticmethod
    def _days_ago(n: int) -> str:
        return (dc.today() - _dt.timedelta(days=n)).isoformat()

    def test_all_recent_loops_untagged_fails(self) -> None:
        self._vocab()
        self._loop("L0001", self._days_ago(2), [])
        self._loop("L0002", self._days_ago(5), [])
        record = H.run([_row("vocabulary-applied")])
        self.assertFalse(record["assertions"][0]["ok"])

    def test_any_tagged_recent_loop_passes(self) -> None:
        self._vocab()
        self._loop("L0001", self._days_ago(2), [])
        self._loop("L0002", self._days_ago(5), ["architecture"])
        record = H.run([_row("vocabulary-applied")])
        self.assertTrue(record["assertions"][0]["ok"])

    def test_no_vocabulary_file_is_not_applicable_and_passes(self) -> None:
        self._loop("L0001", self._days_ago(2), [])
        record = H.run([_row("vocabulary-applied")])
        self.assertTrue(record["assertions"][0]["ok"])

    def test_zero_loops_in_window_is_vacuous_and_passes(self) -> None:
        self._vocab()
        self._loop("L0001", self._days_ago(400), [])  # far outside the window
        record = H.run([_row("vocabulary-applied")])
        self.assertTrue(record["assertions"][0]["ok"])


class CostRecordsSaneTest(LaunchCase):
    """FIX: the check is windowed by health.cost_window_days — one historical
    outlier must not alarm forever, but a fresh runaway still fails, and a
    record whose `at` cannot be parsed counts as in-window (conservative)."""

    @staticmethod
    def _at(days_ago: float) -> str:
        return (_dt.datetime.now() - _dt.timedelta(days=days_ago)) \
            .isoformat(timespec="seconds")

    def test_fresh_runaway_record_fails_against_the_ceiling(self) -> None:
        self.write_state({"costs": [
            {"at": self._at(3), "job": "backfill", "cost": 26.0},
            {"at": self._at(1), "job": "weekly-dream", "cost": 123.45}]})
        record = H.run([_row("cost-records-sane")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("123.45", row["detail"])

    def test_old_outlier_outside_the_window_passes(self) -> None:
        self.write_state({"costs": [
            {"at": self._at(30), "job": "weekly-dream", "cost": 123.45},
            {"at": self._at(1), "job": "backfill", "cost": 26.0}]})
        record = H.run([_row("cost-records-sane")])
        row = record["assertions"][0]
        self.assertTrue(row["ok"], row["detail"])

    def test_undated_record_counts_as_in_window(self) -> None:
        # Conservative: a record we cannot date might be fresh — alarm.
        self.write_state({"costs": [{"at": "x", "job": "weekly-dream",
                                     "cost": 123.45}]})
        record = H.run([_row("cost-records-sane")])
        self.assertFalse(record["assertions"][0]["ok"])

    def test_documented_backfill_ceiling_does_not_false_fire(self) -> None:
        self.write_state({"costs": [{"at": self._at(1), "job": "backfill",
                                     "cost": 26.0}]})
        record = H.run([_row("cost-records-sane")])
        self.assertTrue(record["assertions"][0]["ok"])


class GatesCurrentTest(LaunchCase):
    """Gates are config-driven (health.gates), not code: the fixture seeds one
    synthetic gate and restores the config afterwards, mirroring how the base
    class patches dc.CFG['paths']."""

    def setUp(self) -> None:
        super().setUp()
        health = dc.CFG.setdefault("health", {})
        self._had_gates = "gates" in health
        self._orig_gates = health.get("gates")
        health["gates"] = [{"flag": "sample_gate",
                            "text": "sample acceptance gate open"}]

    def tearDown(self) -> None:
        health = dc.CFG.get("health") or {}
        if self._had_gates:
            health["gates"] = self._orig_gates
        else:
            health.pop("gates", None)
        super().tearDown()

    def test_open_gate_fails_with_its_reminder_and_no_event(self) -> None:
        record = H.run([_row("gates-current")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("sample acceptance gate open", row["detail"])
        self.assertFalse(self.state().get("events"),
                         "info severity must never reach the event channel")

    def test_true_flag_in_gate_state_closes_the_gate(self) -> None:
        (self.tmp / "meta" / "gate-state.json").write_text(
            json.dumps({"sample_gate": True}), encoding="utf-8")
        record = H.run([_row("gates-current")])
        row = record["assertions"][0]
        self.assertTrue(row["ok"], row["detail"])
        self.assertNotIn("sample acceptance gate open", row["detail"])

    def test_no_configured_gates_passes(self) -> None:
        dc.CFG["health"]["gates"] = []
        record = H.run([_row("gates-current")])
        row = record["assertions"][0]
        self.assertTrue(row["ok"])
        self.assertIn("no standing owner gates configured", row["detail"])


class FoldQueueCurrentTest(LaunchCase):
    """FIX 4d: fold-queue-current — the fold queue must stay bounded (depth),
    current (entry age), and quarantine-empty; any leg failing is degraded,
    never blocking, and the detail names depth/oldest/quarantined counts."""

    @staticmethod
    def _at(days_ago: float) -> str:
        return (_dt.datetime.now() - _dt.timedelta(days=days_ago)) \
            .isoformat(timespec="seconds")

    def _pending(self, entries: list) -> None:
        (self.tmp / "meta" / "fold-pending.json").write_text(
            json.dumps(entries), encoding="utf-8")

    def _quarantine(self, entries: list) -> None:
        (self.tmp / "meta" / "fold-quarantine.json").write_text(
            json.dumps(entries), encoding="utf-8")

    def test_registered_degraded_blocking_nothing(self) -> None:
        row = _row("fold-queue-current")
        self.assertEqual(row[1], "degraded")
        self.assertEqual(row[2], ())

    def test_depth_over_max_fails_degraded(self) -> None:
        limit = int(dc.CFG["thread"]["fold_queue_max"])
        self._pending([{"loop_id": f"L{i:04d}",
                        "occurrence": f"[[sources/transcripts/x{i}]]",
                        "enqueued_at": self._at(0)}
                       for i in range(limit + 1)])
        record = H.run([_row("fold-queue-current")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn(str(limit + 1), row["detail"])
        self.assertIn("depth", row["detail"])
        self.assertEqual(record["blocked_legs"], [])
        events = self.state().get("events") or []
        self.assertEqual([e["kind"] for e in events], ["degraded"])

    def test_old_entry_fails(self) -> None:
        self._pending([{"loop_id": "L0001", "occurrence": "[[x]]",
                        "enqueued_at": self._at(10)}])
        record = H.run([_row("fold-queue-current")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("oldest", row["detail"])

    def test_nonempty_quarantine_fails(self) -> None:
        self._pending([])
        self._quarantine([{"loop_id": "L0001", "occurrence": "[[x]]",
                           "attempts": 5}])
        record = H.run([_row("fold-queue-current")])
        row = record["assertions"][0]
        self.assertFalse(row["ok"])
        self.assertIn("quarantin", row["detail"])

    def test_fresh_small_queue_passes_and_unstamped_counts_fresh(self) -> None:
        # Entries without enqueued_at (pre-existing queues) count as fresh —
        # conservative: they age from their first stamped write onward.
        self._pending([{"loop_id": "L0001", "occurrence": "[[x]]"},
                       {"loop_id": "L0002", "occurrence": "[[y]]",
                        "enqueued_at": self._at(0.5)}])
        record = H.run([_row("fold-queue-current")])
        row = record["assertions"][0]
        self.assertTrue(row["ok"], row["detail"])
        for word in ("depth", "oldest", "quarantined"):
            self.assertIn(word, row["detail"])
        self.assertFalse(self.state().get("events"))

    def test_no_queue_files_at_all_passes(self) -> None:
        record = H.run([_row("fold-queue-current")])
        self.assertTrue(record["assertions"][0]["ok"],
                        record["assertions"][0]["detail"])


class RunHealthcheckSmokeTest(HealthCase):
    """FIX 6d: bin/_common.sh run_healthcheck — a non-zero healthcheck exit
    is degraded, never fatal: the function returns 0 and logs the WARN line.
    The python interpreter is stubbed (DREAMER_PYTHON) so the failure is
    deterministic and no real run-state is touched."""

    def test_nonzero_healthcheck_warns_and_returns_zero(self) -> None:
        stub = self.tmp / "fake-python"
        stub.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
        stub.chmod(0o755)
        cmd = (f'source "{ROOT}/bin/_common.sh"; META="{self.tmp}/meta"; '
               f'run_healthcheck "(smoke probe)"; echo "rc=$?"')
        env = dict(os.environ, DREAMER_PYTHON=str(stub), JOB="smoke")
        proc = subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rc=0", proc.stdout,
                      "run_healthcheck must return 0 on healthcheck failure")
        self.assertIn("healthcheck exited non-zero", proc.stdout)
        self.assertIn("(smoke probe)", proc.stdout,
                      "the caller-supplied detail must reach the log line")


class LaunchConfigTest(LaunchCase):
    def test_config_declares_the_launch_thresholds(self) -> None:
        health = dc.CFG.get("health") or {}
        for key in ("embed_pending_max", "index_stale_hours",
                    "untagged_window_days", "cost_max_per_run",
                    "cost_window_days"):
            self.assertIn(key, health, f"config.yaml must carry health.{key}")
        self.assertEqual((dc.CFG.get("qmd") or {}).get("collections"),
                         ["vault", "transcripts", "conclusions", "wisdom"])
        thread = dc.CFG.get("thread") or {}
        for key in ("fold_max_attempts", "fold_queue_max",
                    "fold_queue_max_age_days"):
            self.assertIn(key, thread, f"config.yaml must carry thread.{key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
