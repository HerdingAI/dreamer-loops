#!/usr/bin/env python3
"""Frontier sync with the show.

The "show" is whatever app consumes lessons: the service at frontier.show_url, off while that is
empty (contract: docs/frontier-schema.md). A lesson "airs" when the show presents it.

Dreamer starts every exchange with the show over HTTP(S) (stdlib urllib, token in the
`X-Dreamer-Token` header):

  pull    GET /lessons/status?after=<cursor>. Marks lessons aired, fails skipped / bad /
          overdue ones, applies the owner's taps in id order, advances the cursor.
  push    Releases ready lessons within the ceiling (POST /lessons), re-sends released
          lessons the show does not know, and replaces the show's known list (POST /known).
  status  Prints the sync state (no secrets) and lesson counts by status.
  withdraw <lesson> --cause "..."
          Operator command: stops a lesson before it airs. A ready/held lesson is dropped; a
          released, unaired one is withdrawn at the show (POST /lessons/<id>/withdraw) and
          failed here. Takes frontier.lock non-blocking; exits 1 when it changed nothing.

pull, push and status print one JSON status object and exit 0: a show outage is a reported
state, not a crash. Exit 2 means wiki.lock stayed busy (deferred; nothing marked), as frontier_map.py.

Locking: the caller holds frontier.lock. NO HTTP call is made while wiki.lock is
held. Every step is plan under wiki.lock -> HTTP outside it -> apply under wiki.lock, and the
apply re-reads each page it changes.

Sync state, paths.meta/<frontier.sync_state_file>:
  {"version": 1, "cursor": <last applied tap id>, "conflicts": {lesson id: stored sha256},
   "known_hash": "", "last_pull_at": "", "last_push_at": ""}
The cursor moves only after the page writes commit. A crash in between replays the taps,
and add_tap dedupes on tap id, so the replay is a no-op.

The token is read from frontier.show_env_path (env DREAMER_SHOW_ENV overrides); it is never
logged or printed, and neither is any response body: errors are reported by class name or
HTTP status only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, atomic_write_json, p, read_json_strict, state_lock  # noqa: E402
import frontier as F  # noqa: E402
import frontier_lessons as FL  # noqa: E402
import goals as G  # noqa: E402

TOKEN_KEY = "DREAMER_LESSON_TOKEN"
STATE_VERSION = 1
UNDELIVERED = F.UNDELIVERED


def _cfg(key: str):
    return CFG["frontier"][key]


def enabled() -> bool:
    """The show is opt-in: sync does nothing while frontier.show_url is empty."""
    return bool(str(CFG["frontier"].get("show_url") or "").strip())


class InsecureShowUrl(ValueError):
    """frontier.show_url is not https and not loopback http, and the owner did not allow it."""


def check_show_url(url: str, allow_insecure: bool) -> None:
    """Refuse a show URL that would send the token in clear text across a network: https is
    always accepted, http only to a loopback host (127.0.0.0/8, ::1, localhost) unless
    allow_insecure (frontier.allow_insecure_http). Pure: no network, no DNS."""
    parts = urllib.parse.urlsplit(str(url).strip())
    scheme = parts.scheme.lower()
    if scheme == "https":
        return
    if scheme == "http":
        host = (parts.hostname or "").lower()
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if loopback or allow_insecure:
            return
        raise InsecureShowUrl(
            f"frontier.show_url uses http to a non-loopback host ({host or 'none'}); the token would travel "
            "unencrypted. Use https, or set frontier.allow_insecure_http: true to accept the risk")
    raise InsecureShowUrl(f"frontier.show_url must start with https:// (or http:// to a loopback host), got scheme {scheme or 'none'!r}")


def _insecure_ok() -> bool:
    return bool(CFG["frontier"].get("allow_insecure_http", False))


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Token and state
# --------------------------------------------------------------------------

def env_path() -> Path:
    return Path(os.environ.get("DREAMER_SHOW_ENV") or os.path.expanduser(str(_cfg("show_env_path"))))


def load_token() -> str | None:
    """The token from the env file, or None when the file or the key is missing/blank."""
    path = env_path()
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == TOKEN_KEY:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value or None
    return None


def state_path() -> Path:
    return p("meta") / str(_cfg("sync_state_file"))


def load_state() -> dict:
    raw = read_json_strict(state_path(), default={}) or {}
    return {"version": STATE_VERSION, "cursor": int(raw.get("cursor") or 0),
            "conflicts": dict(raw.get("conflicts") or {}), "known_hash": str(raw.get("known_hash") or ""),
            "last_pull_at": str(raw.get("last_pull_at") or ""),
            "last_push_at": str(raw.get("last_push_at") or "")}


def save_state(state: dict) -> None:
    atomic_write_json(state_path(), state)


def _wiki_lock():
    return state_lock(p("meta") / "wiki.lock", timeout_s=float(_cfg("vault_lock_wait_seconds")))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Show:
    """The show's wire contract. `call` returns (http status, parsed JSON or None); status 0
    is a network failure. It never raises and never keeps or prints a response body."""

    def __init__(self, token: str):
        check_show_url(str(_cfg("show_url")), _insecure_ok())    # before any request
        self._token = token
        self._base = str(_cfg("show_url")).rstrip("/")
        self._timeout = float(_cfg("show_timeout_seconds"))
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | None]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._base + path, data=data, method=method)
        req.add_header("X-Dreamer-Token", self._token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                code, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001 - the status alone is what we report
                raw = b""
        except Exception:  # noqa: BLE001 - URLError, timeout, reset: all "unreachable"
            return 0, None
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            body = None
        return code, body if isinstance(body, dict) else None


def _unusable(code: int) -> str | None:
    """A status naming why the show did not answer a call usefully, else None (2xx)."""
    if 200 <= code < 300:
        return None
    if code == 0 or code >= 500 and code != 503:
        return "show unreachable"
    if code == 401:
        return "show rejected the token"
    if code == 503:
        return "show has no token configured"
    return f"show answered http {code}"


# --------------------------------------------------------------------------
# Page helpers
# --------------------------------------------------------------------------

def _load_lesson(lesson_id: str) -> "F.Lesson | None":
    """None for an unknown lesson, and for an id that cannot be one (the show supplies ids)."""
    try:
        path = F.lesson_path(lesson_id)
    except F.FrontierError:
        return None
    return F.Lesson.from_path(path) if path.exists() else None


def _all_lessons() -> list["F.Lesson"]:
    d = F.lessons_dir()
    return [F.Lesson.from_path(path) for path in sorted(d.glob("K*.md"))] if d.exists() else []


def _set_status(lesson: "F.Lesson", status: str, cause: str, at: str) -> None:
    """Move a lesson and record why on its `check` record (the Lesson schema is unchanged)."""
    F.set_lesson_status(lesson, status, cause, at)


def _numeric_id(lesson_id: str) -> tuple[int, int]:
    head, _, tail = lesson_id.partition("-")
    return (int(head[1:]) if head[1:].isdigit() else 0, int(tail) if tail.isdigit() else 0)


def _show_time(stamp) -> str | None:
    """A show timestamp as an aware local ISO time. The show stamps feedback with a naive
    UTC timestamp (no offset) ("2025-03-04 05:06:07"); Dreamer reads a naive time
    as local, so unconverted a tap would sort hours after the undo that cancelled it."""
    try:
        then = _dt.datetime.fromisoformat(str(stamp or ""))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=_dt.timezone.utc)
    return then.astimezone().isoformat(timespec="seconds")


def _age_days(stamp: str, now: _dt.datetime) -> float | None:
    try:
        then = _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.astimezone()
    return (now - then).total_seconds() / 86400


# --------------------------------------------------------------------------
# Pull
# --------------------------------------------------------------------------

def _apply_lessons(delivered: list[dict], now: _dt.datetime, report: dict) -> None:
    stamp = now.isoformat(timespec="seconds")
    seen = set()
    for entry in delivered:
        lid = str(entry.get("id") or "")
        lesson = _load_lesson(lid) if lid else None
        if lesson is None:
            continue
        seen.add(lid)
        if entry.get("aired") and not lesson.aired_on:
            F.mark_aired(lid, str(entry.get("assigned_date") or now.date().isoformat()))
            report["aired"].append(lid)
        elif entry.get("episode_status") in ("skipped", "bad") and lesson.status == "released":
            _set_status(lesson, "failed", f"the show reported the lesson {entry['episode_status']}", stamp)
            report["failed"].append(lid)
    # Overdue: released, not aired, past the deadline (the show may never have confirmed it).
    deadline = float(_cfg("air_deadline_days"))
    for lesson in _all_lessons():
        if lesson.status != "released" or lesson.aired_on:
            continue
        age = _age_days(lesson.released_at, now)
        if age is not None and age > deadline:
            _set_status(lesson, "failed", f"not aired within {_cfg('air_deadline_days')} days of release", stamp)
            report["failed"].append(lesson.id)


def _reused_id(lesson: "F.Lesson", tid: int, kind: str, at: str | None) -> bool:
    """True when the lesson already records tap `tid` with a different kind or time. `at` is
    compared as add_tap would store it; a time the show sent unreadable (None) is not compared,
    since add_tap would stamp it with the clock and a replay could never match."""
    for t in lesson.taps:
        if t["id"] == str(tid):
            return t.get("kind") != kind or (at is not None and F._now(at) != t.get("at"))
    return False


def _apply_taps(taps: list[dict], cursor: int, stamp: str, report: dict) -> int:
    """Apply taps in id order; returns the cursor after the last tap applied or skipped. A tap
    whose apply raises stops the pass, so the cursor never passes an unapplied tap."""
    for tap in sorted(taps, key=lambda t: int(t["id"])):
        tid = int(tap["id"])
        lid = str(tap.get("lesson_id") or "")
        kind = str(tap.get("kind") or "")
        lesson = _load_lesson(lid) if lid and tap.get("resolved") is not False else None
        if lesson is None or kind not in F.TAP_KINDS:
            report["taps_skipped"].append({"id": tid, "reason": "unresolved" if not lid else "unknown lesson"})
        elif _reused_id(lesson, tid, kind, _show_time(tap.get("at"))):
            # add_tap dedupes on the id alone, so it would drop this tap and the check below
            # would call it applied. Same id, other kind or time: the show handed the id out again.
            report["taps_reused"].append({"id": tid, "lesson": lid, "reason": "the show reused tap id"})
        else:
            F.add_tap(lid, kind, str(tid), _show_time(tap.get("at")))
            lesson = _load_lesson(lid)
            if any(t["id"] == str(tid) for t in lesson.taps):
                report["taps_applied"].append(tid)
                if kind == "got_it":
                    report["follow_ups_cancelled"] += F.cancel_follow_ups(lesson, str(tid), stamp)
            else:
                report["taps_skipped"].append({"id": tid, "reason": "undone before it was pulled"})
        cursor = max(cursor, tid)
    return cursor


def pull(show: Show | None = None) -> dict:
    report = {"cmd": "pull", "status": "ok", "aired": [], "failed": [], "taps_applied": [],
              "taps_skipped": [], "taps_reused": [], "follow_ups_cancelled": [], "cursor": 0}
    if not enabled():
        return {**report, "status": "disabled"}
    if show is None:
        try:
            check_show_url(str(_cfg("show_url")), _insecure_ok())
        except InsecureShowUrl as exc:
            return {**report, "status": str(exc)}
        token = load_token()
        if token is None:
            return {**report, "status": "not configured"}
        show = Show(token)
    state = load_state()
    report["cursor"] = state["cursor"]
    code, data = show.call("GET", f"/lessons/status?after={state['cursor']}")
    why = _unusable(code)
    if why or data is None:
        return {**report, "status": why or "show sent an unreadable reply"}
    now = _dt.datetime.now().astimezone()
    with _wiki_lock():
        _apply_lessons(list(data.get("lessons") or []), now, report)
        taps = list(data.get("taps") or [])
        applied_to = state["cursor"]
        try:
            applied_to = _apply_taps(taps, state["cursor"], now.isoformat(timespec="seconds"), report)
        except Exception as exc:  # noqa: BLE001 - keep the cursor behind the tap that failed
            report["status"] = f"failed applying a tap ({type(exc).__name__})"
            applied_to = max([state["cursor"]] + [t for t in report["taps_applied"]]
                             + [s["id"] for s in report["taps_skipped"]])
        state["cursor"] = applied_to
        state["last_pull_at"] = now.isoformat(timespec="seconds")
        save_state(state)
    report["cursor"] = state["cursor"]
    return report


# --------------------------------------------------------------------------
# Push
# --------------------------------------------------------------------------

def _body_hash(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def _gate_by_goal(report: dict, stamp: str) -> None:
    """Goal status gating for lessons not yet released."""
    status_of = {g.id: g.status for g in G.load_goals()}
    for lesson in _all_lessons():
        if lesson.status not in UNDELIVERED or lesson.goal not in status_of:
            continue
        goal = status_of[lesson.goal]
        if goal in ("achieved", "abandoned"):
            _set_status(lesson, "dropped", f"goal {lesson.goal} is {goal}", stamp)
            report["dropped"].append(lesson.id)
        elif goal == "paused" and lesson.status == "ready":
            _set_status(lesson, "held", f"goal {lesson.goal} is paused", stamp)
            report["held"].append(lesson.id)
        elif goal == "active" and lesson.status == "held":
            _set_status(lesson, "ready", f"goal {lesson.goal} is active again", stamp)
            report["resumed"].append(lesson.id)


def _select_new(ready: list["F.Lesson"], room: int) -> list["F.Lesson"]:
    """Most consequential first, goals taking turns: group by goal (ids sorted), order each
    group by (cause rank, numeric lesson id), deal round-robin, take what the ceiling admits."""
    groups: dict[str, list] = {}
    for lesson in ready:
        groups.setdefault(lesson.goal, []).append(lesson)
    queues = [sorted(groups[g], key=lambda l: (l.cause_rank, _numeric_id(l.id))) for g in sorted(groups)]
    order = []
    while any(queues):
        for q in queues:
            if q:
                order.append(q.pop(0))
    return order[:max(room, 0)]


def _known_titles() -> list[str]:
    """Titles of known-known items, newest evidence first, capped."""
    rows = []
    d = F.items_dir()
    for path in sorted(d.glob("K*.md")) if d.exists() else []:
        item = F.Item.from_path(path)
        title = item.title.strip()
        if item.quadrant == "known_known" and title:
            rows.append((max([e["date"] for e in item.evidence] or [""]), item.id, title))
    rows.sort(reverse=True)
    out, seen = [], set()
    for _, _, title in rows:
        if title.lower() not in seen:
            seen.add(title.lower())
            out.append(title)
    return out[:int(_cfg("known_push_max"))]


def _plan(shown: set[str], conflicts: dict, now: _dt.datetime, report: dict) -> tuple[list[dict], list[str]]:
    """Under wiki.lock: gate by goal, then decide what to send. Returns the sends (frozen
    payloads) and the known titles."""
    stamp = now.isoformat(timespec="seconds")
    _gate_by_goal(report, stamp)
    every = _all_lessons()
    window = float(_cfg("ceiling_window_days"))
    # The ceiling counts every new lesson released in the window, a conflicted one too: a
    # conflict stops its retries, not the fact that it went out.
    used = sum(1 for l in every if l.kind == "new" and l.status != "failed" and l.released_at
               and (_age_days(l.released_at, now) or 0) <= window)
    # A new lesson not yet released here but in conflict with the show's copy: the show holds a
    # version of it, so it fills a slot even though it is never retried.
    used += sum(1 for l in every if l.kind == "new" and l.id in conflicts and not l.released_at
                and l.status in UNDELIVERED)
    lessons = [l for l in every if l.id not in conflicts]    # a conflict is never retried
    ready = [l for l in lessons if l.status == "ready"]
    # A lesson that fails integrity never leaves, so it must not take a ceiling slot from one that
    # can: it is reported here and left out of the selection (and out of the sends below).
    failing = {}
    for lesson in ready:
        problems = FL.lesson_integrity(lesson)
        if problems:
            failing[lesson.id] = problems
            report["rejected"].append({"id": lesson.id, "http": 0, "reason": "; ".join(problems)})
    ready = [l for l in ready if l.id not in failing]
    # The show may already hold a ready lesson (its POST succeeded but the apply step timed out
    # on wiki.lock). Record it first, as released, and let it count: selecting a further lesson
    # on top would put more than the ceiling on the show.
    held = sorted((l for l in ready if l.id in shown), key=lambda l: _numeric_id(l.id))
    held_ids = {l.id for l in held}
    used += sum(1 for l in held if l.kind == "new")
    follow = sorted((l for l in ready if l.kind == "follow_up" and l.id not in held_ids),
                    key=lambda l: _numeric_id(l.id))
    new = _select_new([l for l in ready if l.kind == "new" and l.id not in held_ids],
                      int(_cfg("weekly_ceiling")) - used)
    taken = {l.id for l in new}
    report["waiting"] = [l.id for l in ready if l.kind == "new" and l.id not in taken and l.id not in held_ids]
    sends = []
    for lesson in held + follow + new:
        sends.append({"lesson": lesson.id, "released_at": stamp, "mode": "release"})
    # Released here but unknown to the show: re-send the frozen body. An aired lesson never is.
    for lesson in lessons:
        if lesson.status == "released" and not lesson.aired_on and lesson.id not in shown:
            sends.append({"lesson": lesson.id, "released_at": lesson.released_at or stamp, "mode": "resend"})
    by_id = {l.id: l for l in lessons}
    out = []
    for send in sends:
        lesson = by_id[send["lesson"]]
        # Whatever the healthcheck says, a lesson that fails integrity (no passing check, no
        # verified claim or listed source, nothing to teach) never leaves: reported, not changed.
        if lesson.id in failing:
            continue
        problems = FL.lesson_integrity(lesson)
        if problems:  # a released lesson being re-sent
            report["rejected"].append({"id": lesson.id, "http": 0, "reason": "; ".join(problems)})
            continue
        title, body = FL.lesson_parts(lesson)
        out.append({**send, "title": title, "body": body, "sources": FL.lesson_sources(lesson),
                    "hash": _body_hash(body), "kind": lesson.kind})
    return out, _known_titles()


def _post_all(show: Show, sends: list[dict], report: dict) -> tuple[list[dict], str | None]:
    """HTTP only (no lock held). Returns [(send, outcome)] and a stop reason, if any.
    outcome: delivered | conflict:<stored sha> | rejected:<http>."""
    results, stop = [], None
    for send in sends:
        payload = {"id": send["lesson"], "kind": send["kind"], "title": send["title"],
                   "body": send["body"], "sources": send["sources"], "released_at": send["released_at"]}
        code, data = show.call("POST", "/lessons", payload)
        if 200 <= code < 300:
            results.append((send, "delivered"))
        elif code == 409:
            detail = (data or {}).get("detail")
            stored = str((detail or {}).get("stored_sha256") or "") if isinstance(detail, dict) else ""
            results.append((send, "delivered" if stored and stored == send["hash"] else f"conflict:{stored}"))
        elif 400 <= code < 500 and code not in (401, 408, 429):
            results.append((send, f"rejected:{code}"))
        else:
            stop = _unusable(code) or f"show answered http {code}"
            break
    return results, stop


def _apply_results(results: list[tuple[dict, str]], state: dict, report: dict, stamp: str = "") -> None:
    for send, outcome in results:
        lid = send["lesson"]
        if outcome == "delivered":
            lesson = _load_lesson(lid)
            if lesson is None or lesson.aired_on:
                continue
            if send["mode"] == "release" or lesson.status != "released":
                lesson.status = "released"
                lesson.released_at = send["released_at"]
                lesson.body_hash = send["hash"]
                lesson.save()
            (report["released"] if send["mode"] == "release" else report["redelivered"]).append(lid)
        elif outcome.startswith("conflict:"):
            stored = outcome.split(":", 1)[1]
            if state["conflicts"].get(lid) != stored:      # once per distinct conflict
                report["conflicts_new"].append(lid)
            state["conflicts"][lid] = stored
        else:
            code = int(outcome.split(":", 1)[1])
            report["rejected"].append({"id": lid, "http": code})
            lesson = _load_lesson(lid)
            # The show refused a new release for good: end it, or _select_new picks it again
            # every push and it starves the ceiling. `failed` frees the slot (the ceiling
            # ignores failed lessons) and keeps the page; a released lesson stays as it is.
            if lesson is not None and send["mode"] == "release" and lesson.status == "ready":
                _set_status(lesson, "failed", f"the show refused it (http {code})", stamp)


def push(show: Show | None = None) -> dict:
    report = {"cmd": "push", "status": "ok", "released": [], "redelivered": [], "waiting": [],
              "held": [], "resumed": [], "dropped": [], "conflicts_new": [], "rejected": [],
              "known": "unchanged"}
    if not enabled():
        return {**report, "status": "disabled", "known": "skipped"}
    if show is None:
        try:
            check_show_url(str(_cfg("show_url")), _insecure_ok())
        except InsecureShowUrl as exc:
            return {**report, "status": str(exc), "known": "skipped"}
        token = load_token()
        if token is None:
            return {**report, "status": "not configured", "known": "skipped"}
        show = Show(token)
    state = load_state()
    code, data = show.call("GET", f"/lessons/status?after={state['cursor']}")
    why = _unusable(code)
    if why or data is None:
        return {**report, "status": why or "show sent an unreadable reply", "known": "skipped"}
    shown = {str(e.get("id")) for e in data.get("lessons") or []}
    now = _dt.datetime.now().astimezone()

    with _wiki_lock():
        sends, titles = _plan(shown, state["conflicts"], now, report)
    results, stop = _post_all(show, sends, report)         # no wiki.lock here
    known_hash = _body_hash(json.dumps(titles, ensure_ascii=False))
    known_result = None
    if stop is None and (known_hash != state["known_hash"]) and (titles or state["known_hash"]):
        code, _ = show.call("POST", "/known", {"titles": titles})
        if 200 <= code < 300:
            state["known_hash"], report["known"] = known_hash, "sent"
        else:
            known_result = _unusable(code) or f"show answered http {code}"
            report["known"] = "failed"
    elif stop is not None:
        report["known"] = "skipped"
    with _wiki_lock():
        _apply_results(results, state, report, now.isoformat(timespec="seconds"))
        state["last_push_at"] = now.isoformat(timespec="seconds")
        save_state(state)
    if stop or known_result:
        report["status"] = stop or known_result
    return report


# --------------------------------------------------------------------------
# Withdraw
# --------------------------------------------------------------------------

def withdraw(lesson_id: str, cause: str, show: Show | None = None) -> dict:
    """Stop a lesson before it airs. The report's `ok` is False when nothing was changed."""
    report = {"cmd": "withdraw", "status": "ok", "ok": True, "lesson": lesson_id}

    def refuse(why: str, **extra) -> dict:
        return {**report, "status": why, "ok": False, **extra}

    cause = cause.strip()
    if not cause:
        return refuse("a cause is required")
    why_cause = f"withdrawn by operator: {cause}"
    stamp = _now()
    with _wiki_lock():
        lesson = _load_lesson(lesson_id)
        if lesson is None:
            return refuse("no such lesson")
        if lesson.status in F.UNDELIVERED:
            was = lesson.status
            _set_status(lesson, "dropped", why_cause, stamp)
            return {**report, "was": was, "now": "dropped"}
        if lesson.status != "released" or lesson.aired_on:
            return refuse(f"not withdrawable: the lesson is {lesson.status}")
    # Only a released lesson needs the show; with frontier.show_url empty nothing is asked.
    if not enabled():
        return refuse("disabled")
    if show is None:
        token = load_token()
        if token is None:
            return refuse("not configured")
        try:
            show = Show(token)
        except InsecureShowUrl as exc:
            return refuse(str(exc))
    code, data = show.call("POST", f"/lessons/{lesson_id}/withdraw", {"reason": cause})   # no wiki.lock here
    note = None
    if code == 409:
        detail = (data or {}).get("detail")
        assigned = str(detail.get("assigned_date") or "") if isinstance(detail, dict) else ""
        return refuse("already in a show", assigned_date=assigned)
    if code == 404:
        # The show never stored it. Left `released`, push would re-send it.
        note = "the show never stored it"
    else:
        why = _unusable(code)
        if why or data is None:
            return refuse(why or "show sent an unreadable reply")
    with _wiki_lock():
        lesson = _load_lesson(lesson_id)
        if lesson is None or lesson.status != "released" or lesson.aired_on:
            return refuse("the lesson changed while the show was asked; check it")
        _set_status(lesson, "failed", why_cause, stamp)
    return {**report, "was": "released", "now": "failed", **({"note": note} if note else {})}


# --------------------------------------------------------------------------
# Status and CLI
# --------------------------------------------------------------------------

def status() -> dict:
    if not enabled():
        return {"cmd": "status", "status": "disabled", "configured": False}
    state = load_state()
    counts: dict[str, int] = {}
    for lesson in _all_lessons():
        counts[lesson.status] = counts.get(lesson.status, 0) + 1
    return {"cmd": "status", "status": "ok", "configured": load_token() is not None,
            "state": {**state, "conflicts": sorted(state["conflicts"])}, "lessons": counts}


def _withdraw_cli(lesson_id: str, cause: str) -> int:
    # An operator command outside bin/frontier.sh, so it takes the run's lock itself and gives
    # up at once when a run holds it (as frontier_gaps.py recheck does). The flock is taken
    # directly, not through state_lock: that sets DREAMER_LOCK_HELD, which would turn the
    # wiki.lock below into a no-op and leave the vault writes unprotected.
    lock = p("meta") / str(_cfg("lock_file"))
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps({"cmd": "withdraw", "ok": False,
                              "status": f"{lock.name} is held by a frontier run; try again later"}))
            return 1
        try:
            result = withdraw(lesson_id, cause)
        except TimeoutError as exc:
            print(f"frontier_sync: {exc} (wiki.lock)", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - report the class only; never a body or a token
            result = {"cmd": "withdraw", "ok": False, "status": f"failed ({type(exc).__name__})"}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Knowledge frontier sync with the show")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull", help="apply aired/failed lessons and taps; prints a JSON status")
    sub.add_parser("push", help="release lessons within the ceiling and send the known list")
    sub.add_parser("status", help="sync state and lesson counts")
    wd = sub.add_parser("withdraw", help="stop a lesson before it airs, under frontier.lock; "
                                         "prints a JSON status")
    wd.add_argument("lesson", help="lesson id, e.g. K7001-1")
    wd.add_argument("--cause", required=True, help="why; recorded on the lesson and sent to the show")
    args = ap.parse_args(argv)
    if args.cmd == "withdraw":
        return _withdraw_cli(args.lesson, args.cause)
    try:
        result = {"pull": pull, "push": push, "status": status}[args.cmd]()
    except TimeoutError as exc:
        print(f"frontier_sync: {exc} (wiki.lock)", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report the class only; never a body or a token
        result = {"cmd": args.cmd, "status": f"failed ({type(exc).__name__})"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
