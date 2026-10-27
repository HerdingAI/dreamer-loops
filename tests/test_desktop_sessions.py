#!/usr/bin/env python3
"""Claude desktop app (Cowork) session ingestion.

Second local transcript source beside Claude Code. Fixtures are modelled on the
app's on-disk shape and contain no real data: every credential-looking string
below is invented and marked fake.

Layout: <account>/<org>/local_<id>.json (metadata) plus
<account>/<org>/local_<id>/audit.jsonl (Agent-SDK stream-json records).

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import apply_cc_session as cca  # noqa: E402
import convert_cc_sessions as ccs  # noqa: E402
import convert_desktop_sessions as cds  # noqa: E402
from dreamer_common import redact  # noqa: E402

NOW = 1_790_000_000.0  # fixed clock
HOUR = 3600.0
# Invented. The shape: the word "password" followed by a space and the value,
# with no `=` or `:` between them.
FAKE_PW = "Example_fake_0000!"


def rec(kind: str, content, *, ts: str = "2029-09-24T19:39:45.290Z", **extra) -> dict:
    r = {"type": kind, "uuid": f"u-{abs(hash((kind, str(content), ts))) % 10**8}",
         "session_id": "cli-1", "parent_tool_use_id": None,
         "timestamp": ts, "_audit_timestamp": ts, "_audit_hmac": "00",
         "message": {"role": kind, "content": content}}
    r.update(extra)
    return r


def human(text: str, **kw) -> dict:
    return rec("user", text, **kw)


def replay(text: str) -> dict:
    return rec("user", text, isReplay=True, origin={"kind": "human"})


def assistant(text: str, **kw) -> dict:
    return rec("assistant", [{"type": "text", "text": text}], **kw)


def long_turns(n: int, *, chars: int = 700, tag: str = "") -> list[dict]:
    out = []
    for i in range(n):
        out.append(human(f"{tag}Question {i}: " + "x" * chars))
        out.append(assistant(f"Answer {i}."))
    return out


class DesktopCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-desktop-test-"))
        self.root = self.tmp / "sessions"
        self.org = self.root / "acct-1" / "org-1"
        self.org.mkdir(parents=True)
        self.cfg = cds.load_cfg()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, sid: str = "local_aaaa", *, records=None, age_hours=48.0,
                meta: dict | None | bool = True, last_activity_age_hours=None,
                archived: bool = False, title: str = "Untitled") -> Path:
        """Write local_<sid>/audit.jsonl (+ metadata json). Returns audit path."""
        d = self.org / sid
        d.mkdir(parents=True, exist_ok=True)
        audit = d / "audit.jsonl"
        audit.write_text("\n".join(json.dumps(r) for r in (records or [])) + "\n",
                         encoding="utf-8")
        stamp = NOW - age_hours * HOUR
        os.utime(audit, (stamp, stamp))
        if meta is not False:
            m = {"sessionId": sid, "title": title, "isArchived": archived,
                 "createdAt": int((stamp - HOUR) * 1000),
                 "lastActivityAt": int((NOW - (last_activity_age_hours
                                                if last_activity_age_hours is not None
                                                else age_hours) * HOUR) * 1000),
                 "cwd": "/sandbox/fake"}
            if isinstance(meta, dict):
                m.update(meta)
            (self.org / f"{sid}.json").write_text(json.dumps(m), encoding="utf-8")
        return audit


class ConversationTest(DesktopCase):
    def convo(self, records):
        return cds.conversation(self.session(records=records), self.cfg)

    def test_replayed_human_turns_are_not_duplicated(self) -> None:
        """On resume the app re-emits earlier user turns with isReplay: true;
        each is a copy of an earlier plain turn."""
        c = self.convo([human("first question"), assistant("a"),
                        replay("first question")])
        self.assertEqual(sum(t["text"].count("first question") for t in c), 1)

    def test_replay_does_not_count_toward_triage(self) -> None:
        recs = [human("q" * 800), replay("q" * 800), replay("q" * 800),
                replay("q" * 800)]
        ok, reason = cds.triage(self.session(records=recs), cds.read_meta(
            self.org / "local_aaaa.json"), self.cfg, now=NOW)
        self.assertFalse(ok)
        self.assertIn("1 human turns", reason)

    def test_tool_results_thinking_and_system_records_are_not_prose(self) -> None:
        c = self.convo([
            human("do the thing"),
            rec("assistant", [{"type": "thinking", "thinking": "SECRET THOUGHT"}]),
            rec("assistant", [{"type": "tool_use", "name": "Bash", "id": "t1",
                               "input": {"command": "cat /etc/passwd"}}]),
            rec("user", [{"type": "tool_result", "tool_use_id": "t1",
                          "content": "TOOL OUTPUT"}]),
            rec("system", None, subtype="init"),
            rec("result", None),
            rec("tool_use_summary", None),
            assistant("finished"),
        ])
        blob = json.dumps(c)
        self.assertNotIn("SECRET THOUGHT", blob)
        self.assertNotIn("TOOL OUTPUT", blob)
        self.assertNotIn("cat /etc/passwd", blob)
        self.assertIn("_[tool: Bash]_", blob)
        self.assertIn("finished", blob)

    def test_synthetic_user_records_are_not_the_owner(self) -> None:
        c = self.convo([human("real"), rec("user", [
            {"type": "text", "text": "INJECTED"}], isSynthetic=True)])
        self.assertNotIn("INJECTED", json.dumps(c))

    def test_records_with_a_parent_tool_use_id_are_subagent_traffic(self) -> None:
        """Mirrors Claude Code's isSidechain rule. The field is null on every
        record seen so far, so this is defensive."""
        c = self.convo([human("owner"), human("subagent", parent_tool_use_id="t9"),
                        assistant("sub reply", parent_tool_use_id="t9")])
        blob = json.dumps(c)
        self.assertIn("owner", blob)
        self.assertNotIn("subagent", blob)
        self.assertNotIn("sub reply", blob)

    def test_uploaded_file_manifests_collapse_to_a_count(self) -> None:
        c = self.convo([human(
            "<uploaded_files>\n<file><file_path>/sandbox/a.pdf</file_path></file>\n"
            "<file><file_path>b.json</file_path></file>\n</uploaded_files>\nsummarise")])
        text = c[0]["text"]
        self.assertNotIn("/sandbox", text)
        self.assertNotIn("a.pdf", text)
        self.assertIn("2 files attached", text)
        self.assertIn("summarise", text)

    def test_consecutive_same_role_records_merge_into_one_turn(self) -> None:
        c = self.convo([human("q"), assistant("one"), assistant("two")])
        self.assertEqual([t["role"] for t in c], ["human", "assistant"])

    def test_timestamp_falls_back_to_audit_timestamp(self) -> None:
        """Older files predate the `timestamp` field."""
        r = human("old shape")
        del r["timestamp"]
        r["_audit_timestamp"] = "2029-05-29T15:30:00.000Z"
        first, last = cds.session_span(self.session(records=[r]))
        self.assertEqual(first[:10], "2029-05-29")
        self.assertEqual(last[:10], "2029-05-29")

    def test_a_truncated_final_line_does_not_lose_the_session(self) -> None:
        a = self.session(records=long_turns(3))
        with a.open("a") as fh:
            fh.write('{"type": "user", "mess')
        self.assertGreaterEqual(len(cds.conversation(a, self.cfg)), 6)


class RedactionTest(DesktopCase):
    """The audit files hold plaintext secrets. Redaction is not optional."""

    def test_redactor_catches_a_password_followed_by_a_space(self) -> None:
        """The generic-assignment pattern requires an `=` or `:` after the
        word, so a space-separated password needs its own pattern."""
        out, counts = redact(f"user alice , password {FAKE_PW} Port : 5432")
        self.assertNotIn(FAKE_PW, out)
        self.assertTrue(counts)
        self.assertIn("Port : 5432", out)

    def test_redactor_leaves_ordinary_prose_about_passwords_alone(self) -> None:
        for s in ("the password reset flow is broken",
                  "password management for the team",
                  "set a password later"):
            self.assertEqual(redact(s)[0], s)

    def test_password_never_reaches_the_conversation(self) -> None:
        c = cds.conversation(self.session(records=[
            human(f'DB: Host : 10.0.0.1 , user alice , password {FAKE_PW} Port : 5432')]),
            self.cfg)
        self.assertNotIn(FAKE_PW, json.dumps(c))

    def test_password_never_reaches_the_summariser_prompt_or_payload(self) -> None:
        recs = [human(f"login is user alice, password {FAKE_PW} " + "x" * 700),
                assistant("ok")] + long_turns(3)
        self.session(records=recs)
        out = self.tmp / "logs"
        out.mkdir()
        cds.scan(self.root, out_dir=out, ledger_path=self.tmp / "l.json",
                 cfg=self.cfg, now=NOW)
        files = list(out.glob(".cc-input-*"))
        self.assertEqual(len(files), 2)
        for f in files:
            self.assertNotIn(FAKE_PW, f.read_text(), f.name)
            self.assertNotIn("Example_fake", f.read_text(), f.name)

    def test_other_secret_shapes_are_redacted_too(self) -> None:
        key = "sk-ant-" + "EXAMPLE0000fake" * 2
        c = cds.conversation(self.session(records=[
            human(f"key {key}"), human("api_key = EXAMPLE0000fake")]), self.cfg)
        self.assertNotIn(key, json.dumps(c))
        self.assertNotIn("EXAMPLE0000fake", json.dumps(c))

    def test_a_page_echoing_the_password_is_refused_and_not_written(self) -> None:
        """Belt and braces: if the summariser reproduces a secret, apply
        refuses, exactly as it does for Claude Code pages."""
        payload = {"session_id": "local_x", "project": "desktop",
                   "date": "2029-09-24", "updated_at": "2029-09-24T20:00:00Z",
                   "human_turns": 5, "truncated": False, "turns": [],
                   "source_agent": "claude-desktop"}
        summary = {"title": "Db setup", "goal": f"password {FAKE_PW}",
                   "solution": "s", "outcome": "o", "unresolved": "u",
                   "turns": [{"role": "human", "text": "hi"}]}
        sources = self.tmp / "transcripts"
        with self.assertRaises(ValueError):
            cca.apply_session(payload, json.dumps(summary), sources_dir=sources,
                              ledger_path=self.tmp / "l.json")
        self.assertEqual(list(sources.rglob("*.md")) if sources.exists() else [], [])


class TriageTest(DesktopCase):
    def triage(self, audit: Path, meta=None):
        m = cds.read_meta(self.org / f"{audit.parent.name}.json")
        return cds.triage(audit, m, self.cfg, now=NOW)

    def test_a_real_conversation_is_accepted(self) -> None:
        ok, reason = self.triage(self.session(records=long_turns(5)))
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_too_few_human_turns_is_rejected_with_reason(self) -> None:
        ok, reason = self.triage(self.session(records=long_turns(2)))
        self.assertFalse(ok)
        self.assertIn("2 human turns", reason)

    def test_too_few_human_chars_is_rejected_with_reason(self) -> None:
        ok, reason = self.triage(self.session(records=[
            human("ok"), human("yes"), human("thanks")]))
        self.assertFalse(ok)
        self.assertIn("human chars", reason)

    def test_quiet_window_uses_last_activity_from_metadata(self) -> None:
        """The audit file is old but the app says the session was active 1h
        ago (e.g. work in the outputs folder): still active."""
        a = self.session(records=long_turns(5), age_hours=100,
                         last_activity_age_hours=1)
        ok, reason = self.triage(a)
        self.assertFalse(ok)
        self.assertIn("still active", reason)

    def test_quiet_window_uses_file_mtime_when_it_is_newer(self) -> None:
        a = self.session(records=long_turns(5), age_hours=1,
                         last_activity_age_hours=100)
        self.assertIn("still active", self.triage(a)[1])

    def test_quiet_window_falls_back_to_mtime_without_metadata(self) -> None:
        recent = self.session("local_new", records=long_turns(5), age_hours=1,
                              meta=False)
        old = self.session("local_old", records=long_turns(5), age_hours=48,
                           meta=False)
        self.assertFalse(self.triage(recent)[0])
        self.assertTrue(self.triage(old)[0])

    def test_unreadable_metadata_does_not_reject_the_session(self) -> None:
        a = self.session(records=long_turns(5))
        (self.org / "local_aaaa.json").write_text("{not json")
        self.assertTrue(self.triage(a)[0])

    def test_archived_sessions_are_ingested(self) -> None:
        """Claude Code has no archive concept, so no equivalent rule exists.
        Archiving in the app is tidying, not a verdict on the content."""
        a = self.session(records=long_turns(5), archived=True)
        self.assertTrue(self.triage(a)[0])


class ScanTest(DesktopCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = self.tmp / "logs"
        self.out.mkdir()
        self.ledger = self.tmp / "desktop-ingested.json"

    def scan(self, **kw):
        return cds.scan(self.root, out_dir=self.out, ledger_path=self.ledger,
                        cfg=self.cfg, now=NOW, **kw)

    def led(self) -> dict:
        return json.loads(self.ledger.read_text())

    def test_only_audit_files_at_the_expected_depth_are_sessions(self) -> None:
        self.session("local_a", records=long_turns(5))
        # The nested Claude Code jsonl each session dir carries, and other dirs.
        nested = self.org / "local_a" / ".claude" / "projects" / "p"
        nested.mkdir(parents=True)
        (nested / "uuid.jsonl").write_text(json.dumps(human("dup")) + "\n")
        (self.root / "skills-plugin" / "org").mkdir(parents=True)
        (self.root / "skills-plugin" / "org" / "audit.jsonl").write_text("{}\n")
        self.assertEqual(self.scan()["scanned"], 1)

    def test_the_long_lived_agent_session_is_excluded_by_shape(self) -> None:
        """<org>/agent/local_ditto_*/ is one persistent session that grows for
        months. Re-summarising it on every run is the regrown-transcript
        problem at its worst, so it is left out."""
        d = self.org / "agent" / "local_ditto_x"
        d.mkdir(parents=True)
        (d / "audit.jsonl").write_text(
            "\n".join(json.dumps(r) for r in long_turns(5)) + "\n")
        self.assertEqual(self.scan()["scanned"], 0)

    def test_accepted_session_gets_payload_prompt_and_pending_entry(self) -> None:
        self.session("local_a", records=long_turns(5), title="Kite building")
        self.assertEqual(self.scan()["accepted"], 1)
        payload = json.loads((self.out / ".cc-input-local_a.json").read_text())
        self.assertEqual(payload["source_agent"], "claude-desktop")
        self.assertEqual(payload["session_id"], "local_a")
        self.assertEqual(payload["ledger"], "desktop-ingested.json")
        self.assertEqual(payload["date"], "2029-09-24")
        self.assertEqual(payload["human_turns"], 5)
        self.assertEqual(payload["ai_title"], "Kite building")
        self.assertEqual(self.led()["local_a"]["status"], "pending")
        prompt = (self.out / ".cc-input-local_a.md").read_text()
        self.assertIn("<session>", prompt)
        self.assertIn("Claude desktop", prompt)

    def test_every_rejection_is_recorded_with_its_reason(self) -> None:
        self.session("local_few", records=long_turns(1))
        self.session("local_busy", records=long_turns(5), age_hours=1)
        stats = self.scan()
        self.assertEqual((stats["accepted"], stats["rejected"]), (0, 2))
        led = self.led()
        self.assertEqual(led["local_few"]["status"], "rejected")
        self.assertIn("human turns", led["local_few"]["reason"])
        self.assertIn("still active", led["local_busy"]["reason"])

    def test_unchanged_terminal_sessions_are_skipped(self) -> None:
        self.session("local_few", records=long_turns(1))
        self.scan()
        self.assertEqual(self.scan()["skipped"], 1)

    def test_pending_is_retried(self) -> None:
        self.session("local_a", records=long_turns(5))
        self.scan()
        self.assertEqual(self.scan()["accepted"], 1)

    def test_a_session_that_grew_is_triaged_again(self) -> None:
        """Same behaviour as Claude Code: the (mtime, size) pair is the
        ledger's change key, and an ingested session whose file moved is
        accepted again and re-summarised. The page-rewrite hazard on a changed
        title belongs to the shared regrown-transcript fix, not to this source."""
        a = self.session("local_a", records=long_turns(5))
        self.scan()
        entry = self.led()["local_a"]
        entry.update(status="ingested")
        self.ledger.write_text(json.dumps({"local_a": entry}))
        self.assertEqual(self.scan()["skipped"], 1)
        with a.open("a") as fh:
            fh.write(json.dumps(human("one more " + "y" * 700)) + "\n")
        os.utime(a, (NOW - 48 * HOUR, NOW - 48 * HOUR))
        stats = self.scan()
        self.assertEqual((stats["skipped"], stats["accepted"]), (0, 1))

    def test_duplicate_conversations_are_ingested_once(self) -> None:
        """Two sessions that open with the same prompt: identical human
        content is one conversation filed twice."""
        recs = long_turns(5, tag="dup ")
        self.session("local_a", records=recs)
        self.session("local_b", records=recs)
        stats = self.scan()
        self.assertEqual(stats["accepted"], 1)
        reasons = [e["reason"] for e in self.led().values() if e["status"] == "rejected"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("duplicate of local_a", reasons[0])

    def test_archived_flag_is_recorded_not_filtered(self) -> None:
        self.session("local_a", records=long_turns(5), archived=True)
        self.assertEqual(self.scan()["accepted"], 1)
        self.assertTrue(self.led()["local_a"]["archived"])

    def test_limit_caps_accepted_sessions(self) -> None:
        for i in range(3):
            self.session(f"local_{i}", records=long_turns(5, tag=f"{i} "))
        self.assertEqual(self.scan(limit=2)["accepted"], 2)

    def test_dry_run_writes_nothing(self) -> None:
        self.session("local_a", records=long_turns(5))
        self.assertEqual(self.scan(dry_run=True)["accepted"], 1)
        self.assertFalse(self.ledger.exists())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_a_missing_root_is_an_empty_scan_not_a_crash(self) -> None:
        stats = cds.scan(self.tmp / "nope", out_dir=self.out,
                         ledger_path=self.ledger, cfg=self.cfg, now=NOW)
        self.assertEqual(stats["scanned"], 0)

    def test_session_files_never_read_the_credential_files(self) -> None:
        """Session dirs hold .audit-key and .claude/.credentials.json. Only
        audit.jsonl and the metadata json may be opened."""
        self.session("local_a", records=long_turns(5))
        (self.org / "local_a" / ".audit-key").write_text("KEYMATERIAL")
        opened: list[str] = []
        real = Path.open

        def spy(self_, *a, **k):
            opened.append(self_.name)
            return real(self_, *a, **k)

        Path.open = spy  # type: ignore[method-assign]
        try:
            self.scan()
        finally:
            Path.open = real  # type: ignore[method-assign]
        self.assertTrue(set(opened) <= {"audit.jsonl", "local_a.json", "PROMPT.md"}, opened)


class ConfigTest(unittest.TestCase):
    def test_default_root_comes_from_config_not_code(self) -> None:
        cfg = {"corpora": {"claude_desktop_sessions": "/cfg/path"}}
        self.assertEqual(cds.default_root(cfg), Path("/cfg/path"))

    def test_default_root_expands_a_tilde(self) -> None:
        cfg = {"corpora": {"claude_desktop_sessions": "~/sessions-test"}}
        self.assertEqual(cds.default_root(cfg), Path.home() / "sessions-test")

    def test_default_root_without_the_key_is_under_the_home_dir(self) -> None:
        self.assertEqual(cds.default_root({"corpora": {}}),
                         Path.home() / ".config" / "Claude" / "local-agent-mode-sessions")

    def test_real_config_declares_the_root_and_a_separate_ledger(self) -> None:
        from dreamer_common import CFG
        self.assertIn("claude_desktop_sessions", CFG["corpora"])
        self.assertEqual(cds.load_cfg()["ledger"], "desktop-ingested.json")

    def test_desktop_inherits_the_claude_code_floors(self) -> None:
        cfg = cds.load_cfg()
        for k in ("min_human_turns", "min_human_chars", "min_quiet_hours"):
            self.assertIn(k, cfg)


class ApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-desktop-apply-"))
        self.sources = self.tmp / "transcripts"
        self.ledger = self.tmp / "desktop-ingested.json"
        self.payload = {"session_id": "local_abc", "project": "desktop",
                        "date": "2029-09-24", "updated_at": "2029-09-24T20:00:00Z",
                        "human_turns": 9, "truncated": False, "turns": [],
                        "source_agent": "claude-desktop",
                        "ledger": "desktop-ingested.json"}
        self.summary = {"title": "Birdhouse plans", "goal": "Pick one.",
                        "solution": "Compared three.", "outcome": "Chose one.",
                        "unresolved": "not established",
                        "turns": [{"role": "human", "text": "Which one?"}]}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_page_carries_the_desktop_source_agent(self) -> None:
        path = cca.apply_session(self.payload, json.dumps(self.summary),
                                 sources_dir=self.sources, ledger_path=self.ledger)
        text = path.read_text()
        self.assertIn("source_agent: claude-desktop", text)
        self.assertNotIn("source_agent: claude-code", text)
        self.assertIn("session_id: local_abc", text)

    def test_claude_code_payloads_still_default_to_claude_code(self) -> None:
        for k in ("source_agent", "ledger"):
            self.payload.pop(k)
        path = cca.apply_session(self.payload, json.dumps(self.summary),
                                 sources_dir=self.sources, ledger_path=self.ledger)
        self.assertIn("source_agent: claude-code", path.read_text())

    def test_default_ledger_follows_the_payload(self) -> None:
        self.assertEqual(cca.default_ledger_name(self.payload), "desktop-ingested.json")
        self.assertEqual(cca.default_ledger_name({}), "cc-ingested.json")


if __name__ == "__main__":
    unittest.main()
