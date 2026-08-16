#!/usr/bin/env python3
"""U4 — bin/_common.sh reindex(): config-driven collections, inline embed,
last_reindex coverage record.

reindex() is bash, so this suite shells out (mirroring BashHelperTest in
tests/test_healthcheck.py): it sources bin/_common.sh with META pointed at a
scratch dir and a fake `qmd` shell script first on PATH that records its argv
and fails on demand. No real qmd is ever reached — the fake shadows the nvm
binary that _common.sh prepends to PATH.

Covers: update + embed per configured collection in config order (wisdom
included), embed failure -> degraded event + continue (update coverage kept),
update failure -> degraded event + collection excluded from last_reindex,
qmd absent -> unchanged hardening (rc 1, degraded event, no qmd calls,
no last_reindex), and the all-updates-failed empty coverage record.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import healthcheck as H  # noqa: E402

FAKE_QMD = """\
#!/usr/bin/env bash
# Argv recorder + scriptable failure. QMD_UPDATE_FAIL / QMD_EMBED_FAIL are
# comma-separated collection lists that make that subcommand exit 1.
printf '%s\\n' "$*" >> "$QMD_CALLS"
cmd="${1:-}"; coll="${3:-}"
case "$cmd" in
  update) [[ ",${QMD_UPDATE_FAIL:-}," == *",$coll,"* ]] && exit 1 ;;
  embed)  [[ ",${QMD_EMBED_FAIL:-},"  == *",$coll,"* ]] && exit 1 ;;
esac
exit 0
"""

CONFIGURED = ["vault", "transcripts", "conclusions", "wisdom"]


class ReindexHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-reindex-"))
        self.meta = self.tmp / "meta"
        self.meta.mkdir()
        self.stubbin = self.tmp / "stubbin"
        self.stubbin.mkdir()
        qmd = self.stubbin / "qmd"
        qmd.write_text(FAKE_QMD, encoding="utf-8")
        qmd.chmod(0o755)
        self.calls_file = self.tmp / "qmd-calls.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_reindex(self, *, update_fail: str = "", embed_fail: str = "",
                    qmd_on_path: bool = True) -> subprocess.CompletedProcess:
        if qmd_on_path:
            # The stub must come FIRST: _common.sh prepends the real nvm qmd.
            path_cmd = f'PATH="{self.stubbin}:$PATH"'
        else:
            # Minimal PATH with no qmd anywhere (python3/date live here);
            # exercises the unchanged qmd-missing hardening.
            path_cmd = 'PATH="/usr/bin:/bin"'
        cmd = (f'source "{ROOT}/bin/_common.sh"; '
               f'META="{self.meta}"; {path_cmd}; reindex')
        env = dict(os.environ,
                   JOB="test-reindex",
                   QMD_CALLS=str(self.calls_file),
                   QMD_UPDATE_FAIL=update_fail,
                   QMD_EMBED_FAIL=embed_fail)
        return subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                              capture_output=True, text=True)

    def calls(self) -> list[str]:
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text(encoding="utf-8").splitlines()

    def state(self) -> dict:
        p = self.meta / "run-state.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    def degraded_details(self) -> list[str]:
        return [e["detail"] for e in self.state().get("events") or []
                if e.get("kind") == "degraded"]


class HappyPathTest(ReindexHarness):
    def test_update_and_embed_per_configured_collection_in_order(self) -> None:
        proc = self.run_reindex()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        want = []
        for c in CONFIGURED:
            want += [f"update -c {c}", f"embed -c {c}"]
        self.assertEqual(self.calls(), want,
                         "reindex must drive update+embed from qmd.collections"
                         " (wisdom included), in config order")

    def test_last_reindex_records_all_collections_with_timestamp(self) -> None:
        self.run_reindex()
        rec = self.state().get("last_reindex")
        self.assertIsInstance(rec, dict, "no last_reindex record written")
        self.assertEqual(rec["collections"], CONFIGURED)
        at = _dt.datetime.fromisoformat(rec["at"])  # raises if unparseable
        self.assertLess(abs((_dt.datetime.now() - at).total_seconds()), 300)

    def test_no_degraded_events_on_success(self) -> None:
        self.run_reindex()
        self.assertEqual(self.degraded_details(), [])


class EmbedFailureTest(ReindexHarness):
    def test_embed_failure_is_degraded_never_fatal_and_never_aborts(
            self) -> None:
        proc = self.run_reindex(embed_fail="transcripts")
        # Lexical stays fresh even when embedding fails: rc stays 0.
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.calls()
        # Every update ran, and embeds AFTER the failing one still ran.
        for c in CONFIGURED:
            self.assertIn(f"update -c {c}", calls)
            self.assertIn(f"embed -c {c}", calls)
        details = self.degraded_details()
        self.assertEqual(len(details), 1, details)
        self.assertIn("qmd embed failed for collection 'transcripts'",
                      details[0])
        # `qmd update` succeeded for all four, so coverage keeps all four.
        self.assertEqual(self.state()["last_reindex"]["collections"],
                         CONFIGURED)


class UpdateFailureTest(ReindexHarness):
    def test_update_failure_events_skips_embed_and_drops_coverage(
            self) -> None:
        proc = self.run_reindex(update_fail="transcripts")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        calls = self.calls()
        self.assertIn("update -c transcripts", calls)
        self.assertNotIn("embed -c transcripts", calls,
                         "no embed against a failed update")
        # Later collections still processed in full.
        self.assertIn("update -c wisdom", calls)
        self.assertIn("embed -c wisdom", calls)
        details = self.degraded_details()
        self.assertTrue(any("qmd re-index failed for collection 'transcripts'"
                            in d for d in details), details)
        self.assertEqual(self.state()["last_reindex"]["collections"],
                         ["vault", "conclusions", "wisdom"])

    def test_all_updates_failing_writes_empty_coverage(self) -> None:
        proc = self.run_reindex(update_fail=",".join(CONFIGURED))
        self.assertEqual(proc.returncode, 1)
        rec = self.state().get("last_reindex")
        self.assertIsInstance(rec, dict)
        self.assertEqual(rec["collections"], [],
                         "a fully failed reindex must read as zero coverage, "
                         "not inherit last night's record")


# `qmd status` shape where the static wisdom corpus looks long-unchanged
# (qmd's age tracks content change, not last scan) while everything else is
# fresh — the exact state that made a stale-index block self-sustaining.
STALE_WISDOM_STATUS = """\
QMD Status

Documents
  Total:    16 files indexed
  Vectors:  160 embedded
  Updated:  1m ago

Collections
  vault (qmd://vault/)
    Pattern:  **/*.md
    Files:    4 (updated 1h ago)
  transcripts (qmd://transcripts/)
    Pattern:  **/*.md
    Files:    4 (updated 1h ago)
  conclusions (qmd://conclusions/)
    Pattern:  **/*.md
    Files:    4 (updated 1h ago)
  wisdom (qmd://wisdom/)
    Pattern:  **/*.md
    Files:    4 (updated 14d ago)
"""


class IndexFreshRescueTest(ReindexHarness):
    """FIX (quiet-period self-sustaining research block): a last_reindex
    record older than the window rescues nothing and index-fresh fails; a
    fresh record written by an ACTUAL (stubbed) reindex run rescues it —
    proving the record reindex() writes is the one the assertion's rescue
    window reads, which is why the no-op nightly branch and the pre-gate
    weekly reindex can un-stick a blocked research leg."""

    def setUp(self) -> None:
        super().setUp()
        self._paths = dict(dc.CFG["paths"])
        dc.CFG["paths"]["meta"] = str(self.meta)
        self._qmd = H.qmd_status_text
        H.qmd_status_text = lambda: STALE_WISDOM_STATUS

    def tearDown(self) -> None:
        H.qmd_status_text = self._qmd
        H._QMD_CACHE.clear()
        H._STATE_CACHE.clear()
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(self._paths)
        super().tearDown()

    def _index_fresh(self) -> tuple:
        H._QMD_CACHE.clear()
        H._STATE_CACHE.clear()
        return H.assert_index_fresh()

    def test_reindex_run_rescues_a_stale_looking_static_collection(self) -> None:
        old = (_dt.datetime.now() - _dt.timedelta(hours=999)) \
            .isoformat(timespec="seconds")
        (self.meta / "run-state.json").write_text(json.dumps(
            {"last_reindex": {"collections": CONFIGURED, "at": old}}),
            encoding="utf-8")
        ok, detail = self._index_fresh()
        self.assertFalse(ok, f"an aged last_reindex must rescue nothing: {detail}")
        self.assertIn("wisdom", detail)

        proc = self.run_reindex()  # writes a fresh last_reindex record
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        ok, detail = self._index_fresh()
        self.assertTrue(ok, f"a fresh reindex record must rescue: {detail}")


class QmdAbsentTest(ReindexHarness):
    def test_missing_qmd_keeps_the_stale_dead_hardening(self) -> None:
        proc = self.run_reindex(qmd_on_path=False)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("STALE", proc.stdout)
        self.assertIn("DEAD", proc.stdout)
        self.assertEqual(self.calls(), [], "no qmd call may be attempted")
        details = self.degraded_details()
        self.assertTrue(any("qmd unreachable" in d for d in details), details)
        self.assertNotIn("last_reindex", self.state(),
                         "an unreachable qmd proves nothing about coverage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
