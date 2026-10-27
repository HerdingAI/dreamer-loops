#!/usr/bin/env python3
"""Frontier sync with the show (release ceiling, taps, conflicts),
against a fake show on 127.0.0.1. Every paths.* is patched to a temp dir; nothing touches the vault."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dreamer_common as dc  # noqa: E402
import frontier as F  # noqa: E402
import frontier_sync as S  # noqa: E402

TOKEN = "tok-SECRET-0123456789"
NOW = "2030-01-15T19:00:00"


def sha(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


class FakeShow:
    """Scripted responses per path; records every request and whether wiki.lock was free."""

    def __init__(self, lockfile: Path):
        self.requests: list[dict] = []
        self.lock_free: list[bool] = []
        self.status = {"taps": [], "lessons": []}
        self.status_code = 200
        self.lesson_replies: list = []          # per POST /lessons: (code, body) or a callable(payload)
        self.known_code = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _record(self):
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n)) if n else None
                with open(lockfile, "a") as fh:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        outer.lock_free.append(True)
                    except OSError:
                        outer.lock_free.append(False)
                outer.requests.append({"method": self.command, "path": self.path, "payload": payload,
                                       "token": self.headers.get("X-Dreamer-Token")})
                return payload

            def do_GET(self):
                self._record()
                self._reply(outer.status_code, outer.status)

            def do_POST(self):
                payload = self._record()
                if self.path == "/known":
                    return self._reply(outer.known_code, {"ok": True, "count": len(payload["titles"])})
                reply = outer.lesson_replies.pop(0) if outer.lesson_replies else None
                if callable(reply):
                    reply = reply(payload)
                code, body = reply or (200, {"ok": True, "id": payload["id"], "status": "created",
                                             "sha256": sha(payload["body"])})
                self._reply(code, body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self, path="/lessons"):
        return [r for r in self.requests if r["method"] == "POST" and r["path"] == path]


class SyncCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dreamer-sync-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for n in ("frontier", "goals", "meta"):
            (self.tmp / n).mkdir()
        self._paths = dict(dc.CFG["paths"])
        self._frontier = dict(dc.CFG["frontier"])
        self.addCleanup(self.restore)
        dc.CFG["paths"].update({n: str(self.tmp / n) for n in ("frontier", "goals", "meta")})
        self.fake = FakeShow(self.tmp / "meta" / "wiki.lock")
        self.addCleanup(self.fake.close)
        dc.CFG["frontier"].update({"vault_lock_wait_seconds": 1, "show_url": self.fake.url,
                                   "show_timeout_seconds": 3, "weekly_ceiling": 3,
                                   "ceiling_window_days": 7, "air_deadline_days": 7,
                                   "known_push_max": 200, "sync_state_file": "frontier-sync.json",
                                   "cause_min_chars": 8})
        self.env_file = self.tmp / "show.env"
        self.env_file.write_text(f"# the show\nDREAMER_LESSON_TOKEN={TOKEN}\n")
        patcher = mock.patch.dict(os.environ, {"DREAMER_SHOW_ENV": str(self.env_file)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DREAMER_LOCK_HELD", None)
        self.write_goal("G0001", "active")

    def restore(self):
        dc.CFG["paths"].clear(); dc.CFG["paths"].update(self._paths)
        dc.CFG["frontier"].clear(); dc.CFG["frontier"].update(self._frontier)

    # -- scaffolding ---------------------------------------------------------
    def write_goal(self, gid, status):
        (self.tmp / "goals" / f"{gid}.md").write_text(
            f"---\ntype: goal\nid: {gid}\nstatus: {status}\ntitle: Goal {gid}\ncreated: 2029-11-21\n"
            f"plan_revision: 1\ntarget_date: 2027-06-30\nfinish_line: Done.\nwhy: Because.\n---\n\n# g\n\n"
            f"## Plan (revision 1)\n\n- [ ] Step\n\n## Assessment (derived — hypothesis, not evidence)\n\nx\n",
            encoding="utf-8")

    def item(self, title="Concept", quadrant="unknown_unknown") -> F.Item:
        it = F.create_item(title, "summary", "premortem", cause="created in test", at=NOW)
        if quadrant != it.quadrant:
            it.quadrant = quadrant
            it.save()
        return it

    def lesson(self, it=None, status="ready", goal="G0001", kind="new", rank=1, released_at="",
               aired_on="", body_text=None, check=None) -> F.Lesson:
        it = it or self.item(f"Concept {len(list(F.items_dir().glob('K*.md'))) + 1}")
        text = body_text or f"{it.title} lesson\nThe body of {it.title} [1].\n"
        # The shape research_lesson writes for a lesson that passed: a claim, a listed source, a passing check.
        page = f"## Lesson\n\n{text}\n## Claims\n\n- C1 (fact): a verified claim [1]\n\n" \
               f"## Sources\n\n[1] A source — https://example.com/a\n\n" \
               f"## Web sources (untrusted)\n\n## Egress record\n"
        les = F.create_lesson(it, goal, kind, "a cause", rank, status=status, body=page,
                              check=check if check is not None else
                              {"verdict": "pass", "model_verdict": "pass", "failed": [], "code_reasons": []})
        les.released_at, les.aired_on = released_at, aired_on
        if aired_on:
            les.aired_quadrant = it.quadrant
        les.save()
        return les

    def get(self, lid) -> F.Lesson:
        return F.Lesson.from_path(F.lesson_path(lid))

    def state(self) -> dict:
        return json.loads((self.tmp / "meta" / "frontier-sync.json").read_text())

    def tap(self, tid, lid, kind="got_it", at="2030-01-16T08:00:00", resolved=True):
        return {"id": tid, "date": "2030-01-16", "cluster_id": 1, "kind": kind, "at": at,
                "lesson_id": lid if resolved else None, "resolved": resolved, "reason": ""}

    def days_ago(self, n):
        import datetime as dt
        return (dt.datetime.now().astimezone() - dt.timedelta(days=n)).isoformat(timespec="seconds")


class ConfigAndSecretsTest(SyncCase):
    def test_no_token_file_means_not_configured_and_no_http(self):
        self.env_file.unlink()
        self.assertEqual(S.pull()["status"], "not configured")
        self.assertEqual(S.push()["status"], "not configured")
        self.assertEqual(self.fake.requests, [])

    def test_missing_key_in_the_file_is_not_configured(self):
        self.env_file.write_text("OTHER=1\n")
        self.assertEqual(S.pull()["status"], "not configured")
        self.assertEqual(self.fake.requests, [])

    def test_token_is_sent_in_the_header_and_never_printed(self):
        self.lesson()
        for cmd in ("pull", "push", "status"):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(S.main([cmd]), 0)
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        self.assertTrue(self.fake.requests)
        self.assertTrue(all(r["token"] == TOKEN for r in self.fake.requests))
        self.assertNotIn(TOKEN, (self.tmp / "meta" / "frontier-sync.json").read_text())

    def test_each_command_prints_one_json_object(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            S.main(["pull"])
        self.assertEqual(json.loads(out.getvalue())["cmd"], "pull")

    def test_status_shows_state_and_counts(self):
        self.lesson(); self.lesson(status="held")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            S.main(["status"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["lessons"], {"ready": 1, "held": 1})
        self.assertEqual(data["state"]["cursor"], 0)
        self.assertTrue(data["configured"])


class OptInTest(SyncCase):
    """The show is opt-in: an empty frontier.show_url disables every entry point."""

    def setUp(self):
        super().setUp()
        dc.CFG["frontier"]["show_url"] = ""

    def test_empty_url_disables_every_entry_point_without_token_or_http(self):
        with mock.patch.object(S, "load_token", side_effect=AssertionError("token read")), \
             mock.patch.object(S.urllib.request.OpenerDirector, "open",
                               side_effect=AssertionError("http call")):
            for fn in (S.pull, S.push, S.status):
                self.assertEqual(fn()["status"], "disabled", fn.__name__)
            for cmd in ("pull", "push", "status"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(S.main([cmd]), 0)
                self.assertEqual(json.loads(out.getvalue())["status"], "disabled")
        self.assertEqual(self.fake.requests, [])

    def test_disabled_even_when_a_show_is_injected(self):
        class Boom:
            def call(self, *a, **k):
                raise AssertionError("http call")
        self.assertEqual(S.pull(Boom())["status"], "disabled")
        self.assertEqual(S.push(Boom())["status"], "disabled")

    def test_a_set_url_with_no_token_still_says_not_configured(self):
        dc.CFG["frontier"]["show_url"] = self.fake.url
        self.env_file.unlink()
        self.assertEqual(S.pull()["status"], "not configured")
        self.assertEqual(S.push()["status"], "not configured")


class InsecureUrlTest(SyncCase):
    """check_show_url is the one place the scheme rule lives; no network is involved."""

    def test_non_loopback_http_is_refused_naming_the_key(self):
        for url in ("http://example.org", "http://203.0.113.7:8080", "http://[2001:db8::1]/x",
                    "http://localhost.example.org", "ftp://127.0.0.1", "example.org"):
            with self.assertRaises(S.InsecureShowUrl, msg=url) as cm:
                S.check_show_url(url, False)
            if url.startswith("http://"):
                self.assertIn("frontier.allow_insecure_http", str(cm.exception))

    def test_allow_flag_permits_it(self):
        S.check_show_url("http://example.org", True)

    def test_loopback_http_and_any_https_are_accepted(self):
        for url in ("http://127.0.0.1:9000", "http://127.9.9.9", "http://[::1]:80", "http://localhost:9",
                    "https://example.org", "https://203.0.113.7/x", "HTTPS://example.org"):
            S.check_show_url(url, False)

    def test_default_is_false_and_show_init_refuses_before_any_request(self):
        dc.CFG["frontier"].pop("allow_insecure_http", None)
        dc.CFG["frontier"]["show_url"] = "http://example.org"
        with mock.patch.object(S.urllib.request.OpenerDirector, "open",
                               side_effect=AssertionError("http call")):
            with self.assertRaises(S.InsecureShowUrl):
                S.Show("tok")
            self.assertIn("frontier.allow_insecure_http", S.pull()["status"])
            self.assertIn("frontier.allow_insecure_http", S.push()["status"])
        dc.CFG["frontier"]["allow_insecure_http"] = True
        S.Show("tok")

    def test_example_config_defaults(self):
        import yaml
        cfg = yaml.safe_load((ROOT / "config.example.yaml").read_text())["frontier"]
        self.assertEqual(cfg["show_url"], "")
        self.assertIs(cfg["allow_insecure_http"], False)


class PullTest(SyncCase):
    def aired_lesson(self, **kw):
        it = self.item()
        les = self.lesson(it, status="released", released_at=self.days_ago(1), **kw)
        return it, les

    def test_a_tap_applies_once_across_two_pulls(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [self.tap(5, les.id)],
                            "lessons": [{"id": les.id, "aired": True, "assigned_date": "2030-01-16",
                                         "episode_status": "ready"}]}
        first = S.pull()
        self.assertEqual(first["taps_applied"], [5])
        self.assertEqual(self.state()["cursor"], 5)
        self.assertEqual(F.Item.from_path(F.item_path(it.id)).quadrant, "known_known")
        n_moves = len(F.Item.from_path(F.item_path(it.id)).moves)
        second = S.pull()                       # the fake re-serves the tap: the cursor is not trusted by it
        self.assertEqual(second["taps_applied"], [5])
        back = self.get(les.id)
        self.assertEqual([t["id"] for t in back.taps], ["5"])
        self.assertEqual(len(F.Item.from_path(F.item_path(it.id)).moves), n_moves)
        self.assertIn("after=5", self.fake.requests[-1]["path"])

    def test_crash_between_page_write_and_cursor_replays_as_a_noop(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [self.tap(7, les.id)],
                            "lessons": [{"id": les.id, "aired": True, "assigned_date": "2030-01-16"}]}
        with mock.patch.object(S, "save_state", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                S.pull()
        self.assertEqual([t["id"] for t in self.get(les.id).taps], ["7"])      # page committed
        self.assertFalse((self.tmp / "meta" / "frontier-sync.json").exists())   # cursor did not
        moves = len(F.Item.from_path(F.item_path(it.id)).moves)
        S.pull()
        self.assertEqual([t["id"] for t in self.get(les.id).taps], ["7"])
        self.assertEqual(len(F.Item.from_path(F.item_path(it.id)).moves), moves)
        self.assertEqual(self.state()["cursor"], 7)

    def test_a_pull_after_a_long_gap_applies_every_tap_in_id_order(self):
        its = [self.aired_lesson()[1] for _ in range(3)]
        taps = [self.tap(9, its[2].id), self.tap(2, its[0].id, "dig_deeper"), self.tap(4, its[1].id)]
        self.fake.status = {"taps": taps, "lessons": [{"id": l.id, "aired": True, "assigned_date": "2030-01-09"}
                                                      for l in its]}
        report = S.pull()
        self.assertEqual(report["taps_applied"], [2, 4, 9])
        self.assertEqual(self.state()["cursor"], 9)
        self.assertEqual(self.get(its[0].id).status, "dig_deeper")
        self.assertEqual(self.get(its[2].id).status, "got_it")

    def test_an_undo_before_the_tap_was_pulled_suppresses_it(self):
        it, les = self.aired_lesson()
        F.undo_tap(les.id, "got_it", "operator", cause="reversed the decision", at="2030-01-16T09:00:00")
        self.fake.status = {"taps": [self.tap(3, les.id, at="2030-01-16T08:00:00")], "lessons": []}
        report = S.pull()
        self.assertEqual(report["taps_applied"], [])
        self.assertEqual(report["taps_skipped"][0]["id"], 3)
        self.assertEqual(self.get(les.id).taps, [])
        self.assertNotEqual(F.Item.from_path(F.item_path(it.id)).quadrant, "known_known")
        self.assertEqual(self.state()["cursor"], 3)          # skipped, so never re-pulled

    def test_a_show_tap_time_is_utc_so_an_undo_an_hour_later_still_suppresses_it(self):
        """The show may stamp feedback in UTC with no offset. Read as
        local, a tap in a far-west time zone looks seven hours later than it was, and an undo made an hour
        after it would lose to it."""
        import datetime as dt
        import os
        import time
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Etc/GMT+7"
        time.tzset()

        def restore_tz():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.addCleanup(restore_tz)
        it, les = self.aired_lesson()
        tapped = dt.datetime(2030, 1, 16, 19, 0, 0, tzinfo=dt.timezone.utc)     # 12:00 at UTC-7
        undone = (tapped + dt.timedelta(hours=1)).astimezone().replace(tzinfo=None)
        F.undo_tap(les.id, "got_it", "operator", cause="reversed the decision",
                   at=undone.isoformat(timespec="seconds"))
        self.fake.status = {"taps": [self.tap(3, les.id, at="2030-01-16 19:00:00")], "lessons": []}
        report = S.pull()
        self.assertEqual(report["taps_applied"], [])
        self.assertEqual(self.get(les.id).taps, [])
        # An applied tap keeps its real instant, with the offset written out.
        it2, les2 = self.aired_lesson()
        self.fake.status = {"taps": [self.tap(4, les2.id, at="2030-01-16 19:00:00")], "lessons": []}
        S.pull()
        self.assertEqual(self.get(les2.id).taps[0]["at"], tapped.astimezone().isoformat(timespec="seconds"))

    def test_an_undone_tap_is_never_reapplied(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [self.tap(3, les.id, at="2030-01-16T08:00:00")],
                            "lessons": [{"id": les.id, "aired": True, "assigned_date": "2030-01-16"}]}
        S.pull()
        F.undo_tap(les.id, "got_it", "operator", cause="reversed the decision", at="2030-01-16T09:00:00")
        S.pull()                                              # the show serves the same tap again
        self.assertEqual(self.get(les.id).status, "aired")
        self.assertNotEqual(F.Item.from_path(F.item_path(it.id)).quadrant, "known_known")

    def test_unresolved_taps_and_unknown_lessons_are_skipped_and_reported(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [self.tap(1, None, resolved=False), self.tap(2, "K9999-1"),
                                     self.tap(3, les.id)], "lessons": []}
        report = S.pull()
        self.assertEqual([s["id"] for s in report["taps_skipped"]], [1, 2])
        self.assertEqual(report["taps_applied"], [3])
        self.assertEqual(self.state()["cursor"], 3)

    def test_a_show_supplied_id_that_is_not_a_lesson_id_is_skipped_and_the_cursor_advances(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [self.tap(1, "../items/K7001"), self.tap(2, les.id)],
                            "lessons": [{"id": "../items/K7001", "aired": True}]}
        report = S.pull()
        self.assertEqual([s["id"] for s in report["taps_skipped"]], [1])
        self.assertEqual(report["taps_applied"], [2])
        self.assertEqual(self.state()["cursor"], 2)
        self.assertEqual(report["status"], "ok")
        with self.assertRaises(F.FrontierError):
            F.lesson_path("../items/K7001")

    def test_aired_lessons_are_marked_and_pin_the_quadrant(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [], "lessons": [{"id": les.id, "aired": True, "assigned_date": "2030-01-17"}]}
        self.assertEqual(S.pull()["aired"], [les.id])
        back = self.get(les.id)
        self.assertEqual((back.status, back.aired_on, back.aired_quadrant), ("aired", "2030-01-17", "unknown_unknown"))
        self.assertEqual(S.pull()["aired"], [])               # already aired: left alone

    def test_a_lesson_the_show_skipped_or_found_bad_fails_and_frees_its_slot(self):
        its = [self.lesson(status="released", released_at=self.days_ago(1)) for _ in range(3)]
        ready = self.lesson()
        self.fake.status = {"taps": [], "lessons": [{"id": l.id, "aired": False, "episode_status": st}
                                                    for l, st in zip(its, ("skipped", "bad", "ready"))]}
        report = S.pull()
        self.assertEqual(sorted(report["failed"]), sorted([its[0].id, its[1].id]))
        failed = self.get(its[0].id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("skipped", failed.check["status_cause"])
        self.assertEqual(self.get(its[2].id).status, "released")
        # two slots are free again
        self.assertEqual(S.push()["released"], [ready.id])

    def test_a_released_lesson_not_aired_by_the_deadline_fails(self):
        old = self.lesson(status="released", released_at=self.days_ago(9))
        fresh = self.lesson(status="released", released_at=self.days_ago(2))
        S.pull()
        self.assertEqual(self.get(old.id).status, "failed")
        self.assertEqual(self.get(fresh.id).status, "released")

    def test_an_aired_lesson_with_no_tap_stays_aired_and_its_item_does_not_move(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [], "lessons": [{"id": les.id, "aired": True, "assigned_date": "2030-01-09"}]}
        S.pull()
        S.pull()
        self.assertEqual(self.get(les.id).status, "aired")
        self.assertEqual(F.Item.from_path(F.item_path(it.id)).quadrant, "unknown_unknown")

    def test_got_it_cancels_the_items_unreleased_follow_ups_only(self):
        it, les = self.aired_lesson()
        queued = self.lesson(it, kind="follow_up", status="ready")
        held = self.lesson(it, kind="follow_up", status="held")
        out = self.lesson(it, kind="follow_up", status="released", released_at=self.days_ago(1))
        self.fake.status = {"taps": [self.tap(1, les.id)], "lessons": []}
        S.pull()
        self.assertEqual((self.get(queued.id).status, self.get(held.id).status), ("dropped", "dropped"))
        self.assertEqual(self.get(out.id).status, "released")
        self.assertIn("got_it tap 1", self.get(queued.id).check["status_cause"])

    def test_show_down_or_refusing_is_a_state_not_a_crash(self):
        for code, text in ((401, "show rejected the token"), (503, "show has no token configured"),
                           (500, "show unreachable")):
            self.fake.status_code = code
            self.assertEqual(S.pull()["status"], text)
        self.assertFalse((self.tmp / "meta" / "frontier-sync.json").exists())

    def test_a_busy_wiki_lock_defers_with_exit_2_and_marks_nothing(self):
        it, les = self.aired_lesson()
        self.fake.status = {"taps": [self.tap(4, les.id)], "lessons": []}
        dc.CFG["frontier"]["vault_lock_wait_seconds"] = 0.3
        fh = open(self.tmp / "meta" / "wiki.lock", "a")
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(S.main(["pull"]), 2)
        self.assertEqual(self.get(les.id).taps, [])
        self.assertFalse((self.tmp / "meta" / "frontier-sync.json").exists())
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        self.assertEqual(S.pull()["taps_applied"], [4])


class PushTest(SyncCase):
    def test_five_ready_lessons_with_a_ceiling_of_three_release_three(self):
        ls = [self.lesson(rank=r) for r in (5, 4, 3, 2, 1)]
        report = S.push()
        self.assertEqual(report["released"], [ls[4].id, ls[3].id, ls[2].id])      # by cause rank
        self.assertEqual(sorted(report["waiting"]), sorted([ls[0].id, ls[1].id]))
        self.assertEqual(len(self.fake.posts()), 3)
        done = self.get(ls[4].id)
        self.assertEqual(done.status, "released")
        self.assertEqual(done.body_hash, sha("The body of " + self.get(ls[4].id).body.split("The body of ")[1].split(" [1].")[0] + " [1]."))
        self.assertTrue(done.released_at)
        self.assertEqual(self.get(ls[0].id).status, "ready")
        # the next night a slot is still not free (3 in the window): the two wait
        self.assertEqual(S.push()["released"], [])

    def test_waiting_lesson_releases_when_the_window_frees_a_slot(self):
        old = self.lesson(status="released", released_at=self.days_ago(8), aired_on="2030-01-07")
        self.lesson(status="released", released_at=self.days_ago(2), aired_on="2030-01-13")
        self.lesson(status="released", released_at=self.days_ago(1), aired_on="2030-01-14")
        a, b = self.lesson(rank=1), self.lesson(rank=2)
        self.assertEqual(S.push()["released"], [a.id])           # old one is outside the window
        self.assertEqual(self.get(b.id).status, "ready")

    def test_goals_take_turns(self):
        self.write_goal("G0002", "active")
        dc.CFG["frontier"]["weekly_ceiling"] = 2     # goal G0001 has the two best, G0002 still gets a turn
        a1, a2 = self.lesson(rank=1, goal="G0001"), self.lesson(rank=2, goal="G0001")
        b1 = self.lesson(rank=3, goal="G0002")
        self.assertEqual(S.push()["released"], [a1.id, b1.id])

    def test_follow_ups_are_exempt_from_the_ceiling(self):
        news = [self.lesson(rank=r) for r in (1, 2, 3, 4)]
        fu = self.lesson(kind="follow_up")
        report = S.push()
        self.assertIn(fu.id, report["released"])
        self.assertEqual(len([r for r in report["released"] if r != fu.id]), 3)
        self.assertEqual(self.fake.posts()[0]["payload"]["kind"], "follow_up")

    def test_a_ready_lesson_that_fails_integrity_is_not_sent_and_is_reported(self):
        good = self.lesson(rank=2)
        # the realistic defect: a page that reached `ready` without a passing check record
        bad = self.lesson(rank=1, check={"verdict": "reject", "model_verdict": "reject"})
        report = S.push()
        self.assertEqual(report["released"], [good.id])
        self.assertEqual([p["payload"]["id"] for p in self.fake.posts()], [good.id])
        [rej] = report["rejected"]
        self.assertEqual((rej["id"], rej["http"]), (bad.id, 0))
        self.assertIn("check did not pass", rej["reason"])
        self.assertEqual(self.get(bad.id).status, "ready")          # left unchanged
        self.assertEqual(self.get(bad.id).released_at, "")

    def test_a_released_lesson_that_fails_integrity_is_not_resent(self):
        les = self.lesson(status="released", released_at="2030-01-13T19:00:00+05:00",
                          check={"verdict": "reject"})
        self.fake.status = {"taps": [], "lessons": []}
        report = S.push()
        self.assertEqual(self.fake.posts(), [])
        self.assertEqual([r["id"] for r in report["rejected"]], [les.id])
        self.assertEqual(self.get(les.id).status, "released")

    def test_zero_ready_lessons_push_nothing(self):
        self.write_goal("G0002", "paused")
        self.lesson(status="held", goal="G0002"); self.lesson(status="rejected")
        report = S.push()
        self.assertEqual(report["released"], [])
        self.assertEqual(self.fake.posts(), [])

    def test_payload_is_the_frozen_wire_shape(self):
        les = self.lesson()
        S.push()
        pl = self.fake.posts()[0]["payload"]
        self.assertEqual(set(pl), {"id", "kind", "title", "body", "sources", "released_at"})
        self.assertEqual(pl["id"], les.id)
        self.assertEqual(pl["sources"], [{"n": 1, "title": "A source", "url": "https://example.com/a"}])
        self.assertRegex(pl["released_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_a_paused_goals_lesson_is_held_then_released_when_the_goal_resumes(self):
        les = self.lesson()
        self.write_goal("G0001", "paused")
        report = S.push()
        self.assertEqual((report["held"], report["released"]), ([les.id], []))
        self.assertEqual(self.get(les.id).status, "held")
        self.assertEqual(self.fake.posts(), [])
        self.write_goal("G0001", "active")
        report = S.push()
        self.assertEqual((report["resumed"], report["released"]), ([les.id], [les.id]))

    def test_an_achieved_goals_lesson_is_dropped_and_the_item_stays(self):
        it = self.item()
        les = self.lesson(it)
        held = self.lesson(status="held")
        self.write_goal("G0001", "achieved")
        S.push()
        self.assertEqual((self.get(les.id).status, self.get(held.id).status), ("dropped", "dropped"))
        self.assertTrue(F.item_path(it.id).exists())
        self.assertEqual(self.fake.posts(), [])

    def test_a_released_lesson_airs_whatever_its_goal_does(self):
        les = self.lesson(status="released", released_at=self.days_ago(1))
        self.write_goal("G0001", "abandoned")
        S.push()
        self.assertEqual(self.get(les.id).status, "released")

    def test_with_the_show_down_nothing_is_released_and_the_next_run_delivers(self):
        les = self.lesson()
        good = dc.CFG["frontier"]["show_url"]
        dc.CFG["frontier"]["show_url"] = "http://127.0.0.1:9"       # discard port: refused
        report = S.push()
        self.assertEqual(report["status"], "show unreachable")
        self.assertEqual(self.get(les.id).status, "ready")
        dc.CFG["frontier"]["show_url"] = good
        self.assertEqual(S.push()["released"], [les.id])

    def test_a_show_that_dies_mid_push_stops_and_marks_only_what_was_delivered(self):
        a, b, c = self.lesson(rank=1), self.lesson(rank=2), self.lesson(rank=3)
        self.fake.lesson_replies = [None, (502, {}), None]
        report = S.push()
        self.assertEqual(report["status"], "show unreachable")
        self.assertEqual(report["released"], [a.id])
        self.assertEqual((self.get(b.id).status, self.get(c.id).status), ("ready", "ready"))
        self.assertEqual(len(self.fake.posts()), 2)             # stopped; c never sent
        self.assertEqual(report["known"] in ("skipped", "unchanged"), True)

    def test_a_409_with_an_equal_hash_counts_as_delivered(self):
        les = self.lesson()
        title, body = S.FL.lesson_parts(les)
        self.fake.lesson_replies = [(409, {"detail": {"error": "changed", "stored_sha256": sha(body)}})]
        report = S.push()
        self.assertEqual(report["released"], [les.id])
        self.assertEqual(self.get(les.id).status, "released")
        self.assertEqual(self.get(les.id).body_hash, sha(body))

    def test_a_409_with_another_hash_is_reported_once_and_never_retried(self):
        les = self.lesson()
        self.fake.lesson_replies = [(409, {"detail": {"error": "changed", "stored_sha256": "f" * 64}})]
        first = S.push()
        self.assertEqual(first["conflicts_new"], [les.id])
        self.assertEqual(self.get(les.id).status, "ready")
        self.assertEqual(self.state()["conflicts"], {les.id: "f" * 64})
        n = len(self.fake.posts())
        second = S.push()
        self.assertEqual(second["conflicts_new"], [])
        self.assertEqual(len(self.fake.posts()), n)             # not retried

    def test_a_conflicted_released_lesson_still_holds_its_ceiling_slot(self):
        """A conflict stops retries, not the count: the lesson went out inside the window."""
        held = [self.lesson(status="released", released_at=self.days_ago(1)) for _ in range(3)]
        state = {"version": 1, "cursor": 0, "conflicts": {held[0].id: "f" * 64}, "known_hash": "",
                 "last_pull_at": "", "last_push_at": ""}
        (self.tmp / "meta" / "frontier-sync.json").write_text(json.dumps(state))
        self.fake.status = {"taps": [], "lessons": [{"id": l.id} for l in held]}
        waiting = self.lesson()
        report = S.push()
        self.assertEqual(report["released"], [])
        self.assertEqual(report["waiting"], [waiting.id])

    def test_413_and_other_4xx_fail_the_lesson_with_a_cause_and_free_its_slot(self):
        """A lesson the show refuses for good must not be picked again every push."""
        a, b = self.lesson(rank=1), self.lesson(rank=2)
        self.fake.lesson_replies = [(413, {}), (422, {})]
        report = S.push()
        self.assertEqual([r["http"] for r in report["rejected"]], [413, 422])
        for les, code in ((a, 413), (b, 422)):
            got = self.get(les.id)
            self.assertEqual(got.status, "failed")
            self.assertIn(f"http {code}", got.check["status_cause"])
        self.assertEqual(report["status"], "ok")
        n = len(self.fake.posts())
        self.assertEqual(S.push()["rejected"], [])
        self.assertEqual(len(self.fake.posts()), n)             # never re-sent

    def test_retryable_statuses_leave_the_lesson_ready(self):
        les = self.lesson()
        for code in (401, 408, 429):
            self.fake.lesson_replies = [(code, {})]
            S.push()
            self.assertEqual(self.get(les.id).status, "ready", code)

    def test_a_ready_lesson_failing_integrity_does_not_starve_the_ceiling(self):
        bad = self.lesson(rank=1, check={"verdict": "reject", "model_verdict": "reject"})
        good = [self.lesson(rank=r) for r in (2, 3, 4)]
        report = S.push()
        self.assertEqual(report["released"], [l.id for l in good])     # ceiling 3: three go out
        self.assertEqual([r["id"] for r in report["rejected"]], [bad.id])

    def test_a_lesson_the_show_already_holds_is_released_first_and_counts_toward_the_ceiling(self):
        """The POST can succeed while the apply step times out on wiki.lock: the lesson stays
        ready locally. The next push must record it, not release a further lesson on top."""
        held = self.lesson(rank=9)
        others = [self.lesson(rank=r) for r in (1, 2, 3)]
        self.fake.status = {"taps": [], "lessons": [{"id": held.id}]}
        report = S.push()
        self.assertEqual(report["released"][0], held.id)
        self.assertEqual(report["released"][1:], [others[0].id, others[1].id])    # 3 in total, not 4
        self.assertEqual(self.get(held.id).status, "released")
        self.assertEqual(self.get(others[2].id).status, "ready")
        self.assertEqual(self.fake.posts()[0]["payload"]["id"], held.id)

    def test_a_conflicted_new_lesson_holds_a_ceiling_slot(self):
        """The show has some version of it: the conflict stops its retries, not its count."""
        released = [self.lesson(status="released", released_at=self.days_ago(1)) for _ in range(2)]
        conflicted = self.lesson(rank=1)
        state = {"version": 1, "cursor": 0, "conflicts": {conflicted.id: "f" * 64}, "known_hash": "",
                 "last_pull_at": "", "last_push_at": ""}
        (self.tmp / "meta" / "frontier-sync.json").write_text(json.dumps(state))
        self.fake.status = {"taps": [], "lessons": [{"id": l.id} for l in released]}
        waiting = self.lesson(rank=2)
        report = S.push()
        self.assertEqual(report["released"], [])
        self.assertEqual(report["waiting"], [waiting.id])

    def test_a_released_lesson_the_show_does_not_know_is_resent_frozen(self):
        les = self.lesson(status="released", released_at="2030-01-13T19:00:00+05:00")
        gone = S.FL.lesson_parts(les)[1]
        self.fake.status = {"taps": [], "lessons": []}
        report = S.push()
        self.assertEqual(report["redelivered"], [les.id])
        self.assertEqual(self.fake.posts()[0]["payload"]["released_at"], "2030-01-13T19:00:00+05:00")
        self.assertEqual(self.fake.posts()[0]["payload"]["body"], gone)
        # known to the show: not re-sent
        self.fake.status = {"taps": [], "lessons": [{"id": les.id, "aired": False}]}
        n = len(self.fake.posts())
        S.push()
        self.assertEqual(len(self.fake.posts()), n)

    def test_an_aired_lesson_is_never_resent(self):
        self.lesson(status="aired", released_at=self.days_ago(3), aired_on="2030-01-13")
        self.fake.status = {"taps": [], "lessons": []}
        S.push()
        self.assertEqual(self.fake.posts(), [])

    def test_the_known_list_holds_known_knowns_only_newest_evidence_first(self):
        old = self.item("Old known", "known_known")
        old.add_evidence("explained", "t1", "2029-11-21", "q1"); old.save()
        new = self.item("New known", "known_known")
        new.add_evidence("explained", "t2", "2030-01-09", "q2"); new.save()
        self.item("A gap", "unknown_unknown")
        self.item("Asked", "known_unknown")
        self.item("Hidden", "unknown_known")
        self.assertEqual(S.push()["known"], "sent")
        self.assertEqual(self.fake.posts("/known")[0]["payload"], {"titles": ["New known", "Old known"]})

    def test_the_known_list_is_capped(self):
        dc.CFG["frontier"]["known_push_max"] = 2
        for n in range(4):
            self.item(f"Known {n}", "known_known")
        S.push()
        self.assertEqual(len(self.fake.posts("/known")[0]["payload"]["titles"]), 2)

    def test_an_unchanged_known_list_is_not_posted_again(self):
        self.item("Known", "known_known")
        S.push()
        S.push()
        self.assertEqual(len(self.fake.posts("/known")), 1)
        self.item("Another", "known_known")
        S.push()
        self.assertEqual(len(self.fake.posts("/known")), 2)

    def test_an_empty_known_list_is_not_sent_until_there_was_something_to_replace(self):
        S.push()
        self.assertEqual(self.fake.posts("/known"), [])

    def test_a_failed_known_post_is_retried_next_run(self):
        self.item("Known", "known_known")
        self.fake.known_code = 500
        self.assertEqual(S.push()["known"], "failed")
        self.fake.known_code = 200
        self.assertEqual(S.push()["known"], "sent")

    def test_no_http_call_is_made_while_wiki_lock_is_held(self):
        self.lesson(); self.lesson(status="held")
        self.item("Known", "known_known")
        S.pull(); S.push()
        self.assertTrue(self.fake.requests)
        self.assertTrue(all(self.fake.lock_free), self.fake.lock_free)


if __name__ == "__main__":
    unittest.main()
