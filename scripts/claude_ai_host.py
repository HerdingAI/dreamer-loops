#!/usr/bin/env python3
"""Chrome native-messaging host for the Dreamer Sync extension.

Protocol (Chrome's): each message is a 4-byte native-endian length followed by
that many bytes of UTF-8 JSON, on stdin (from Chrome) and stdout (to Chrome).
NOTHING else may be written to stdout. Logs go to logs/claude-ai-host.log.

Commands (each reply echoes the request's optional `id`):
  status        -> {ok, due, cursor, settings}
  conversation  {chat}    spool one chat (a full claude.ai chat object)
  commit        {cursor, count, pending?, detail?}
                write ONE atomic drop to_ingest/claude-ai-sync-<UTC>.json and
                record state `ok`
  report        {status, detail}   record a failed attempt; cursor never moves

State file: vault/.vault-meta/claude-ai-sync.json, contract in
docs/claude-ai-sync-state.md. Settings come from config.yaml `claude_ai_sync:`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dreamer_common as dc  # noqa: E402

STATE_FILE = "claude-ai-sync.json"
FAILURES = ("needs_login", "blocked", "schema_changed", "error")
MAX_MESSAGE = 64 * 1024 * 1024   # Chrome's cap on one message to a host
DETAIL_MAX = 300

# Settings the extension needs; everything else in the block is host/cron side.
EXTENSION_KEYS = ("quiet_hours", "max_per_run", "spacing_seconds", "page_size",
                  "max_pages", "max_retries", "backoff_base_seconds",
                  "backoff_max_seconds")


# --- framing ---------------------------------------------------------------

def read_message(stream) -> dict | None:
    """One framed message, or None at clean EOF. Raises ValueError on junk."""
    head = stream.read(4)
    if not head:
        return None
    if len(head) < 4:
        raise ValueError("truncated length header")
    (n,) = struct.unpack("=I", head)
    if n > MAX_MESSAGE:
        raise ValueError(f"message of {n} bytes exceeds the {MAX_MESSAGE} cap")
    body = stream.read(n)
    if len(body) < n:
        raise ValueError("truncated message body")
    msg = json.loads(body.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError("message is not a JSON object")
    return msg


def write_message(stream, obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


# --- helpers ---------------------------------------------------------------

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$")


def parse_ts(s) -> _dt.datetime | None:
    """ISO timestamp -> aware datetime (microsecond precision), else None."""
    m = _TS.match(s) if isinstance(s, str) else None
    if not m:
        return None
    base, frac, tz = m.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    tz = "+00:00" if tz == "Z" else tz
    try:
        return _dt.datetime.fromisoformat(f"{base}.{frac}{tz}")
    except ValueError:
        return None


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Host:
    def __init__(self, *, cfg: dict | None = None, meta: Path | None = None,
                 inbox: Path | None = None, logfile: Path | None = None,
                 now=utc_now, spool_root: Path | None = None) -> None:
        self.cfg = dict(cfg if cfg is not None else dc.CFG["claude_ai_sync"])
        self.meta = Path(meta) if meta else dc.p("meta")
        self.inbox = Path(inbox) if inbox else dc.ROOT / "to_ingest"
        self.logfile = Path(logfile) if logfile else dc.p("logs") / "claude-ai-host.log"
        self.now = now
        self.spool = Path(tempfile.mkdtemp(prefix="claude-ai-spool-",
                                           dir=str(spool_root) if spool_root else None))

    # -- logging / state ----------------------------------------------------
    def log(self, msg: str) -> None:
        try:
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            with self.logfile.open("a", encoding="utf-8") as fh:
                fh.write(f"[{iso(self.now())}] {msg}\n")
        except OSError:
            pass  # logging must never break the protocol

    @property
    def state_path(self) -> Path:
        return self.meta / STATE_FILE

    def read_state(self) -> dict:
        st = dc.read_json(self.state_path, default=None)
        return st if isinstance(st, dict) else {}

    def ledger_cursor(self) -> str | None:
        """Newest updated_at in the converter's dedupe ledger.

        ingested.json holds claude.ai conversations only (Claude Code and
        desktop sessions have their own ledger, cc-ingested.json), so the
        maximum is the right first-run cursor: everything at or below it came
        from an export or an earlier sync.
        """
        led = dc.read_json(self.meta / "ingested.json", default=None)
        best, best_t = None, None
        if isinstance(led, dict):
            for entry in led.values():
                u = entry.get("updated_at") if isinstance(entry, dict) else None
                t = parse_ts(u)
                if t is not None and (best_t is None or t > best_t):
                    best, best_t = u, t
        return best

    def cursor(self) -> str | None:
        st = self.read_state()
        return st.get("cursor") if parse_ts(st.get("cursor")) else self.ledger_cursor()

    def write_state(self, **fields) -> dict:
        st = self.read_state()
        st.update(fields)
        dc.atomic_write_json(self.state_path, st)
        return st

    # -- commands -----------------------------------------------------------
    def handle(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        fn = {"status": self.cmd_status, "conversation": self.cmd_conversation,
              "commit": self.cmd_commit, "report": self.cmd_report}.get(cmd)
        try:
            reply = fn(msg) if fn else {"ok": False, "error": f"unknown cmd {cmd!r}"}
        except Exception as exc:  # noqa: BLE001 - the protocol must answer
            self.log(f"ERROR {cmd}: {exc!r}")
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if "id" in msg:
            reply["id"] = msg["id"]
        return reply

    def due(self, st: dict) -> bool:
        """last_ok older than due_hours -- or a capped run left a backlog.

        After a failed attempt the hourly tick waits retry_after_failure_hours
        before trying again, so a block or a logged-out session is not hammered
        (the owner's click on the icon forces a run regardless).
        """
        now = self.now()
        last_ok, attempt = parse_ts(st.get("last_ok")), parse_ts(st.get("last_attempt"))
        failed = st.get("status") in FAILURES
        if failed and attempt is not None:
            if (now - attempt).total_seconds() < float(self.cfg["retry_after_failure_hours"]) * 3600:
                return False
        if st.get("pending"):
            return True  # a capped run left chats waiting; drain on the next tick
        if last_ok is None:
            return True
        return (now - last_ok).total_seconds() > float(self.cfg["due_hours"]) * 3600

    def cmd_status(self, msg: dict) -> dict:
        st = self.read_state()
        if "login_expires_at" in msg:
            st = self._record_login_expiry(st, msg["login_expires_at"])
        settings = {k: self.cfg[k] for k in EXTENSION_KEYS}
        return {"ok": True, "due": self.due(st), "cursor": self.cursor(), "settings": settings}

    def _record_login_expiry(self, st: dict, raw) -> dict:
        """Keep the claude.ai login's expiry (UTC) for the freshness warning.

        None is meaningful (Chrome holds no login) and is recorded; anything
        else unparseable is logged and ignored, never allowed to fail the
        status call that every hourly tick depends on. Written only on change,
        so the hourly tick does not rewrite the state file for nothing.
        """
        if raw is None:
            value = None
        else:
            t = parse_ts(raw)
            if t is None:
                self.log(f"status: ignoring unreadable login_expires_at {str(raw)[:40]!r}")
                return st
            value = iso(t)
        if "login_expires_at" in st and st["login_expires_at"] == value:
            return st
        return self.write_state(login_expires_at=value)

    def cmd_conversation(self, msg: dict) -> dict:
        chat = msg.get("chat")
        if not (isinstance(chat, dict) and isinstance(chat.get("uuid"), str) and chat["uuid"]
                and parse_ts(chat.get("updated_at")) and isinstance(chat.get("chat_messages"), list)):
            return {"ok": False, "error": "chat needs uuid, updated_at, chat_messages"}
        uuid = chat["uuid"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uuid):
            return {"ok": False, "error": "unsafe uuid"}
        path = self.spool / f"{uuid}.json"
        prev = dc.read_json(path, default=None)
        if prev and parse_ts(prev.get("updated_at")) >= parse_ts(chat["updated_at"]):
            return {"ok": True, "count": len(list(self.spool.glob("*.json")))}
        chat.setdefault("account", None)  # export shape; the converter ignores it
        dc.atomic_write_json(path, chat)
        return {"ok": True, "count": len(list(self.spool.glob("*.json")))}

    def cmd_commit(self, msg: dict) -> dict:
        chats = [dc.read_json(f) for f in sorted(self.spool.glob("*.json"))]
        chats = [c for c in chats if isinstance(c, dict)]
        count = msg.get("count")
        # Cross-check the extension's tally with what actually arrived: a lost
        # message must fail the run, not silently advance the cursor.
        if isinstance(count, int) and count != len(chats):
            return {"ok": False, "error": f"expected {count} chats, spool holds {len(chats)}"}
        cursor = msg.get("cursor")
        if cursor is not None and parse_ts(cursor) is None:
            return {"ok": False, "error": "cursor is not an ISO timestamp"}
        if chats and cursor is None:
            return {"ok": False, "error": "chats delivered but no cursor"}
        old = self.cursor()
        if old and cursor and parse_ts(old) > parse_ts(cursor):
            cursor = old  # never move backwards
        drop = None
        if chats:
            chats.sort(key=lambda c: (parse_ts(c["updated_at"]), c["uuid"]))
            drop = self._write_drop(chats)
        now = iso(self.now())
        detail = str(msg.get("detail") or f"fetched {len(chats)}")[:DETAIL_MAX]
        self.write_state(last_attempt=now, last_ok=now, status="ok", detail=detail,
                         cursor=cursor if cursor else None,
                         fetched=len(chats), converted=0,
                         pending=bool(msg.get("pending")))
        self.log(f"commit: {len(chats)} chat(s), cursor {cursor}, drop {drop.name if drop else '-'}")
        self._clear_spool()
        return {"ok": True, "fetched": len(chats), "drop": drop.name if drop else None}

    def cmd_report(self, msg: dict) -> dict:
        status = msg.get("status")
        if status not in FAILURES:
            status = "error"
        detail = str(msg.get("detail") or "")[:DETAIL_MAX]
        st = self.read_state()
        # last_ok and the cursor are untouched: a failure never advances either.
        self.write_state(last_attempt=iso(self.now()), status=status, detail=detail,
                         cursor=self.cursor(), last_ok=st.get("last_ok"),
                         fetched=0, converted=0)
        self.log(f"report: {status}: {detail}")
        self._clear_spool()
        return {"ok": True}

    # -- drop / spool ---------------------------------------------------------
    def _write_drop(self, chats: list[dict]) -> Path:
        self.inbox.mkdir(parents=True, exist_ok=True)
        stamp = self.now().astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest, n = self.inbox / f"claude-ai-sync-{stamp}.json", 1
        while dest.exists():
            n += 1
            dest = self.inbox / f"claude-ai-sync-{stamp}-{n}.json"
        # atomic_write: temp file in the same dir (ending .part, which the
        # ingest globs ignore) then os.replace, so ingest never sees half a drop.
        dc.atomic_write(dest, json.dumps(chats, ensure_ascii=False) + "\n")
        return dest

    def _clear_spool(self) -> None:
        for f in self.spool.glob("*.json"):
            f.unlink(missing_ok=True)

    def close(self) -> None:
        shutil.rmtree(self.spool, ignore_errors=True)


def serve(stdin, stdout, host: Host) -> None:
    host.log("host started")
    try:
        while True:
            try:
                msg = read_message(stdin)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                host.log(f"bad frame: {exc}")
                write_message(stdout, {"ok": False, "error": f"bad frame: {exc}"})
                return  # framing is lost; Chrome reconnects on the next run
            if msg is None:
                return
            write_message(stdout, host.handle(msg))
    finally:
        host.log("host stopped")
        host.close()


def set_converted(n: int, host: Host | None = None) -> dict:
    """Record how many chats the cron job converted (contract field `converted`).

    Called by bin/sync-claude-ai.sh. Touches nothing else in the state file; a
    missing file is left missing (nothing has synced, so there is no run to
    annotate).
    """
    host = host or Host()
    try:
        if not host.state_path.exists():
            return {}
        return host.write_state(converted=int(n))
    finally:
        host.close()


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--set-converted":
        print(json.dumps(set_converted(int(sys.argv[2]))))
        return 0
    serve(sys.stdin.buffer, sys.stdout.buffer, Host())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
