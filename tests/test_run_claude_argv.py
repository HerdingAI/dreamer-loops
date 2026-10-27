#!/usr/bin/env python3
"""run_claude argv shapes. A stub `claude` on PATH records its
argv and environment; no real claude runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

STUB = r"""#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
with open(os.environ["STUB_RECORD"], "w") as fh:
    json.dump({"argv": sys.argv[1:],
               "approved": os.environ.get("DREAMER_EGRESS_APPROVED"),
               "log": os.environ.get("DREAMER_EGRESS_LOG"),
               "results": os.environ.get("DREAMER_EGRESS_RESULTS"),
               "no_memory": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"),
               "no_mds": os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS")}, fh)
print(json.dumps({"result": "ok"}))
"""

# Flags that take several values: each must be followed by another flag (or the
# end of argv), never by something that would be read as one more value.
VARIADIC = {"--tools", "--allowedTools", "--allowed-tools",
            "--disallowedTools", "--disallowed-tools"}


class RunClaudeArgvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        stub = bindir / "claude"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.bindir = bindir
        self.record = self.tmp / "record.json"
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text("hello", encoding="utf-8")
        self.out = self.tmp / "out.json"

    def run_mode(self, mode: str | None, max_turns: int = 10, env_extra=None):
        mode_arg = f" {mode}" if mode else ""
        cmd = (f'source "{ROOT}/bin/_common.sh"; '
               f'META="{self.tmp}"; PATH="{self.bindir}:$PATH"; '
               f'run_claude "{self.prompt}" {max_turns} "{self.out}"{mode_arg}')
        env = dict(os.environ, JOB="test-argv", STUB_RECORD=str(self.record))
        env.pop("DREAMER_FAKE_CLAUDE", None)
        env.pop("DREAMER_EGRESS_APPROVED", None)
        env.pop("DREAMER_EGRESS_LOG", None)
        env.pop("DREAMER_EGRESS_RESULTS", None)
        env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        env.pop("CLAUDE_CODE_DISABLE_CLAUDE_MDS", None)
        env.update(env_extra or {})
        proc = subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), env=env,
                              capture_output=True, text=True)
        rec = (json.loads(self.record.read_text(encoding="utf-8"))
               if self.record.exists() else None)
        return proc, rec

    def assert_no_swallow(self, argv):
        for i, tok in enumerate(argv):
            if tok in VARIADIC:
                j = i + 1
                while j < len(argv) and not argv[j].startswith("--"):
                    j += 1
                # everything between the flag and the next flag is its values;
                # the caller asserts the exact values, this asserts the next
                # token is a flag or the end.
                self.assertTrue(j == len(argv) or argv[j].startswith("--"))

    def value_of(self, argv, flag):
        """Values following a variadic flag, up to the next flag."""
        i = argv.index(flag) + 1
        vals = []
        while i < len(argv) and not argv[i].startswith("--"):
            vals.append(argv[i])
            i += 1
        return vals

    # ---- restricted ------------------------------------------------------

    def test_restricted_is_sealed(self):
        proc, rec = self.run_mode("restricted")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = rec["argv"]
        self.assertEqual(self.value_of(argv, "--tools"), [""])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--permission-mode", argv)
        # the existing deny list stays
        denied = self.value_of(argv, "--disallowedTools")
        for t in ("Bash", "Edit", "Write", "WebSearch", "WebFetch", "Read",
                  "mcp__qmd", "mcp__dreamer"):
            self.assertIn(t, denied)
        self.assert_no_swallow(argv)

    def test_restricted_clamps_turns_to_four(self):
        _, rec = self.run_mode("restricted", max_turns=30)
        argv = rec["argv"]
        self.assertEqual(argv[argv.index("--max-turns") + 1], "4")
        _, rec = self.run_mode("restricted", max_turns=3)
        self.assertEqual(rec["argv"][rec["argv"].index("--max-turns") + 1], "3")

    # ---- gated -----------------------------------------------------------

    def gated(self, **extra):
        approved = self.tmp / "approved.txt"
        approved.write_text("a generic question\n", encoding="utf-8")
        env = {"DREAMER_EGRESS_APPROVED": str(approved)}
        env.update(extra)
        return self.run_mode("gated", env_extra=env), approved

    def test_gated_holds_only_websearch(self):
        (proc, rec), approved = self.gated(DREAMER_EGRESS_LOG=str(self.tmp / "e.jsonl"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = rec["argv"]
        self.assertEqual(self.value_of(argv, "--tools"), ["WebSearch"])
        self.assertEqual(self.value_of(argv, "--allowedTools"), ["WebSearch"])
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertIn("--disable-slash-commands", argv)
        flat = " ".join(argv)
        for banned in ("qmd", "WebFetch", "Bash", "acceptEdits"):
            self.assertNotIn(banned, flat)
        self.assert_no_swallow(argv)

    def test_gated_settings_file_names_the_hook(self):
        (_, rec), _ = self.gated(DREAMER_EGRESS_LOG=str(self.tmp / "e.jsonl"))
        argv = rec["argv"]
        settings = Path(argv[argv.index("--settings") + 1])
        self.assertTrue(settings.is_file())
        cfg = json.loads(settings.read_text(encoding="utf-8"))
        pre = cfg["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["matcher"], "*")
        cmd = pre[0]["hooks"][0]["command"]
        self.assertIn(str(ROOT / "scripts" / "egress_hook.py"), cmd)

    def test_gated_drops_user_settings_and_memory(self):
        """The research call must not carry the owner's global CLAUDE.md, auto-memory or
        user hooks/plugins into a context that reads untrusted search results."""
        (_, rec), _ = self.gated(DREAMER_EGRESS_LOG=str(self.tmp / "e.jsonl"))
        argv = rec["argv"]
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        self.assertIn("--settings", argv)  # flag settings still load: the hooks live there
        self.assertNotIn("--bare", argv)  # --bare disables OAuth
        self.assertEqual(rec["no_memory"], "1")
        self.assertEqual(rec["no_mds"], "1")  # --setting-sources alone leaves the user CLAUDE.md loaded

    def test_restricted_is_unchanged_by_the_isolation_flags(self):
        _, rec = self.run_mode("restricted")
        self.assertNotIn("--setting-sources", rec["argv"])
        self.assertIsNone(rec["no_memory"])
        self.assertIsNone(rec["no_mds"])

    def test_gated_results_file_defaults_next_to_approved_and_is_passed(self):
        (_, rec), approved = self.gated(DREAMER_EGRESS_LOG=str(self.tmp / "e.jsonl"))
        self.assertEqual(rec["results"], f"{approved}.results")
        (_, rec), _ = self.gated(DREAMER_EGRESS_LOG=str(self.tmp / "e.jsonl"),
                                 DREAMER_EGRESS_RESULTS=str(self.tmp / "r.txt"))
        self.assertEqual(rec["results"], str(self.tmp / "r.txt"))

    def test_gated_settings_file_has_the_websearch_result_hook(self):
        (_, rec), _ = self.gated(DREAMER_EGRESS_LOG=str(self.tmp / "e.jsonl"))
        argv = rec["argv"]
        cfg = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
        post = cfg["hooks"]["PostToolUse"]
        self.assertEqual(post[0]["matcher"], "WebSearch")
        self.assertIn(str(ROOT / "scripts" / "egress_results_hook.py"), post[0]["hooks"][0]["command"])

    def test_gated_env_reaches_the_hook_and_turns_come_from_config(self):
        log = self.tmp / "e.jsonl"
        (_, rec), approved = self.gated(DREAMER_EGRESS_LOG=str(log))
        self.assertEqual(rec["approved"], str(approved))
        self.assertEqual(rec["log"], str(log))
        argv = rec["argv"]
        self.assertEqual(argv[argv.index("--max-turns") + 1],
                         str(CFG["budget"]["max_turns_frontier_research"]))

    def test_gated_log_path_defaults_from_config(self):
        (_, rec), _ = self.gated()
        self.assertEqual(rec["log"], CFG["frontier"]["egress"]["log_path"])

    def test_gated_without_approved_file_refuses_and_runs_nothing(self):
        proc, rec = self.run_mode("gated")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNone(rec)

    # ---- full mode unchanged ---------------------------------------------

    def test_full_mode_unchanged(self):
        _, rec = self.run_mode(None)
        argv = rec["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(self.value_of(argv, "--allowedTools"),
                         ["Bash(qmd:*)", "mcp__qmd", "WebSearch", "WebFetch"])
        self.assertNotIn("--tools", argv)
        self.assertNotIn("--strict-mcp-config", argv)


if __name__ == "__main__":
    unittest.main()
