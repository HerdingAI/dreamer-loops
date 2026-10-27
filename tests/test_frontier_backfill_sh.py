#!/usr/bin/env python3
"""bin/frontier-backfill.sh end to end in a throwaway copy of the repo.

The copy's config.yaml points every path at the copy, claude is tests/fake_claude.py,
qmd is a no-op stub, and there is no postgres.env, so nothing reaches the real vault,
qmd or the network."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sh(cmd, cwd, env=None, **kw):
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, **kw)


class BackfillScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-bf-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for d in ("bin", "scripts", "skills"):
            shutil.copytree(ROOT / d, self.repo / d, ignore=shutil.ignore_patterns("__pycache__"))
        cfg = (ROOT / "config.yaml").read_text()
        (self.repo / "config.yaml").write_text(cfg)
        for sub in ("sources/transcripts/2029/12", ".vault-meta", "loops", "goals", "frontier/items"):
            (self.repo / "vault" / sub).mkdir(parents=True)
        (self.repo / "logs").mkdir()
        (self.repo / ".gitignore").write_text("logs/.*\n__pycache__/\n")
        self.add_chat("a", "2029-12-21", "Explained rim thickness to the club.")
        sh(["git", "init", "-q"], self.repo)
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], self.repo)
        stub = self.tmp / "stubbin"
        stub.mkdir()
        (stub / "qmd").write_text("#!/bin/sh\nexit 0\n")
        (stub / "qmd").chmod(0o755)
        self.script = self.tmp / "script.json"
        self.script.write_text(json.dumps({"extract": {"2029-12-21--a": [{
            "title": "Rim thickness", "summary": "How thick a mug rim should be.", "kind": "explained",
            "quote": "Explained rim thickness to the club.", "goals": []}]}}))
        self.env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}",
                    "DREAMER_FAKE_CLAUDE": str(ROOT / "tests" / "fake_claude.py"),
                    "DREAMER_FAKE_FRONTIER_SCRIPT": str(self.script),
                    "DREAMER_PG_ENV": str(self.tmp / "no-such-postgres.env"),
                    "DREAMER_TODAY": "2030-01-15", "HOME": str(self.tmp)}
        self.env.pop("DREAMER_LOCK_HELD", None)
        self.env.pop("DREAMER_FAKE_FRONTIER_FAIL", None)

    def add_chat(self, slug, date, text):
        path = self.repo / "vault/sources/transcripts/2029/12" / f"{date}--{slug}.md"
        path.write_text(f"---\ntype: transcript\ndate: {date}\nmessage_count: 1\n---\n\n# t\n\n"
                        f"## Human\n\n{text}\n", encoding="utf-8")

    def run_script(self, *args):
        return sh(["bash", str(self.repo / "bin" / "frontier-backfill.sh"), *args],
                  self.repo, self.env, timeout=120)

    def git_log(self) -> str:
        return sh(["git", "log", "--format=%s"], self.repo).stdout

    def test_drains_the_corpus_commits_and_is_a_noop_afterwards(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ledger = json.loads((self.repo / "vault/.vault-meta/frontier-map-ledger.json").read_text())
        self.assertEqual(list(ledger["done"]), ["sources/transcripts/2029/12/2029-12-21--a"])
        self.assertEqual(len(list((self.repo / "vault/frontier/items").glob("K*.md"))), 1)
        log = self.git_log()
        self.assertIn("frontier-backfill 2030-01-15: batch 1 mapped=1 created=1", log)
        self.assertEqual(sh(["git", "status", "--porcelain", "--", "vault/frontier",
                                      "vault/.vault-meta/frontier-map-ledger.json"], self.repo).stdout, "")
        again = self.run_script()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(len(list((self.repo / "vault/frontier/items").glob("K*.md"))), 1)

    def test_batch_count_limits_the_run(self):
        # one batch holds a single chat when the batch is tiny
        cfg = (self.repo / "config.yaml").read_text().replace("map_batch_chars: 80000", "map_batch_chars: 10")
        (self.repo / "config.yaml").write_text(cfg)
        self.add_chat("b", "2029-12-22", "Decided to log kiln cooling weekly.")
        r = self.run_script("1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ledger = json.loads((self.repo / "vault/.vault-meta/frontier-map-ledger.json").read_text())
        self.assertEqual(len(ledger["done"]), 1)
        self.assertIn("batch limit (1) reached", r.stdout)

    def test_usage_limit_exit_defers_the_rest_and_exits_cleanly(self):
        self.env["DREAMER_FAKE_FRONTIER_FAIL"] = "extract=exit"
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("deferred", r.stdout)
        self.assertFalse((self.repo / "vault/.vault-meta/frontier-map-ledger.json").exists())

    def test_a_failed_index_refresh_stops_before_any_batch(self):
        # A stale index would hide items earlier batches wrote, so matching never runs on one.
        (self.tmp / "stubbin" / "qmd").write_text('#!/bin/sh\n[ "$1" = update ] && exit 1\nexit 0\n')
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("could not be refreshed", r.stdout)
        self.assertFalse((self.repo / "vault/.vault-meta/frontier-map-ledger.json").exists())
        self.assertEqual(list((self.repo / "vault/frontier/items").glob("K*.md")), [])

    def test_second_run_while_the_frontier_lock_is_held_skips(self):
        import fcntl
        lock = open(self.repo / "vault/.vault-meta/frontier.lock", "a")
        self.addCleanup(lock.close)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        r = self.run_script()
        self.assertEqual(r.returncode, 0)
        self.assertIn("another frontier run holds", r.stdout)
        self.assertFalse((self.repo / "vault/.vault-meta/frontier-map-ledger.json").exists())

    def test_a_busy_wiki_lock_defers_instead_of_hanging(self):
        # a wiki.lock held by someone else delays the apply and the run defers cleanly
        import fcntl
        lock = open(self.repo / "vault/.vault-meta/wiki.lock", "a")
        self.addCleanup(lock.close)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        cfg = (self.repo / "config.yaml").read_text().replace("vault_lock_wait_seconds: 20", "vault_lock_wait_seconds: 1")
        (self.repo / "config.yaml").write_text(cfg)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # waits, defers, does not hang
        self.assertFalse((self.repo / "vault/.vault-meta/frontier-map-ledger.json").exists())
        self.assertIn("wiki.lock stayed busy", r.stdout)


if __name__ == "__main__":
    unittest.main()
