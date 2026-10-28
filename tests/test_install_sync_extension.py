#!/usr/bin/env python3
"""bin/install-sync-extension.sh: extension-ID validation and the host manifest.

The installer is copied into a sandbox tree (its ROOT is derived from its own
location) beside a stub host and a sandbox config.yaml, and run with a sandbox
HOME. It never touches the real ~/.config/google-chrome or a real Chrome.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "bin" / "install-sync-extension.sh"

GOOD_ID = "a" * 16 + "p" * 16     # 32 chars from a-p: the shape Chrome assigns
HOST_NAME = "com.dreamer.claude_ai_sync"


class InstallerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-install-test-"))
        self.root = self.tmp / "repo"
        (self.root / "bin").mkdir(parents=True)
        shutil.copy(INSTALLER, self.root / "bin" / INSTALLER.name)
        host = self.root / "bin" / "claude-ai-host"
        host.write_text("#!/bin/sh\nexit 0\n")
        host.chmod(0o755)
        ext = self.root / "extension" / "dreamer-sync"
        ext.mkdir(parents=True)
        shutil.copy(INSTALLER.parent.parent / "extension" / "dreamer-sync" / "background.js", ext)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.nmh = self.home / ".config" / "google-chrome" / "NativeMessagingHosts"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, ext_id_line: str, host_name: str = HOST_NAME) -> None:
        (self.root / "config.yaml").write_text(
            "claude_ai_sync:\n"
            f"  host_name: {host_name}\n"
            f"{ext_id_line}\n", encoding="utf-8")

    def run_installer(self) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        return subprocess.run(["bash", str(self.root / "bin" / INSTALLER.name)],
                              env=env, capture_output=True, text=True, timeout=60)

    def written(self) -> list[Path]:
        return sorted(p for p in self.home.rglob("*") if p.is_file())


class RejectedIdTest(InstallerCase):
    def assert_rejected(self, ext_id_line: str) -> None:
        self.config(ext_id_line)
        r = self.run_installer()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("claude_ai_sync.extension_id", r.stderr)
        self.assertEqual(self.written(), [], "a rejected run must write nothing")
        self.assertFalse(self.nmh.exists(), "not even the host directory")

    def test_empty_id_is_rejected(self) -> None:
        self.assert_rejected('  extension_id: ""')

    def test_missing_key_is_rejected(self) -> None:
        self.assert_rejected("  unrelated: 1")

    def test_null_id_is_rejected(self) -> None:
        self.assert_rejected("  extension_id:")

    def test_malformed_ids_are_rejected(self) -> None:
        for bad in ("short", "A" * 32, "q" * 32, GOOD_ID + "a", GOOD_ID[:-1]):
            with self.subTest(bad=bad):
                self.assert_rejected(f"  extension_id: {bad}")

    def test_the_message_tells_the_user_what_to_do(self) -> None:
        self.config('  extension_id: ""')
        r = self.run_installer()
        self.assertIn("chrome://extensions", r.stderr)

    def test_a_host_name_the_extension_does_not_use_is_rejected(self) -> None:
        # The extension connects to the name compiled into background.js; a manifest
        # under any other name would install cleanly and never be reached.
        self.config(f"  extension_id: {GOOD_ID}", host_name="com.example.other_host")
        r = self.run_installer()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("claude_ai_sync.host_name", r.stderr)
        self.assertIn(HOST_NAME, r.stderr)
        self.assertEqual(self.written(), [])


class WrittenManifestTest(InstallerCase):
    def setUp(self) -> None:
        super().setUp()
        self.config(f"  extension_id: {GOOD_ID}")
        self.result = self.run_installer()

    def manifest(self) -> dict:
        return json.loads((self.nmh / f"{HOST_NAME}.json").read_text(encoding="utf-8"))

    def test_a_valid_id_installs(self) -> None:
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        self.assertEqual(self.written(), [self.nmh / f"{HOST_NAME}.json"])

    def test_exactly_one_allowed_origin_for_the_configured_id(self) -> None:
        self.assertEqual(self.manifest()["allowed_origins"],
                         [f"chrome-extension://{GOOD_ID}/"])

    def test_the_manifest_points_at_the_host_launcher(self) -> None:
        m = self.manifest()
        self.assertEqual(m["name"], HOST_NAME)
        self.assertEqual(m["type"], "stdio")
        self.assertEqual(m["path"], str(self.root / "bin" / "claude-ai-host"))

    def test_the_closing_steps_do_not_claim_the_manifest_pins_the_id(self) -> None:
        out = self.result.stdout
        self.assertNotIn("pins", out)
        self.assertIn("chrome://extensions", out)
        self.assertIn("claude_ai_sync.extension_id", out)

    def test_rerunning_is_idempotent(self) -> None:
        first = (self.nmh / f"{HOST_NAME}.json").read_text(encoding="utf-8")
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.nmh / f"{HOST_NAME}.json").read_text(encoding="utf-8"), first)
        self.assertEqual(len(self.written()), 1, "no temp files left behind")


class ShippedFilesTest(unittest.TestCase):
    def test_the_manifest_carries_no_pinned_key(self) -> None:
        m = json.loads((ROOT / "extension" / "dreamer-sync" / "manifest.json")
                       .read_text(encoding="utf-8"))
        self.assertNotIn("key", m)


if __name__ == "__main__":
    unittest.main()
