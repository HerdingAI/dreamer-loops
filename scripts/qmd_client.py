#!/usr/bin/env python3
"""Warm qmd client.

One `qmd mcp` process stays up for the life of the context manager and answers
many hybrid (lex + vec, rerank off) queries; a cold `qmd vsearch` pays model
load every call (several seconds cold vs a fraction of one warm).

    with QmdClient() as qmd:
        hits, degraded = qmd.query("kiln glaze recipes", "transcripts")

Every query returns `(hits, degraded)`. `degraded=True` means retrieval FAILED
(crash, hang past `frontier.qmd_timeout_seconds`, protocol error, unregistered
collection, unexpected shape). A caller must read that as "cannot judge", never
as "never discussed": `([], False)` is the only genuine "nothing found". After
one failure the client stays degraded and does not respawn, so a sick server
costs one timeout per run, not one per query.

Each hit is `{docid, file, line, title, score, snippet}`; `line` is the best
matching chunk's line in the source markdown (owner_text.passages centres on
it), so a hit without one is a protocol change and degrades the result.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, p  # noqa: E402

_PROTOCOL = "2024-11-05"
_COLLECTIONS_RE = re.compile(r"^Collections \([^)]*\):\s*(.+)$", re.M)
_HIT_KEYS = ("docid", "file", "line", "title", "score", "snippet")


def _cfg(key: str):
    return CFG["frontier"][key]


def parse_collections(instructions: str) -> set[str] | None:
    """Collections qmd names in its initialize `instructions`, or None when the
    line is absent (format change: then the registration check is skipped)."""
    m = _COLLECTIONS_RE.search(instructions or "")
    if not m:
        return None
    return {c.strip() for c in m.group(1).split(",") if c.strip()}


def _clean_segment(segment: str) -> str:
    """One path segment as qmd stores it: every run of characters that is not a
    letter, a digit or "$" becomes a single "-", and edge dashes go."""
    out, gap = [], False
    for ch in segment:
        if ch == "$" or unicodedata.category(ch)[0] in "LN":
            if gap and out:
                out.append("-")
            out.append(ch)
            gap = False
        else:
            gap = True
    return "".join(out)


def handelize(path: str) -> str:
    """qmd's display path for a file path (port of qmd 2.5.3 store.js handelize,
    pins.qmd_version; emoji-to-hex is left out, Dreamer file names carry none).

    A hit's `file` is this display path, NOT the name on disk:
    "2026-01-05--kiln-notes.md" is reported as "2026-01-05-kiln-notes.md"."""
    parts = [s for s in path.replace("___", "/").split("/")]
    out = []
    for i, seg in enumerate(parts):
        if i == len(parts) - 1:
            m = re.search(r"(\.[a-z0-9]+)$", seg, re.I)
            ext = m.group(1) if m else ""
            seg = _clean_segment(seg[: len(seg) - len(ext)] if ext else seg) + ext
        else:
            seg = _clean_segment(seg)
        if seg:
            out.append(seg)
    return "/".join(out)


def resolve_display_path(base: Path, display_rest: str) -> Path | None:
    """The file under `base` (a collection root) that qmd reports as `display_rest`
    (the hit's file with the collection prefix removed), or None when it is gone,
    ambiguous, or outside `base`. The exact path is tried first; otherwise each
    segment is matched against the directory's entries by handelize."""
    root = base.resolve()

    def inside(path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(root)
        except OSError:
            return False

    exact = base / display_rest
    if inside(exact) and exact.is_file():
        return exact
    segs = [s for s in display_rest.split("/") if s]
    cur = base
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        try:
            entries = list(cur.iterdir())
        except OSError:
            return None
        found = [e for e in entries
                 if (e.is_file() if last else e.is_dir()) and handelize(e.name) == seg]
        if len(found) != 1 or not inside(found[0]):
            return None
        cur = found[0]
    return cur if segs else None


def _lex_text(text: str) -> str:
    """qmd lex syntax gives `"` and a leading `-` meaning; free text must not."""
    words = [w.replace('"', "") for w in text.split()]
    return " ".join(w for w in words if w and not w.startswith("-"))


class _Degraded(Exception):
    """Internal: this call cannot be answered."""


class QmdClient:
    def __init__(self, argv: list[str] | None = None, timeout: float | None = None,
                 cwd: str | Path | None = None):
        self._argv = list(argv) if argv else ["qmd", "mcp"]
        self._timeout = float(timeout if timeout is not None else _cfg("qmd_timeout_seconds"))
        # qmd resolves its index from the cwd; pinned to the project root like healthcheck.
        self._cwd = str(cwd) if cwd else str(p("root"))
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._known: set[str] | None = None
        self._ready = False
        self._broken = False
        self._next_id = 0

    # lifecycle -----------------------------------------------------------
    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def __enter__(self) -> "QmdClient":
        try:
            self._proc = subprocess.Popen(
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, cwd=self._cwd)
        except OSError as exc:
            print(f"qmd_client: cannot start {self._argv[0]}: {exc}", file=sys.stderr)
            self._broken = True
            return self
        threading.Thread(target=self._pump, args=(self._proc.stdout,), daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._kill()

    def _pump(self, stream) -> None:
        for line in stream:
            self._lines.put(line)
        self._lines.put(None)  # EOF: the server exited

    def _kill(self) -> None:
        proc = self._proc
        if not proc:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                stream and stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    # protocol ------------------------------------------------------------
    def _send(self, msg: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise _Degraded(f"write failed: {exc}") from exc

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        want = self._next_id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        deadline = _now() + self._timeout
        while True:
            try:
                line = self._lines.get(timeout=max(deadline - _now(), 0.001))
            except queue.Empty:
                raise _Degraded(f"{method} timed out after {self._timeout:g}s") from None
            if line is None:
                raise _Degraded("server exited")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _Degraded(f"non-JSON line from server: {exc}") from exc
            if not isinstance(msg, dict) or msg.get("id") != want:
                if _now() >= deadline:
                    raise _Degraded(f"{method} timed out after {self._timeout:g}s")
                continue  # notification or a stale reply
            if "error" in msg:
                raise _Degraded(f"{method} error: {msg['error']}")
            result = msg.get("result")
            if not isinstance(result, dict):
                raise _Degraded(f"{method}: no result")
            return result

    def _handshake(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": _PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "dreamer-frontier", "version": "1"}})
        self._known = parse_collections(str(result.get("instructions") or ""))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True

    # query ---------------------------------------------------------------
    def query(self, text: str, collection: str, limit: int | None = None,
              lex_only: bool = False) -> tuple[list[dict], bool]:
        """Hybrid (lex + vec) search, or with `lex_only` a keyword search alone.

        qmd ANDs every lex term, so a lex leg over a long text matches almost
        nothing; `lex_only` is for short texts such as a title, where it finds the
        chats that use those exact words and the meaning search ranks low."""
        if self._broken or not self._proc:
            return [], True
        try:
            if not self._ready:
                self._handshake()
            if self._known is not None and collection not in self._known:
                # qmd answers an unregistered collection with "no results".
                raise _Degraded(f"collection {collection!r} is not registered in qmd")
            # qmd refuses a structured search with a newline in it ("queries must be
            # single-line"), and callers pass "title\nsummary": send one line.
            text = " ".join(text.split())
            lex = _lex_text(text)
            if lex_only:
                if not lex:  # nothing to search for is an empty answer, not a failure
                    return [], False
                searches = [{"type": "lex", "query": lex}]
            else:
                searches = [{"type": "vec", "query": text}]
                if lex:
                    searches.insert(0, {"type": "lex", "query": lex})
            result = self._request("tools/call", {"name": "query", "arguments": {
                "searches": searches, "collections": [collection],
                "limit": int(limit if limit is not None else _cfg("qmd_default_limit")),
                "rerank": False}})
            return self._parse(result), False
        except _Degraded as exc:
            print(f"qmd_client: degraded: {exc}", file=sys.stderr)
            self._broken = True
            self._kill()
            return [], True

    @staticmethod
    def _parse(result: dict) -> list[dict]:
        if result.get("isError"):
            raise _Degraded("tool reported an error")
        structured = result.get("structuredContent")
        raw = structured.get("results") if isinstance(structured, dict) else None
        if not isinstance(raw, list):
            raise _Degraded("response has no structuredContent.results")
        hits = []
        for h in raw:
            if not isinstance(h, dict) or not h.get("file"):
                raise _Degraded("hit without a file")
            try:
                line = int(h["line"])
                score = float(h.get("score", 0.0))
            except (KeyError, TypeError, ValueError):
                raise _Degraded("hit without a usable line or score") from None
            hits.append({"docid": h.get("docid"), "file": h["file"], "line": line,
                         "title": h.get("title") or "", "score": score,
                         "snippet": h.get("snippet") or ""})
        return hits


def _now() -> float:
    return time.monotonic()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Warm qmd client probe (read-only).")
    ap.add_argument("--probe", required=True, metavar="QUERY")
    ap.add_argument("--collection", default="transcripts")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    with QmdClient() as qmd:
        hits, degraded = qmd.query(a.probe, a.collection, a.limit)
    print(f"collection={a.collection} hits={len(hits)} degraded={degraded}")
    for h in hits:
        print(f"{h['score']:.2f} {h['file']}:{h['line']}  {h['title']}")
    return 1 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
