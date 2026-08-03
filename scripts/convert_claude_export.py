#!/usr/bin/env python3
"""Claude export -> one Markdown transcript per conversation (§6.1).

Written against the REAL export schema, verified 2026-08-01 against an 887-
conversation dump. The v2.0 spec (and §12) described `id` / `messages`, which
do not exist. Actual shape:

    [ { uuid, name, summary, created_at, updated_at, account,
        chat_messages: [ { uuid, sender, text, content: [...],
                           attachments: [...], files: [...],
                           created_at, updated_at } ] } ]

Two traps this handles that a naive `.text` reader would not:
  1. 104 messages in the real archive carry an EMPTY `text` but a populated
     `content[]`. Reading `.text` alone silently drops them.
  2. `content[]` block types include thinking / tool_use / tool_result /
     token_budget / voice_note. Tool noise is not reasoning the owner did, and
     including it would poison loop extraction, so it is summarised, not dumped.

Usage:
    convert_claude_export.py <export-dir-or-zip-or-json> [--limit N]
                            [--out DIR] [--dry-run] [--oldest-first]
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, as_date, atomic_write, atomic_write_json, log, p, read_json, redact,
    safe_relpath, slugify,
)

# Content block types that represent actual reasoning by human or assistant.
PROSE_BLOCKS = {"text", "voice_note"}
# Block types deliberately excluded from transcripts. `thinking` is omitted
# because it is model scratchpad, not the owner's reasoning, and it would
# dominate token cost during extraction for no gain.
NOISE_BLOCKS = {"thinking", "token_budget"}


def rel_claimed_by_other(ledger: dict, rel: str, cid: str) -> bool:
    """True if another conversation already owns this path."""
    for other_cid, entry in ledger.items():
        if other_cid != cid and isinstance(entry, dict) and entry.get("path") == rel:
            return True
    return False


def locate_conversations(target: Path) -> tuple[list, str]:
    """Return (conversations, provenance_label). Accepts dir, zip, or json."""
    if target.is_dir():
        cand = target / "conversations.json"
        if not cand.exists():
            raise SystemExit(f"no conversations.json in {target}")
        return json.loads(cand.read_text(encoding="utf-8")), cand.name
    if target.suffix == ".zip":
        with zipfile.ZipFile(target) as zf:
            names = [n for n in zf.namelist() if n.endswith("conversations.json")]
            if not names:
                raise SystemExit(f"no conversations.json inside {target}")
            with zf.open(names[0]) as fh:
                return json.load(fh), names[0]
    if target.suffix == ".json":
        return json.loads(target.read_text(encoding="utf-8")), target.name
    raise SystemExit(f"unsupported export target: {target}")


def message_text(msg: dict) -> str:
    """Extract prose from a message, tolerating the empty-text case."""
    parts: list[str] = []
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in PROSE_BLOCKS:
            t = (block.get("text") or "").strip()
            if t:
                parts.append(t)
        elif btype == "tool_use":
            # Keep a one-line marker: the fact that a tool ran is sometimes the
            # context that makes a following remark legible, but the payload is
            # never the owner's reasoning.
            parts.append(f"_[tool_use: {block.get('name', 'unknown')}]_")
        elif btype == "tool_result":
            parts.append("_[tool_result omitted]_")
        elif btype in NOISE_BLOCKS:
            continue
    if not parts:
        # Fall back to the flat field for older records that predate content[].
        flat = (msg.get("text") or "").strip()
        if flat:
            parts.append(flat)
    # Pasted documents are where both ideas and credentials live; keep them.
    for att in msg.get("attachments") or []:
        if isinstance(att, dict):
            extracted = (att.get("extracted_content") or "").strip()
            if extracted:
                name = att.get("file_name", "attachment")
                parts.append(f"\n**[attachment: {name}]**\n\n{extracted}")
    return "\n\n".join(parts).strip()


def render(conv: dict) -> tuple[str, dict[str, int]]:
    """Return (markdown_body, redaction_counts)."""
    lines: list[str] = []
    total_counts: dict[str, int] = {}
    for msg in conv.get("chat_messages") or []:
        if not isinstance(msg, dict):
            continue
        body = message_text(msg)
        if not body:
            continue
        body, counts = redact(body)
        for k, v in counts.items():
            total_counts[k] = total_counts.get(k, 0) + v
        sender = msg.get("sender", "unknown")
        role = {"human": "Human", "assistant": "Assistant"}.get(sender, sender.title())
        lines.append(f"## {role}\n\n{body}")
    return "\n\n".join(lines), total_counts


def convert(target: Path, out_dir: Path, limit: int | None, dry_run: bool,
            oldest_first: bool) -> dict:
    conversations, provenance = locate_conversations(target)
    log(f"loaded {len(conversations)} conversations from {provenance}", job="convert")

    ledger_path = p("meta") / "ingested.json"
    ledger: dict = read_json(ledger_path, default={}) or {}

    # Chronological order (§6.8 rule 3) so recurrence accretes as it did in life.
    # The sort runs BEFORE the per-record try/except, so it must tolerate junk
    # itself — otherwise one non-object record aborts the whole run, which is
    # the exact failure mode DoD 6.1 forbids.
    def sort_key(c: object) -> str:
        if isinstance(c, dict):
            return str(c.get("created_at") or "")
        return ""

    conversations = sorted(conversations, key=sort_key, reverse=not oldest_first)

    stats = {"seen": 0, "emitted": 0, "written": 0, "replaced": 0,
             "skipped_dedupe": 0, "skipped_bad": 0, "skipped_empty": 0,
             "redactions": {}}

    for conv in conversations:
        if limit is not None and stats["emitted"] >= limit:
            break
        stats["seen"] += 1
        try:
            if not isinstance(conv, dict):
                raise ValueError("record is not an object")
            cid = conv.get("uuid")
            if not cid:
                raise ValueError("record has no uuid")
            updated = str(conv.get("updated_at") or conv.get("created_at") or "")
            created = as_date(conv.get("created_at"))
            if created is None:
                raise ValueError(f"unparseable created_at: {conv.get('created_at')!r}")

            prior = ledger.get(cid)
            if prior and prior.get("updated_at") == updated:
                stats["skipped_dedupe"] += 1
                continue

            body, counts = render(conv)
            if not body.strip():
                stats["skipped_empty"] += 1
                continue

            slug = slugify(conv.get("name") or "untitled")
            rel = safe_relpath(f"{created:%Y}", f"{created:%m}",
                               f"{created:%Y-%m-%d}--{slug}.md")
            # Two different conversations can share a date AND a title — real
            # archives do this routinely ("Cheapest plan for 100k requests"
            # twice in one day). Without disambiguation the second silently
            # overwrites the first, and the ledger marks both ingested so the
            # lost one is never re-emitted. Suffix with the conversation id.
            if rel_claimed_by_other(ledger, str(rel), cid):
                rel = safe_relpath(f"{created:%Y}", f"{created:%m}",
                                   f"{created:%Y-%m-%d}--{slug}--{cid[:8]}.md")
            dest = out_dir / rel

            fm = [
                "---",
                "type: transcript",
                "source_agent: claude.ai",
                f"conversation_id: {cid}",
                f"date: {created.isoformat()}",
                f"updated_at: {updated}",
                f'title: "{(conv.get("name") or "Untitled").replace(chr(34), chr(39))}"',
                f"message_count: {len(conv.get('chat_messages') or [])}",
                "---",
                "",
                f"# {conv.get('name') or 'Untitled'}",
                "",
                "",
            ]
            content = "\n".join(fm) + body + "\n"

            if not dry_run:
                # A conversation that grew replaces its prior file; if the slug
                # changed too, remove the stale path so links do not fork.
                old_path = prior.get("path") if prior else None
                if old_path and old_path != str(rel):
                    stale = out_dir / old_path
                    if stale.exists():
                        stale.unlink()
                atomic_write(dest, content)
                ledger[cid] = {"updated_at": updated, "path": str(rel),
                               "date": created.isoformat()}

            stats["replaced" if prior else "written"] += 1
            stats["emitted"] += 1
            for k, v in counts.items():
                stats["redactions"][k] = stats["redactions"].get(k, 0) + v

        except Exception as exc:  # noqa: BLE001 — one bad record must not abort
            stats["skipped_bad"] += 1
            log(f"SKIP bad record #{stats['seen']}: {exc}", job="convert")

    if not dry_run:
        atomic_write_json(ledger_path, ledger)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default=CFG["corpora"]["claude_export"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N emitted transcripts (Phase 0.5a slice)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--oldest-first", action="store_true", default=True)
    ap.add_argument("--newest-first", dest="oldest_first", action="store_false")
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else p("sources")
    stats = convert(Path(args.target), out_dir, args.limit, args.dry_run,
                    args.oldest_first)

    log(f"seen={stats['seen']} emitted={stats['emitted']} new={stats['written']} "
        f"replaced={stats['replaced']} dedupe-skip={stats['skipped_dedupe']} "
        f"empty-skip={stats['skipped_empty']} bad-skip={stats['skipped_bad']}",
        job="convert")
    if stats["redactions"]:
        total = sum(stats["redactions"].values())
        log(f"REDACTED {total} secret(s): {stats['redactions']}", job="convert")
    else:
        log("no secrets detected", job="convert")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
