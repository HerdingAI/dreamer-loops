#!/usr/bin/env python3
"""Publishing straight after an owner write, so a change made through the MCP
reaches a display in minutes, not at the next hourly publish."""

from __future__ import annotations

import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import dreamer_common as dc  # noqa: E402
import dreamer_mcp as M  # noqa: E402
import publish_trigger as PT  # noqa: E402
import vault as V  # noqa: E402
from test_owner_writes import D, OwnerWritesCase  # noqa: E402


class TriggerCase(OwnerWritesCase):
    def setUp(self) -> None:
        super().setUp()
        self._enabled = PT.ENABLED
        self.spawned: list[tuple[str, Path]] = []
        patcher = mock.patch.object(PT, "_spawn", side_effect=lambda cmd, log: self.spawned.append((cmd, log)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        PT.ENABLED = self._enabled
        super().tearDown()


class FireTest(TriggerCase):
    def test_off_by_default_so_a_test_or_a_script_never_publishes(self) -> None:
        self.assertFalse(PT.ENABLED)
        self.assertFalse(PT.fire())
        self.assertEqual(self.spawned, [])

    def test_enabled_spawns_the_board_publisher_then_the_health_publisher(self) -> None:
        PT.ENABLED = True
        self.assertTrue(PT.fire())
        self.assertEqual(len(self.spawned), 1)
        cmd, log = self.spawned[0]
        self.assertLess(cmd.index("board_publish.py"), cmd.index("freshness_publish.py"))
        self.assertIn("board-publish.log", cmd)
        self.assertIn("freshness-publish.log", cmd)
        self.assertEqual(log.name, "publish-trigger.log")

    def test_config_can_turn_it_off(self) -> None:
        PT.ENABLED = True
        with mock.patch.dict(dc.CFG["board"], {"publish_on_owner_write": False}):
            self.assertFalse(PT.fire())
        self.assertEqual(self.spawned, [])

    def test_a_spawn_failure_is_swallowed(self) -> None:
        PT.ENABLED = True
        with mock.patch.object(PT, "_spawn", side_effect=OSError("no sh")):
            self.assertFalse(PT.fire())


class SpawnTest(unittest.TestCase):
    def test_the_child_is_detached_and_writes_only_to_its_log(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "logs" / "t.log"
            started = time.monotonic()
            PT._spawn("sleep 0.3; echo from-child", log)
            self.assertLess(time.monotonic() - started, 0.25)  # did not wait for it
            for _ in range(40):
                if log.exists() and "from-child" in log.read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)
            self.assertIn("from-child", log.read_text(encoding="utf-8"))


class McpWiringTest(TriggerCase):
    def resolve(self, **over) -> dict:
        t = self.transcript("2026-07-01", "loop-trans")
        loop = V.create_loop("Loop to resolve", t, D("2026-07-01"))
        args = dict(id=loop.id, status="resolved", owner_quote="settled this outside the system",
                    session_id="s3")
        args.update(over)
        return json.loads(M.tool_set_status(**args))

    def test_a_status_change_publishes_straight_away(self) -> None:
        PT.ENABLED = True
        self.assertTrue(self.resolve().get("ok"))
        self.assertEqual(len(self.spawned), 1)

    def test_a_refused_write_publishes_nothing(self) -> None:
        PT.ENABLED = True
        self.assertIn("error", self.resolve(owner_quote=""))
        self.assertIn("error", json.loads(M.tool_set_status(
            id="L9999", status="resolved", owner_quote="settled this outside the system", session_id="s3")))
        self.assertEqual(self.spawned, [])

    def test_a_status_change_in_a_test_or_script_publishes_nothing(self) -> None:
        self.assertTrue(self.resolve().get("ok"))
        self.assertEqual(self.spawned, [])

    def test_a_trigger_failure_never_fails_the_write(self) -> None:
        PT.ENABLED = True
        with mock.patch.object(PT, "_spawn", side_effect=OSError("no sh")):
            self.assertTrue(self.resolve().get("ok"))

    def test_only_the_running_server_arms_the_trigger(self) -> None:
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            self.assertEqual(M.main(), 0)
        self.assertTrue(PT.ENABLED)


if __name__ == "__main__":
    unittest.main()
