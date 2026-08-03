#!/usr/bin/env python3
"""DoD 6.1 — Transcript Ingestion. Stdlib unittest, no external deps.

Each test maps to a named DoD checkbox so a failure says which gate broke.
Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import convert_claude_export as cc  # noqa: E402


def conversation(uuid: str, name: str, created: str, updated: str,
                 msgs: list[tuple[str, str]]) -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "created_at": created,
        "updated_at": updated,
        "account": {"uuid": "acct"},
        "chat_messages": [
            {"uuid": f"m{i}", "sender": sender, "text": text,
             "content": [{"type": "text", "text": text}],
             "attachments": [], "files": [],
             "created_at": created, "updated_at": created}
            for i, (sender, text) in enumerate(msgs)
        ],
    }


class ConverterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-test-"))
        self.out = self.tmp / "sources"
        self.meta = self.tmp / "meta"
        self.meta.mkdir(parents=True)
        # Redirect the ledger to the sandbox.
        self._orig_p = cc.p
        cc.p = lambda key: self.meta if key == "meta" else self._orig_p(key)

    def tearDown(self) -> None:
        cc.p = self._orig_p
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_export(self, convs: list[dict], as_zip: bool = False) -> Path:
        if as_zip:
            z = self.tmp / "export.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("conversations.json", json.dumps(convs))
            return z
        d = self.tmp / "export"
        d.mkdir(exist_ok=True)
        (d / "conversations.json").write_text(json.dumps(convs), encoding="utf-8")
        return d

    def _convert(self, target: Path, **kw) -> dict:
        opts = {"limit": None, "dry_run": False, "oldest_first": True}
        opts.update(kw)
        return cc.convert(target, self.out, **opts)

    # --- DoD: N conversations in -> N Markdown files with valid frontmatter ---
    def test_zip_end_to_end(self) -> None:
        convs = [
            conversation("u1", "Memory Arch", "2026-07-14T10:00:00Z",
                         "2026-07-14T11:00:00Z", [("human", "how do I persist state?")]),
            conversation("u2", "Router Design", "2026-07-15T10:00:00Z",
                         "2026-07-15T11:00:00Z", [("human", "route by intent?")]),
        ]
        stats = self._convert(self._write_export(convs, as_zip=True))
        self.assertEqual(stats["written"], 2)
        files = sorted(self.out.rglob("*.md"))
        self.assertEqual(len(files), 2)
        fm, body = dc.parse_frontmatter(files[0].read_text(encoding="utf-8"))
        self.assertEqual(fm["conversation_id"], "u1")
        self.assertEqual(fm["source_agent"], "claude.ai")
        self.assertEqual(str(fm["date"]), "2026-07-14")
        self.assertIn("persist state", body)
        # Filename convention YYYY-MM-DD--<slug>.md under YYYY/MM/
        self.assertEqual(files[0].name, "2026-07-14--memory-arch.md")
        self.assertEqual(files[0].parent.parts[-2:], ("2026", "07"))

    # --- DoD: re-running on the same export produces zero new files ---
    def test_rerun_is_idempotent(self) -> None:
        convs = [conversation("u1", "A", "2026-07-14T10:00:00Z",
                              "2026-07-14T11:00:00Z", [("human", "x")])]
        target = self._write_export(convs)
        self._convert(target)
        before = {f: f.read_text() for f in self.out.rglob("*.md")}
        stats = self._convert(target)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["skipped_dedupe"], 1)
        after = {f: f.read_text() for f in self.out.rglob("*.md")}
        self.assertEqual(before, after)

    # --- DoD: a grown conversation is re-emitted once and replaces its file ---
    def test_grown_conversation_replaces(self) -> None:
        c = conversation("u1", "A", "2026-07-14T10:00:00Z",
                         "2026-07-14T11:00:00Z", [("human", "first")])
        self._convert(self._write_export([c]))
        grown = conversation("u1", "A", "2026-07-14T10:00:00Z",
                             "2026-07-14T12:00:00Z",
                             [("human", "first"), ("assistant", "second")])
        stats = self._convert(self._write_export([grown]))
        self.assertEqual(stats["replaced"], 1)
        self.assertEqual(stats["written"], 0)
        files = list(self.out.rglob("*.md"))
        self.assertEqual(len(files), 1, "must replace, not fork")
        self.assertIn("second", files[0].read_text(encoding="utf-8"))

    # --- DoD: a renamed+grown conversation must not leave a stale twin ---
    def test_rename_removes_stale_path(self) -> None:
        c = conversation("u1", "Old Name", "2026-07-14T10:00:00Z",
                         "2026-07-14T11:00:00Z", [("human", "x")])
        self._convert(self._write_export([c]))
        renamed = conversation("u1", "New Name", "2026-07-14T10:00:00Z",
                               "2026-07-14T12:00:00Z", [("human", "x"), ("assistant", "y")])
        self._convert(self._write_export([renamed]))
        files = sorted(f.name for f in self.out.rglob("*.md"))
        self.assertEqual(files, ["2026-07-14--new-name.md"])

    # --- DoD: malformed record is logged and skipped without aborting ---
    def test_malformed_record_does_not_abort(self) -> None:
        convs = [
            conversation("u1", "Good", "2026-07-14T10:00:00Z",
                         "2026-07-14T11:00:00Z", [("human", "ok")]),
            {"name": "no uuid", "chat_messages": []},          # missing uuid
            "not even an object",                               # wrong type
            {"uuid": "u3", "created_at": "not-a-date",
             "chat_messages": [{"sender": "human", "text": "x",
                                "content": [{"type": "text", "text": "x"}]}]},
            conversation("u4", "AlsoGood", "2026-07-16T10:00:00Z",
                         "2026-07-16T11:00:00Z", [("human", "fine")]),
        ]
        stats = self._convert(self._write_export(convs))
        self.assertEqual(stats["written"], 2, "good records must still convert")
        self.assertEqual(stats["skipped_bad"], 3)

    # --- DoD: secrets are redacted before anything reaches sources/ ---
    def test_secrets_redacted_before_write(self) -> None:
        secret_text = (
            "here is the key AKIAIOSFODNN7EXAMPLE and the db "
            "postgresql://alice:hunter2pass@localhost:5432/testdb"
        )
        c = conversation("u1", "Leaky", "2026-07-14T10:00:00Z",
                         "2026-07-14T11:00:00Z", [("human", secret_text)])
        stats = self._convert(self._write_export([c]))
        blob = "\n".join(f.read_text(encoding="utf-8") for f in self.out.rglob("*.md"))
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", blob)
        self.assertNotIn("hunter2pass", blob)
        self.assertIn("[REDACTED:aws-access-key]", blob)
        self.assertIn("[REDACTED:db-dsn-password]", blob)
        self.assertGreaterEqual(sum(stats["redactions"].values()), 2)
        # Surrounding context must survive so the conversation stays legible.
        self.assertIn("postgresql://alice:", blob)
        self.assertIn("localhost:5432/testdb", blob)

    # --- DoD: a traversal-shaped title cannot escape sources/ ---
    def test_traversal_title_is_contained(self) -> None:
        c = conversation("u1", "../../etc/passwd", "2026-07-14T10:00:00Z",
                         "2026-07-14T11:00:00Z", [("human", "x")])
        self._convert(self._write_export([c]))
        files = list(self.out.rglob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].resolve().is_relative_to(self.out.resolve()))
        self.assertNotIn("..", str(files[0].relative_to(self.out)))

    # --- Real-schema trap: empty .text but populated content[] ---
    def test_empty_text_with_content_blocks_is_not_dropped(self) -> None:
        c = {
            "uuid": "u1", "name": "Blocks", "created_at": "2026-07-14T10:00:00Z",
            "updated_at": "2026-07-14T11:00:00Z",
            "chat_messages": [{
                "uuid": "m0", "sender": "human", "text": "",
                "content": [
                    {"type": "thinking", "text": "internal scratchpad"},
                    {"type": "text", "text": "the actual question I asked"},
                ],
                "attachments": [], "files": [],
            }],
        }
        stats = self._convert(self._write_export([c]))
        self.assertEqual(stats["written"], 1)
        blob = "\n".join(f.read_text(encoding="utf-8") for f in self.out.rglob("*.md"))
        self.assertIn("the actual question I asked", blob)
        self.assertNotIn("internal scratchpad", blob, "thinking blocks are noise")

    # --- A conversation with no prose at all must not create an empty page ---
    def test_contentless_conversation_skipped(self) -> None:
        c = {"uuid": "u1", "name": "Empty", "created_at": "2026-07-14T10:00:00Z",
             "updated_at": "2026-07-14T11:00:00Z", "chat_messages": []}
        stats = self._convert(self._write_export([c]))
        self.assertEqual(stats["skipped_empty"], 1)
        self.assertEqual(list(self.out.rglob("*.md")), [])

    # --- Attachments carry pasted docs; they must be included AND redacted ---
    def test_attachment_content_included(self) -> None:
        c = {
            "uuid": "u1", "name": "Att", "created_at": "2026-07-14T10:00:00Z",
            "updated_at": "2026-07-14T11:00:00Z",
            "chat_messages": [{
                "uuid": "m0", "sender": "human", "text": "see attached",
                "content": [{"type": "text", "text": "see attached"}],
                "attachments": [{"file_name": "notes.txt",
                                 "extracted_content": "token ghp_" + "a" * 36}],
                "files": [],
            }],
        }
        self._convert(self._write_export([c]))
        blob = "\n".join(f.read_text(encoding="utf-8") for f in self.out.rglob("*.md"))
        self.assertIn("notes.txt", blob)
        self.assertIn("[REDACTED:github-token]", blob)


class AtomicWriteTest(unittest.TestCase):
    """§6.7 — readers must never observe a partial file."""

    def test_atomic_write_leaves_no_partial(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dreamer-atomic-"))
        try:
            target = tmp / "page.md"
            dc.atomic_write(target, "v1")
            self.assertEqual(target.read_text(), "v1")
            dc.atomic_write(target, "v2-longer-content")
            self.assertEqual(target.read_text(), "v2-longer-content")
            self.assertEqual(list(tmp.glob(".tmp-*")), [], "no temp residue")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failed_write_cleans_up(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dreamer-atomic-"))
        try:
            # Non-str content makes fh.write() raise mid-flight, exercising the
            # cleanup path that must not leave a .part file behind.
            with self.assertRaises(TypeError):
                dc.atomic_write(tmp / "p.md", 12345)  # type: ignore[arg-type]
            self.assertEqual(list(tmp.glob(".tmp-*")), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CollisionTest(ConverterTest):
    """Regression: two conversations sharing a date AND title must not
    overwrite each other. Found against the real 887-conversation archive,
    where 'Cheapest plan for 100k monthly requests' appeared twice on one day."""

    def test_same_date_same_title_both_survive(self) -> None:
        convs = [
            conversation("uuid-aaaaaaaa-1", "Cheapest plan for 100k requests",
                         "2026-06-11T09:00:00Z", "2026-06-11T09:30:00Z",
                         [("human", "first conversation content")]),
            conversation("uuid-bbbbbbbb-2", "Cheapest plan for 100k requests",
                         "2026-06-11T14:00:00Z", "2026-06-11T14:30:00Z",
                         [("human", "second conversation content")]),
        ]
        stats = self._convert(self._write_export(convs))
        self.assertEqual(stats["written"], 2)
        files = sorted(self.out.rglob("*.md"))
        self.assertEqual(len(files), 2, "collision must not destroy a conversation")
        blob = "\n".join(f.read_text(encoding="utf-8") for f in files)
        self.assertIn("first conversation content", blob)
        self.assertIn("second conversation content", blob)

    def test_collision_survivor_is_stable_across_reruns(self) -> None:
        convs = [
            conversation("uuid-aaaaaaaa-1", "Same Title", "2026-06-11T09:00:00Z",
                         "2026-06-11T09:30:00Z", [("human", "a")]),
            conversation("uuid-bbbbbbbb-2", "Same Title", "2026-06-11T14:00:00Z",
                         "2026-06-11T14:30:00Z", [("human", "b")]),
        ]
        target = self._write_export(convs)
        self._convert(target)
        first = sorted(f.name for f in self.out.rglob("*.md"))
        stats = self._convert(target)
        self.assertEqual(stats["written"], 0, "second run must dedupe both")
        self.assertEqual(first, sorted(f.name for f in self.out.rglob("*.md")))
