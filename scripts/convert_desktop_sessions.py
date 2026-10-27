#!/usr/bin/env python3
"""Claude desktop app (Cowork) session ingestion — the deterministic half.

The desktop app's local agent writes each session to

    <root>/<account>/<org>/local_<id>.json          metadata (title, times, ...)
    <root>/<account>/<org>/local_<id>/audit.jsonl   Agent-SDK stream-json

audit.jsonl is the transcript. This module is the Claude Code converter
(convert_cc_sessions.py) with a different front end: it reuses that module's
cleaning, redaction, budgeting and prompt rendering, and only replaces what is
specific to this format — discovery, what counts as an owner turn, and the
quiet-window clock. Payloads are written under the same `.cc-input-<sid>` names
so bin/ingest-cc.sh summarises and applies them with no further changes.

Properties of the files that shape the code:

* On resume the app re-emits earlier user turns with `isReplay: true`. Each
  replay duplicates an earlier plain turn; they must not be counted or
  summarised twice.
* `parent_tool_use_id` has been null on every record seen, so subagent traffic
  is not distinguishable by it. The sidechain rule is kept as a defence, not
  as an observed fact.
* Each session dir also holds `.audit-key`, `.claude/.credentials.json` and a
  nested copy of the Claude Code jsonl. None of them is ever opened here.
* audit.jsonl holds plaintext secrets. Everything goes through clean_text()
  (dreamer_common.redact) before it reaches a payload or a prompt.

Deliberately dependency-light: stdlib + dreamer_common + convert_cc_sessions.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import convert_cc_sessions as ccs  # noqa: E402
from dreamer_common import (CFG, atomic_write, atomic_write_json, log,  # noqa: E402
                            read_json)

SOURCE_AGENT = "claude-desktop"

# Fallbacks so the job works before config.yaml has the block. The floors and
# the quiet window are inherited from cc_ingest: same rules, one dial.
DEFAULTS: dict[str, Any] = {
    "ledger": "desktop-ingested.json",
    "project_label": "desktop",
    "max_sessions_per_run": 10,
}


def load_cfg() -> dict[str, Any]:
    """cc_ingest settings, overridden by any `desktop_ingest` block."""
    cfg = ccs.load_cfg()
    cfg.update(DEFAULTS)
    cfg.update(CFG.get("desktop_ingest") or {})
    return cfg


def default_root(cfg: dict | None = None) -> Path:
    corpora = (cfg if cfg is not None else CFG).get("corpora", {})
    return Path(corpora.get("claude_desktop_sessions")
                or "~/.config/Claude/local-agent-mode-sessions").expanduser()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

read_records = ccs.read_records


def read_meta(path: Path) -> dict:
    """The metadata json, or {} when absent or unreadable.

    Only title, lastActivityAt and isArchived are ever used. The file also
    carries the system prompt and account email; those are never read on.
    """
    meta = read_json(path, default=None)
    return meta if isinstance(meta, dict) else {}


def meta_path(audit: Path) -> Path:
    return audit.parent.with_name(audit.parent.name + ".json")


def session_files(root: Path) -> list[Path]:
    """<root>/<account>/<org>/local_<id>/audit.jsonl and nothing deeper.

    The nested `.claude/projects/**.jsonl` inside each session dir is the
    Claude Code CLI's own copy of the same conversation; ingesting it would
    file every session twice.
    """
    return sorted(root.glob("*/*/local_*/audit.jsonl"))


def is_owner_turn(rec: dict) -> bool:
    return (rec.get("type") == "user"
            and not rec.get("isReplay")
            and not rec.get("isSynthetic")
            and not rec.get("isMeta")
            and rec.get("parent_tool_use_id") is None)


def _is_agent_turn(rec: dict) -> bool:
    return (rec.get("type") == "assistant"
            and rec.get("parent_tool_use_id") is None)


# The app prefixes a turn with a manifest of attached files (host paths).
_UPLOADED = re.compile(r"<uploaded_files>(.*?)</uploaded_files>",
                       re.DOTALL | re.IGNORECASE)


def _collapse_uploads(text: str) -> str:
    def sub(m: re.Match) -> str:
        n = max(1, m.group(1).lower().count("<file>"))
        return f"_[{n} file{'s' if n != 1 else ''} attached]_"
    return _UPLOADED.sub(sub, text)


def _prepared(rec: dict) -> dict:
    """A copy of the record with attachment manifests collapsed."""
    rec = dict(rec)
    msg = dict(rec.get("message") or {})
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = _collapse_uploads(content)
    elif isinstance(content, list):
        msg["content"] = [
            dict(b, text=_collapse_uploads(b["text"]))
            if isinstance(b, dict) and isinstance(b.get("text"), str) else b
            for b in content]
    rec["message"] = msg
    return rec


def owner_text(rec: dict) -> str:
    return ccs.owner_text(_prepared(rec))


def human_turns(path: Path) -> list[str]:
    return [t for t in (owner_text(r) for r in read_records(path)
                        if is_owner_turn(r)) if t]


def conversation(path: Path, cfg: dict[str, Any] | None = None) -> list[dict]:
    """[{role, text}, ...] — same shape and merging rules as the CC converter."""
    cfg = cfg or load_cfg()
    out: list[dict] = []
    for rec in read_records(path):
        if is_owner_turn(rec):
            role = "human"
        elif _is_agent_turn(rec):
            role = "assistant"
        else:
            continue
        text = ccs._turn_text(_prepared(rec), cfg)
        if not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["text"] = ccs._join_turn(out[-1]["text"], text)
        else:
            out.append({"role": role, "text": text})
    return out


def session_span(path: Path) -> tuple[str, str]:
    """(first, last) timestamp over conversation records.

    Older files predate the `timestamp` field and carry only
    `_audit_timestamp`.
    """
    first = last = ""
    for rec in read_records(path):
        if rec.get("type") not in ("user", "assistant"):
            continue
        ts = rec.get("timestamp") or rec.get("_audit_timestamp")
        if isinstance(ts, str) and len(ts) >= 10:
            first = first or ts
            last = ts
    return first, last


def fingerprint(path: Path) -> str:
    """Hash of the owner's turns, for dedupe of one conversation filed twice."""
    h = hashlib.sha256()
    for t in human_turns(path):
        h.update(t.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------


def last_activity(audit: Path, meta: dict) -> float:
    """Newest sign of life, epoch seconds: the app's lastActivityAt (ms) or the
    audit file's mtime, whichever is later. Using the later of the two can only
    delay an ingest, never cut a live session in half."""
    candidates = [audit.stat().st_mtime]
    la = meta.get("lastActivityAt")
    if isinstance(la, (int, float)) and la > 0:
        candidates.append(la / 1000.0)
    return max(candidates)


def triage(audit: Path, meta: dict, cfg: dict[str, Any] | None = None,
           now: float | None = None) -> tuple[bool, str]:
    """(accepted, reason). Same rules and wording as the Claude Code triage.

    Not ported: the entrypoint filter (no headless/cron mode exists here) and
    exclude_projects (no project dimension). Archived sessions are NOT
    rejected — Claude Code has no equivalent rule and archiving is tidying.
    """
    cfg = cfg or load_cfg()
    now = time.time() if now is None else now

    age = now - last_activity(audit, meta)
    if age < float(cfg["min_quiet_hours"]) * 3600:
        return False, (f"still active — last activity {age / 3600:.1f}h ago, "
                       f"under the {cfg['min_quiet_hours']}h quiet window")

    turns = human_turns(audit)
    if len(turns) < int(cfg["min_human_turns"]):
        return False, (f"{len(turns)} human turns, "
                       f"minimum {cfg['min_human_turns']}")
    chars = sum(len(t) for t in turns)
    if chars < int(cfg["min_human_chars"]):
        return False, f"{chars} human chars, minimum {cfg['min_human_chars']}"
    return True, ""


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------

_SOURCE_NOTE = ("this is a Claude desktop app (Cowork) agent session — often "
                "not coding at all (research, writing, planning). Reconstruct "
                "it the same way.")


def build_payload(audit: Path, meta: dict, cfg: dict[str, Any]) -> dict:
    turns = conversation(audit, cfg)
    n_human = sum(1 for t in turns if t["role"] == "human")
    turns, truncated = ccs._fit(turns, int(cfg["max_summarizer_chars"]))
    first, last = session_span(audit)
    title = meta.get("title")
    return {
        "session_id": audit.parent.name,
        "project": cfg["project_label"],
        "date": first[:10],
        "updated_at": last,
        "ai_title": ccs.clean_text(title, cfg) if isinstance(title, str) else "",
        "human_turns": n_human,
        "truncated": truncated,
        "turns": turns,
        "source_agent": SOURCE_AGENT,
        "ledger": cfg["ledger"],
        "source_note": _SOURCE_NOTE,
    }


def render_prompt(payload: dict, template: str | None = None) -> str:
    """The CC prompt, with the source stated so the summariser does not assume
    a coding session."""
    text = ccs.render_prompt(payload, template)
    marker = f"- project: `{payload['project']}`"
    return text.replace(marker, f"{marker}\n- source: {payload['source_note']}", 1)


def bounded_prompt(payload: dict, cfg: dict[str, Any]) -> str:
    """ccs.bounded_prompt with this module's renderer."""
    prompt = render_prompt(payload)
    budget = int(cfg["max_summarizer_chars"])
    while len(prompt.encode("utf-8")) > ccs.MAX_PROMPT_BYTES and budget > 2000:
        budget = budget * 2 // 3
        payload["turns"], trimmed = ccs._fit(payload["turns"], budget)
        payload["truncated"] = payload["truncated"] or trimmed
        prompt = render_prompt(payload)
    return prompt


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------


def scan(root: Path, *, out_dir: Path, ledger_path: Path,
         cfg: dict[str, Any] | None = None, now: float | None = None,
         limit: int | None = None, dry_run: bool = False) -> dict:
    """Triage every session; emit a payload for each that qualifies.

    Ledger semantics are Claude Code's: a terminal entry with an unchanged
    (mtime, size) is skipped; `pending` is retried; a changed file is triaged
    again, so a session that grew after ingest is re-summarised exactly as a
    grown Claude Code session is.
    """
    cfg = cfg or load_cfg()
    now = time.time() if now is None else now
    ledger: dict[str, Any] = {} if dry_run else (read_json(ledger_path, default={}) or {})
    stats = {"scanned": 0, "accepted": 0, "rejected": 0, "skipped": 0}

    # fingerprint -> sid, for sessions already ingested or accepted this run.
    seen: dict[str, str] = {
        e["fingerprint"]: sid for sid, e in ledger.items()
        if isinstance(e, dict) and e.get("fingerprint")
        and e.get("status") in ("ingested", "pending")}

    for audit in session_files(root):
        if limit is not None and stats["accepted"] >= limit:
            break
        stats["scanned"] += 1
        sid = audit.parent.name
        st = audit.stat()

        prior = ledger.get(sid)
        if (prior and prior.get("mtime") == st.st_mtime
                and prior.get("size") == st.st_size
                and prior.get("status") in ccs.TERMINAL):
            stats["skipped"] += 1
            continue

        meta = read_meta(meta_path(audit))
        ok, reason = triage(audit, meta, cfg, now=now)
        entry = {"mtime": st.st_mtime, "size": st.st_size,
                 "project": cfg["project_label"], "source": str(audit),
                 "archived": bool(meta.get("isArchived"))}

        fp = ""
        if ok:
            fp = fingerprint(audit)
            twin = seen.get(fp)
            if twin and twin != sid:
                ok, reason = False, f"duplicate of {twin} (identical owner turns)"

        if not ok:
            stats["rejected"] += 1
            entry.update(status="rejected", reason=reason)
            ledger[sid] = entry
            continue

        stats["accepted"] += 1
        seen[fp] = sid
        payload = build_payload(audit, meta, cfg)
        entry.update(status="pending", reason="", fingerprint=fp,
                     date=payload["date"], human_turns=payload["human_turns"])
        ledger[sid] = entry
        if not dry_run:
            prompt = bounded_prompt(payload, cfg)
            atomic_write(out_dir / f".cc-input-{sid}.md", prompt)
            atomic_write_json(out_dir / f".cc-input-{sid}.json", payload)

    if not dry_run:
        atomic_write_json(ledger_path, ledger)

    log(f"desktop scanned={stats['scanned']} accepted={stats['accepted']} "
        f"rejected={stats['rejected']} skipped={stats['skipped']}")
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse

    from dreamer_common import p

    cfg = load_cfg()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=default_root(),
                    help="Claude desktop local-agent-mode-sessions directory")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=cfg["max_sessions_per_run"])
    ap.add_argument("--dry-run", action="store_true",
                    help="triage and report; write nothing")
    args = ap.parse_args(argv)

    out = args.out or p("logs")
    ledger = args.ledger or (p("meta") / cfg["ledger"])
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)

    if not args.root.is_dir():
        # Not an error: the desktop app may not be installed on this host, and
        # the Claude Code leg of the same cron line must still run.
        log(f"no desktop sessions directory at {args.root} — nothing to do")
        print(json.dumps({"scanned": 0, "accepted": 0, "rejected": 0,
                          "skipped": 0}))
        return 0

    stats = scan(args.root, out_dir=out, ledger_path=ledger, cfg=cfg,
                 limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
