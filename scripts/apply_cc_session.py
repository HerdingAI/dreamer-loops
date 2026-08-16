#!/usr/bin/env python3
"""Claude Code session ingestion — write the page.

Design notes: docs/architecture.md (Claude Code ingestion).

The deterministic half of the summarising step. Takes the payload
convert_cc_sessions.py produced and the JSON the summariser returned, and
writes one transcript page — or refuses and writes nothing.

Refusing matters more here than in most places. `vault/sources/` is immutable
and every run commits to git, so a page written wrong is retained permanently
and cannot be quietly fixed. The prompt asks the summariser for no code, no
paths and no credentials; this module is the assertion that it complied. A
prompt instruction is a mechanism, not a guarantee.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (atomic_write, atomic_write_json, log,  # noqa: E402
                            parse_frontmatter, read_json, redact,
                            safe_relpath, slugify)

REQUIRED = ("title", "goal", "solution", "outcome", "unresolved", "turns")

HEADINGS = {
    "human": "## Human (reconstructed)",
    "assistant": "## Assistant (reconstructed)",
}

_JSON_SPAN = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE = re.compile(r"^\s*```", re.MULTILINE)
_HOME_PATH = re.compile(r"/home/|/Users/|(?:^|\s)[A-Za-z]:\\")


# --------------------------------------------------------------------------
# Parsing the summariser's reply
# --------------------------------------------------------------------------


def parse_summary(text: str) -> dict:
    """The JSON object out of whatever the model actually returned.

    The contract asks for bare JSON on stdout and the model fences it anyway —
    observed live on the first real session. Tolerating that is cheaper than
    losing the run, and matches how backfill.sh already quarantines
    unparseable output instead of assuming it away.
    """
    m = _JSON_SPAN.search(text or "")
    if not m:
        raise ValueError("no JSON object in the summariser's reply")
    try:
        data = json.loads(m.group(0))
    except ValueError as e:
        raise ValueError(f"summariser reply is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("summariser returned JSON that is not an object")

    missing = [f for f in REQUIRED if f not in data]
    if missing:
        raise ValueError(f"summary is missing {', '.join(missing)}")
    if not str(data["title"]).strip():
        raise ValueError("summary has an empty title")
    if not isinstance(data["turns"], list):
        raise ValueError("summary `turns` is not a list")
    for i, t in enumerate(data["turns"]):
        if not isinstance(t, dict) or "role" not in t or "text" not in t:
            raise ValueError(f"turn {i} is not a {{role, text}} object")
        if t["role"] not in HEADINGS:
            raise ValueError(f"turn {i} has unknown role {t['role']!r}")
    return data


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def page_text(payload: dict, summary: dict) -> str:
    """The full page.

    Frontmatter is hand-assembled rather than YAML-dumped, the way
    convert_claude_export.py assembles a transcript's, so both ingestion paths
    produce byte-comparable pages.
    """
    title = str(summary["title"]).strip().replace('"', "'")
    fm = [
        "---",
        "type: transcript",
        "source_agent: claude-code",
        f"session_id: {payload['session_id']}",
        f"project: {payload['project']}",
        f"date: {payload['date']}",
        f"updated_at: {payload.get('updated_at') or payload['date']}",
        f'title: "{title}"',
        f"human_turns: {payload.get('human_turns', 0)}",
        "---",
        "",
        f"# {title}",
        "",
        "## Session abstract (derived)",
        "",
        f"**Goal:** {str(summary['goal']).strip()}",
        "",
        f"**Solution reached:** {str(summary['solution']).strip()}",
        "",
        f"**Outcome / current state:** {str(summary['outcome']).strip()}",
        "",
        f"**Left unresolved:** {str(summary['unresolved']).strip()}",
        "",
    ]
    if payload.get("truncated"):
        fm += ["_The source session was trimmed to fit the summariser budget; "
               "the middle of the conversation is not represented._", ""]

    body: list[str] = []
    for t in summary["turns"]:
        body += [HEADINGS[t["role"]], "", str(t["text"]).strip(), ""]

    return "\n".join(fm + body).rstrip() + "\n"


def check_clean(text: str) -> None:
    """Refuse a page that still carries what the prompt was told to leave out.

    Raises ValueError naming what was found. The caller writes nothing.
    """
    if _CODE_FENCE.search(text):
        raise ValueError("page carries a fenced code block")
    _redacted, counts = redact(text)
    if counts:
        kinds = ", ".join(sorted(counts))
        raise ValueError(f"page carries a secret-shaped string ({kinds})")
    if _HOME_PATH.search(text):
        raise ValueError("page carries a filesystem path")


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


def target_path(payload: dict, title: str, sources_dir: Path) -> Path:
    """`YYYY/MM/YYYY-MM-DD--slug.md`, disambiguated on a real collision.

    Collisions are settled against what is on disk rather than against the two
    ledgers. A ledger can drift from the tree; the tree cannot drift from
    itself, and a page already carries its own `session_id`. Re-ingesting a
    grown session therefore lands on its own page and rewrites it, while a
    different session with the same date and title takes a suffix — the same
    resolution convert_claude_export.py uses.
    """
    date = str(payload["date"])
    sid = str(payload["session_id"])
    slug = slugify(title)
    rel = safe_relpath(date[:4], date[5:7], f"{date}--{slug}.md")
    path = sources_dir / rel
    if not path.exists():
        return path
    fm, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    if str(fm.get("session_id") or "") == sid:
        return path
    rel = safe_relpath(date[:4], date[5:7], f"{date}--{slug}--{sid[:8]}.md")
    return sources_dir / rel


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


def apply_session(payload: dict, summary_text: str, *, sources_dir: Path,
                  ledger_path: Path) -> Path:
    """Validate, render, assert clean, then write. In that order.

    Nothing touches the filesystem until the page has passed every check, so
    a refusal leaves no half-written page and no ledger entry claiming one.
    """
    summary = parse_summary(summary_text)
    text = page_text(payload, summary)
    check_clean(text)

    path = target_path(payload, str(summary["title"]).strip(), sources_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, text)

    ledger: dict[str, Any] = read_json(ledger_path, default={}) or {}
    entry = dict(ledger.get(payload["session_id"]) or {})
    entry.update(status="ingested", reason="",
                 path=path.relative_to(sources_dir).as_posix(),
                 date=payload["date"], project=payload["project"],
                 human_turns=payload.get("human_turns", 0))
    ledger[payload["session_id"]] = entry
    atomic_write_json(ledger_path, ledger)

    log(f"wrote {path.relative_to(sources_dir).as_posix()}")
    return path


def record_failure(payload: dict, reason: str, *, ledger_path: Path) -> None:
    """Mark a session as refused, with why.

    A refusal has to be terminal and visible. Left `pending`, the nightly
    sweep would re-summarise the session and refuse it again every night at
    full cost; deleted outright, the reason would be lost and the same
    over-strict check would look like a quiet week.
    """
    ledger: dict[str, Any] = read_json(ledger_path, default={}) or {}
    entry = dict(ledger.get(payload["session_id"]) or {})
    entry.pop("path", None)
    entry.update(status="failed", reason=reason,
                 date=payload.get("date", ""), project=payload.get("project", ""))
    ledger[payload["session_id"]] = entry
    atomic_write_json(ledger_path, ledger)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from dreamer_common import p

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--payload", type=Path, required=True,
                    help="the .cc-input-<sid>.json written by the scan")
    ap.add_argument("--summary", type=Path, required=True,
                    help="the summariser's reply")
    ap.add_argument("--sources", type=Path, default=None)
    ap.add_argument("--ledger", type=Path, default=None)
    args = ap.parse_args(argv)

    sources = args.sources or p("sources")
    ledger = args.ledger or (p("meta") / "cc-ingested.json")

    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    try:
        path = apply_session(payload,
                             args.summary.read_text(encoding="utf-8"),
                             sources_dir=sources, ledger_path=ledger)
    except ValueError as e:
        # Loud, terminal and non-zero: the wrapper must not commit a success
        # message for a session that produced no page, and the sweep must not
        # re-buy the same refusal tomorrow night.
        record_failure(payload, str(e), ledger_path=ledger)
        log(f"FATAL: {payload.get('session_id', '?')}: {e}")
        return 1
    print(json.dumps({"session_id": payload["session_id"],
                      "path": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
