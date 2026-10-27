#!/usr/bin/env python3
"""Native-messaging host for the Dreamer Sync extension: framing, every
command, the atomic drop (read back by the REAL converter in a temp vault),
first-run cursor, and the state-file transition for every status."""

from __future__ import annotations

import copy
import datetime as _dt
import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import claude_ai_host as H  # noqa: E402
import convert_claude_export as cc  # noqa: E402
import dreamer_common as dc  # noqa: E402
import freshness as F  # noqa: E402

T0 = _dt.datetime(2030, 1, 14, 12, 0, 0, tzinfo=_dt.timezone.utc)


def chat(uuid: str, updated: str, text: str = "hello there", n: int = 1) -> dict:
    return {"uuid": uuid, "name": f"Chat {uuid}", "summary": "", "created_at": "2029-12-21T09:00:00.000000Z",
            "updated_at": updated, "model": "x", "settings": {},
            "chat_messages": [
                {"uuid": f"{uuid}-m{i}", "text": "", "sender": "human" if i % 2 == 0 else "assistant",
                 "content": [{"type": "text", "text": f"{text} {i}"}], "attachments": [], "files": [],
                 "created_at": "2029-12-21T09:00:00.000000Z", "updated_at": "2029-12-21T09:00:00.000000Z",
                 "parent_message_uuid": None, "index": i, "truncated": False}
                for i in range(n)]}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-host-"))
        self.meta, self.inbox = self.tmp / "meta", self.tmp / "to_ingest"
        self.meta.mkdir()
        self.clock = [T0]
        self.cfg = dict(dc.CFG["claude_ai_sync"])
        self.host = self.new_host()

    def new_host(self) -> H.Host:
        return H.Host(cfg=self.cfg, meta=self.meta, inbox=self.inbox,
                      logfile=self.tmp / "host.log", now=lambda: self.clock[0],
                      spool_root=self.tmp)

    def tearDown(self) -> None:
        self.host.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self) -> dict:
        return json.loads((self.meta / H.STATE_FILE).read_text())

    def drops(self) -> list[Path]:
        return sorted(self.inbox.glob("claude-ai-sync-*.json")) if self.inbox.exists() else []


class FramingTest(Base):
    def frame(self, obj) -> bytes:
        b = json.dumps(obj).encode()
        return struct.pack("=I", len(b)) + b

    def test_round_trip_and_eof(self) -> None:
        buf = io.BytesIO(self.frame({"cmd": "status", "id": 3, "t": "é✓"}))
        self.assertEqual(H.read_message(buf)["t"], "é✓")
        self.assertIsNone(H.read_message(buf))
        out = io.BytesIO()
        H.write_message(out, {"ok": True, "t": "é"})
        raw = out.getvalue()
        self.assertEqual(struct.unpack("=I", raw[:4])[0], len(raw) - 4)
        self.assertEqual(json.loads(raw[4:]), {"ok": True, "t": "é"})

    def test_bad_frames(self) -> None:
        for raw in (b"\x01\x00", struct.pack("=I", 10) + b"abc",
                    struct.pack("=I", H.MAX_MESSAGE + 1), self.frame([1, 2])[:4] + b"[1,2]"):
            with self.assertRaises(ValueError):
                H.read_message(io.BytesIO(raw))

    def test_serve_answers_each_message_with_its_id(self) -> None:
        stdin = io.BytesIO(self.frame({"cmd": "status", "id": 1}) + self.frame({"cmd": "nope", "id": 2}))
        stdout = io.BytesIO()
        H.serve(stdin, stdout, self.host)
        stdout.seek(0)
        a, b = H.read_message(stdout), H.read_message(stdout)
        self.assertEqual((a["id"], a["ok"]), (1, True))
        self.assertEqual((b["id"], b["ok"]), (2, False))

    def test_serve_survives_a_garbage_frame(self) -> None:
        stdout = io.BytesIO()
        H.serve(io.BytesIO(struct.pack("=I", 5) + b"not{j"), stdout, self.host)
        stdout.seek(0)
        self.assertFalse(H.read_message(stdout)["ok"])


class StatusTest(Base):
    def test_cursor_init_from_ledger_max(self) -> None:
        (self.meta / "ingested.json").write_text(json.dumps({
            "a": {"updated_at": "2029-11-21T10:00:00.5Z", "path": "x"},
            "b": {"updated_at": "2030-01-09T08:00:00.123456Z", "path": "y"},
            "c": {"updated_at": "garbage"}, "d": "not-a-dict"}))
        r = self.host.handle({"cmd": "status"})
        self.assertEqual(r["cursor"], "2030-01-09T08:00:00.123456Z")
        self.assertTrue(r["due"])
        self.assertEqual(r["settings"]["quiet_hours"], self.cfg["quiet_hours"])
        self.assertNotIn("python", r["settings"])
        self.assertFalse((self.meta / H.STATE_FILE).exists())  # status is not an attempt

    def test_no_ledger_no_state_means_no_cursor(self) -> None:
        self.assertIsNone(self.host.handle({"cmd": "status"})["cursor"])

    def test_state_cursor_wins_over_ledger(self) -> None:
        (self.meta / "ingested.json").write_text(json.dumps({"a": {"updated_at": "2030-01-09T08:00:00Z"}}))
        (self.meta / H.STATE_FILE).write_text(json.dumps({"cursor": "2030-01-13T08:00:00Z"}))
        self.assertEqual(self.host.handle({"cmd": "status"})["cursor"], "2030-01-13T08:00:00Z")

    def test_due_rules(self) -> None:
        def due(**st) -> bool:
            (self.meta / H.STATE_FILE).write_text(json.dumps(st))
            return self.host.handle({"cmd": "status"})["due"]
        ago = lambda h: H.iso(T0 - _dt.timedelta(hours=h))  # noqa: E731
        self.assertFalse(due(status="ok", last_ok=ago(19), last_attempt=ago(19)))
        self.assertTrue(due(status="ok", last_ok=ago(21), last_attempt=ago(21)))
        self.assertTrue(due(status="ok", last_ok=None, last_attempt=ago(1)))
        # failed 1h ago: backs off; failed 4h ago with a stale last_ok: retry
        self.assertFalse(due(status="blocked", last_ok=ago(30), last_attempt=ago(1)))
        self.assertTrue(due(status="needs_login", last_ok=ago(30), last_attempt=ago(4)))
        # a capped run leaves a backlog: due again despite a fresh last_ok
        self.assertTrue(due(status="ok", last_ok=ago(1), last_attempt=ago(1), pending=True))
        self.assertFalse(due(status="ok", last_ok=ago(1), last_attempt=ago(1), pending=False))


class LoginExpiryTest(Base):
    """The extension sends the sessionKey cookie's expiry (never its value)
    with every hourly status call; the host keeps it in the state file."""

    def status(self, **extra) -> dict:
        reply = self.host.handle({"cmd": "status", **extra})
        self.assertTrue(reply["ok"], reply)
        return reply

    def test_expiry_is_recorded_in_utc(self) -> None:
        self.status(login_expires_at="2030-02-24T14:15:00+05:00")
        self.assertEqual(self.state()["login_expires_at"], "2030-02-24T09:15:00Z")

    def test_null_means_no_login_and_is_recorded(self) -> None:
        self.status(login_expires_at="2030-02-24T09:15:00Z")
        self.status(login_expires_at=None)
        self.assertIsNone(self.state()["login_expires_at"])

    def test_absent_key_leaves_the_recorded_value(self) -> None:
        self.status(login_expires_at="2030-02-24T09:15:00Z")
        self.status()
        self.assertEqual(self.state()["login_expires_at"], "2030-02-24T09:15:00Z")

    def test_garbage_is_ignored_and_the_status_call_still_answers(self) -> None:
        self.status(login_expires_at="2030-02-24T09:15:00Z")
        for bad in ("soon", 17, {"x": 1}):
            with self.subTest(bad=bad):
                self.status(login_expires_at=bad)
                self.assertEqual(self.state()["login_expires_at"], "2030-02-24T09:15:00Z")

    def test_unchanged_value_does_not_rewrite_the_state(self) -> None:
        self.status(login_expires_at="2030-02-24T09:15:00Z")
        writes = []
        orig = self.host.write_state
        self.host.write_state = lambda **f: writes.append(f) or orig(**f)
        self.status(login_expires_at="2030-02-24T09:15:00Z")
        self.assertEqual(writes, [])

    def test_recording_expiry_does_not_touch_the_run_fields(self) -> None:
        self.host.handle({"cmd": "report", "status": "needs_login", "detail": "x"})
        before = {k: v for k, v in self.state().items() if k != "login_expires_at"}
        self.status(login_expires_at="2030-02-24T09:15:00Z")
        after = {k: v for k, v in self.state().items() if k != "login_expires_at"}
        self.assertEqual(before, after)

    def test_freshness_reads_what_the_host_wrote(self) -> None:
        # End to end through the real state file: 2 days left -> warning.
        self.status(login_expires_at=H.iso(T0 + _dt.timedelta(days=2)))
        self.host.handle({"cmd": "commit", "count": 0, "cursor": "2030-01-13T08:20:10Z"})
        # extension_id: the claude.ai axes are only judged once the sync is set up.
        with unittest.mock.patch.dict(dc.CFG["paths"], {"meta": str(self.meta)}), \
                unittest.mock.patch.dict(dc.CFG["claude_ai_sync"], {"extension_id": "a" * 32}):
            now = T0.astimezone().replace(tzinfo=None)
            a = {x["axis"]: x for x in F.collect(now)["axes"]}["source.claude-ai"]
        self.assertIn(a["status"], F.FAILING)
        self.assertIn("login expires in 2.0 days", a["reason"])


class ConversationCommitTest(Base):
    def test_conversation_validates(self) -> None:
        for bad in ({}, {"uuid": "a"}, {"uuid": "../x", "updated_at": "2029-12-21T00:00:00Z", "chat_messages": []},
                    {"uuid": "a", "updated_at": "nope", "chat_messages": []}):
            self.assertFalse(self.host.handle({"cmd": "conversation", "chat": bad})["ok"])
        self.assertFalse(self.host.handle({"cmd": "conversation"})["ok"])

    def test_commit_writes_one_atomic_drop_the_converter_reads(self) -> None:
        c1, c2 = chat("u1", "2029-12-30T10:00:00.000000Z", n=2), chat("u2", "2030-01-01T10:00:00.000000Z", n=3)
        for c in (c2, c1):
            self.assertTrue(self.host.handle({"cmd": "conversation", "chat": copy.deepcopy(c)})["ok"])
        r = self.host.handle({"cmd": "commit", "cursor": c2["updated_at"], "count": 2, "id": 9})
        self.assertEqual((r["ok"], r["fetched"], r["id"]), (True, 2, 9))
        drops = self.drops()
        self.assertEqual(len(drops), 1)
        self.assertRegex(drops[0].name, r"^claude-ai-sync-\d{8}T\d{6}Z\.json$")
        self.assertEqual(list(self.inbox.glob(".tmp-*")), [])           # no stray temp files
        data = json.loads(drops[0].read_text())
        self.assertEqual([c["uuid"] for c in data], ["u1", "u2"])        # oldest first, export-shaped list
        self.assertTrue(all("account" in c for c in data))
        self.assertEqual(list(self.host.spool.glob("*.json")), [])       # spool emptied
        self.assert_converter_reads(drops[0], ["u1", "u2"])

    def assert_converter_reads(self, drop: Path, uuids: list[str]) -> None:
        vault = self.tmp / "vault"
        saved = copy.deepcopy(dc.CFG["paths"])
        try:
            for k, sub in (("vault", ""), ("meta", ".vault-meta"), ("sources", "sources/transcripts")):
                dc.CFG["paths"][k] = str(vault / sub) if sub else str(vault)
            (vault / ".vault-meta").mkdir(parents=True)
            stats = cc.convert(drop, vault / "sources/transcripts", None, False, True)
            self.assertEqual((stats["written"], stats["skipped_bad"]), (len(uuids), 0))
            ledger = json.loads((vault / ".vault-meta/ingested.json").read_text())
            self.assertEqual(sorted(ledger), sorted(uuids))
            pages = list((vault / "sources/transcripts").rglob("*.md"))
            self.assertEqual(len(pages), len(uuids))
            self.assertIn("hello there", pages[0].read_text())
            # re-running the same drop changes nothing (dedupe ledger)
            again = cc.convert(drop, vault / "sources/transcripts", None, False, True)
            self.assertEqual((again["written"], again["skipped_dedupe"]), (0, len(uuids)))
        finally:
            dc.CFG["paths"].clear()
            dc.CFG["paths"].update(saved)

    def test_commit_count_mismatch_fails_and_moves_nothing(self) -> None:
        self.host.handle({"cmd": "conversation", "chat": chat("u1", "2029-12-30T10:00:00Z")})
        r = self.host.handle({"cmd": "commit", "cursor": "2029-12-30T10:00:00Z", "count": 2})
        self.assertFalse(r["ok"])
        self.assertEqual(self.drops(), [])
        self.assertFalse((self.meta / H.STATE_FILE).exists())

    def test_commit_rejects_bad_cursor(self) -> None:
        self.assertFalse(self.host.handle({"cmd": "commit", "cursor": "soon", "count": 0})["ok"])
        self.host.handle({"cmd": "conversation", "chat": chat("u1", "2029-12-30T10:00:00Z")})
        self.assertFalse(self.host.handle({"cmd": "commit", "cursor": None, "count": 1})["ok"])

    def test_commit_with_nothing_new_is_ok_and_writes_no_drop(self) -> None:
        (self.meta / H.STATE_FILE).write_text(json.dumps({"cursor": "2030-01-09T00:00:00Z"}))
        r = self.host.handle({"cmd": "commit", "cursor": "2030-01-09T00:00:00Z", "count": 0})
        self.assertTrue(r["ok"])
        self.assertEqual(self.drops(), [])
        st = self.state()
        self.assertEqual((st["status"], st["fetched"], st["cursor"]), ("ok", 0, "2030-01-09T00:00:00Z"))

    def test_two_commits_in_one_second_do_not_collide(self) -> None:
        for u in ("a", "b"):
            self.host.handle({"cmd": "conversation", "chat": chat(u, "2029-12-30T10:00:00Z")})
            self.assertTrue(self.host.handle({"cmd": "commit", "cursor": "2029-12-30T10:00:00Z", "count": 1})["ok"])
        self.assertEqual(len(self.drops()), 2)

    def test_cursor_never_moves_backwards(self) -> None:
        (self.meta / H.STATE_FILE).write_text(json.dumps({"cursor": "2030-01-09T00:00:00Z"}))
        self.host.handle({"cmd": "commit", "cursor": "2029-12-21T00:00:00Z", "count": 0})
        self.assertEqual(self.state()["cursor"], "2030-01-09T00:00:00Z")


class StateTransitionTest(Base):
    def test_ok_commit_sets_every_contract_field(self) -> None:
        self.host.handle({"cmd": "conversation", "chat": chat("u1", "2029-12-30T10:00:00Z")})
        self.host.handle({"cmd": "commit", "cursor": "2029-12-30T10:00:00Z", "count": 1,
                          "pending": True, "detail": "fetched 1; deferred 0 quiet, 4 over cap"})
        st = self.state()
        self.assertEqual(st, {"last_attempt": "2030-01-14T12:00:00Z", "last_ok": "2030-01-14T12:00:00Z",
                              "status": "ok", "detail": "fetched 1; deferred 0 quiet, 4 over cap",
                              "cursor": "2029-12-30T10:00:00Z", "fetched": 1, "converted": 0, "pending": True})

    def test_every_failure_status_keeps_last_ok_and_cursor(self) -> None:
        ok_at = "2030-01-13T18:00:00Z"
        for status in H.FAILURES:
            (self.meta / H.STATE_FILE).write_text(json.dumps(
                {"status": "ok", "last_ok": ok_at, "last_attempt": ok_at, "cursor": "2030-01-09T00:00:00Z",
                 "fetched": 5, "converted": 5}))
            r = self.host.handle({"cmd": "report", "status": status, "detail": f"why {status}"})
            self.assertTrue(r["ok"])
            st = self.state()
            self.assertEqual(st["status"], status)
            self.assertEqual(st["last_ok"], ok_at)
            self.assertEqual(st["cursor"], "2030-01-09T00:00:00Z")
            self.assertEqual(st["last_attempt"], "2030-01-14T12:00:00Z")
            self.assertEqual((st["fetched"], st["converted"]), (0, 0))
            self.assertEqual(st["detail"], f"why {status}")
            self.assertEqual(self.drops(), [])

    def test_unknown_status_becomes_error_and_never_ok(self) -> None:
        self.host.handle({"cmd": "report", "status": "ok", "detail": "sneaky"})
        self.assertEqual(self.state()["status"], "error")

    def test_failure_after_delivery_discards_the_spool(self) -> None:
        self.host.handle({"cmd": "conversation", "chat": chat("u1", "2029-12-30T10:00:00Z")})
        self.host.handle({"cmd": "report", "status": "error", "detail": "mid-run"})
        self.assertEqual(self.drops(), [])
        self.assertEqual(list(self.host.spool.glob("*.json")), [])

    def test_first_failure_with_no_state_records_cursor_from_ledger(self) -> None:
        (self.meta / "ingested.json").write_text(json.dumps({"a": {"updated_at": "2030-01-09T08:00:00Z"}}))
        self.host.handle({"cmd": "report", "status": "needs_login", "detail": "no cookie"})
        st = self.state()
        self.assertEqual((st["cursor"], st["last_ok"]), ("2030-01-09T08:00:00Z", None))

    def test_detail_is_truncated(self) -> None:
        self.host.handle({"cmd": "report", "status": "error", "detail": "x" * 5000})
        self.assertEqual(len(self.state()["detail"]), H.DETAIL_MAX)

    def test_freshness_axis_reads_every_state(self) -> None:
        """The state this host writes is what source.claude-ai judges."""
        spec = next(s for s in F.AXES if s["axis"] == "source.claude-ai")
        saved = copy.deepcopy(dc.CFG["paths"])
        try:
            dc.CFG["paths"]["meta"] = str(self.meta)
            self.host.handle({"cmd": "commit", "cursor": None, "count": 0})
            now = T0.replace(tzinfo=None) + _dt.timedelta(minutes=1)
            ok = F._rule_claude_ai_sync(spec, {}, {}, now)
            self.assertEqual(ok["status"], "ok")
            self.host.handle({"cmd": "report", "status": "needs_login", "detail": "x"})
            bad = F._rule_claude_ai_sync(spec, {}, {}, now)
            self.assertEqual(bad["status"], "stale")
            self.assertIn("needs re-login", bad["reason"])
        finally:
            dc.CFG["paths"].clear()
            dc.CFG["paths"].update(saved)


class SyncScriptTest(unittest.TestCase):
    """bin/sync-claude-ai.sh in a hermetic copy of the tree (config paths point
    at a temp dir, so nothing touches the live vault). Covers the extraction
    race guard: the conversion must wait for wiki.lock, and when the wait runs
    out it converts nothing, leaves the drop and exits non-zero."""

    def setUp(self) -> None:
        import os
        import subprocess
        self.subprocess, self.os = subprocess, os
        self.root = Path(tempfile.mkdtemp(prefix="dreamer-sync-sh-"))
        shutil.copytree(ROOT / "bin", self.root / "bin")
        shutil.copytree(ROOT / "scripts", self.root / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        # Relative paths resolve against paths.root; pin it to the sandbox.
        cfg["paths"]["root"] = str(self.root)
        (self.root / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
        self.meta = self.root / "vault/.vault-meta"
        for d in (self.meta, self.root / "vault/sources/transcripts", self.root / "logs", self.root / "to_ingest"):
            d.mkdir(parents=True, exist_ok=True)
        (self.meta / H.STATE_FILE).write_text(json.dumps({"status": "ok", "fetched": 1, "converted": 0}))
        self.drop = self.root / "to_ingest/claude-ai-sync-20300114T120000Z.json"
        self.drop.write_text(json.dumps([chat("u1", "2029-12-30T10:00:00.000000Z")]))
        self.env = dict(self.os.environ, DREAMER_PG_ENV="/nonexistent", DREAMER_LOCK_HELD="")
        self.env.pop("DREAMER_LOCK_HELD")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def run_script(self, wait: int) -> "subprocess.CompletedProcess":
        cfg = (self.root / "config.yaml").read_text()
        import re as _re
        (self.root / "config.yaml").write_text(
            _re.sub(r"lock_wait_seconds: *\d+", f"lock_wait_seconds: {wait}", cfg))
        return self.subprocess.run([str(self.root / "bin/sync-claude-ai.sh")], env=self.env,
                                   capture_output=True, text=True, timeout=60)

    def holder(self, seconds: float):
        return self.subprocess.Popen(
            ["flock", str(self.meta / "wiki.lock"), "sleep", str(seconds)])

    def state(self) -> dict:
        return json.loads((self.meta / H.STATE_FILE).read_text())

    def test_converts_when_lock_is_free(self) -> None:
        r = self.run_script(5)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.drop.exists())
        self.assertEqual(len(list((self.root / "to_ingest/processed").glob("*claude-ai-sync-*"))), 1)
        self.assertEqual(len(list((self.root / "vault/sources/transcripts").rglob("*.md"))), 1)
        self.assertEqual(self.state()["converted"], 1)

    def test_nothing_to_convert_exits_zero_without_the_lock(self) -> None:
        self.drop.unlink()
        holder = self.holder(5)
        try:
            import time
            time.sleep(0.3)
            r = self.run_script(1)
        finally:
            holder.terminate()
            holder.wait()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_lock_wait_timeout_leaves_the_drop_and_fails(self) -> None:
        import time
        holder = self.holder(10)
        try:
            time.sleep(0.3)
            r = self.run_script(1)
        finally:
            holder.terminate()
            holder.wait()
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.drop.exists(), "drop must stay for the next run")
        self.assertEqual(list((self.root / "vault/sources/transcripts").rglob("*.md")), [])
        self.assertFalse((self.meta / "ingested.json").exists(), "converter must not have run")
        self.assertIn("vault lock still held", r.stdout + r.stderr)
        self.assertEqual(self.state()["converted"], 0)

    def test_waits_for_the_lock_then_converts(self) -> None:
        import time
        holder = self.holder(2)
        try:
            time.sleep(0.3)
            t0 = time.monotonic()
            r = self.run_script(30)
            waited = time.monotonic() - t0
        finally:
            holder.terminate()
            holder.wait()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGreater(waited, 1.0, "should have blocked on the lock")
        self.assertFalse(self.drop.exists())
        self.assertEqual(self.state()["converted"], 1)

    def test_conversion_failure_is_nonzero_and_keeps_the_drop(self) -> None:
        self.drop.write_text("not json")
        r = self.run_script(5)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.drop.exists())


if __name__ == "__main__":
    unittest.main()
