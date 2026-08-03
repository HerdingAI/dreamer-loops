"""Shared helpers for Dreamer scripts (§6.1-6.9).

Deliberately dependency-light: stdlib + PyYAML only. Every module that
touches the vault goes through here so the write-safety rules (§6.3 rule 8)
are enforced in one place rather than re-implemented per script.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


CFG = load_config()


def p(key: str) -> Path:
    """Resolve a configured path by its key under `paths:`.

    Relative paths resolve against the repo root, so a checkout works wherever
    it is cloned. Absolute paths are honoured unchanged, which is what you
    want for a vault kept outside the repo (on a NAS, or in a separate
    private git repo so your notes never share history with the code).
    """
    raw = Path(CFG["paths"][key]).expanduser()
    return raw if raw.is_absolute() else (ROOT / raw)


# --------------------------------------------------------------------------
# Atomic writes (§6.7 "Consistent reads")
# --------------------------------------------------------------------------
# wiki-lock is advisory and does NOT stop a non-participating reader (e.g.
# dreamer-mcp) from opening a file mid-write. The guarantee therefore comes
# from the writer side: write to a temp file in the same directory, fsync,
# then os.replace() — which is atomic on POSIX. A reader sees the old file
# or the new one, never a partial one.


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray .part behind to confuse the linter or the catalog.
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


# --------------------------------------------------------------------------
# Slug sanitization (§6.1)
# --------------------------------------------------------------------------
# The slug derives from the owner-authored conversation `name`. It is used as
# a filename component, so traversal sequences and separators must not survive.

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str, maxlen: int = 60) -> str:
    s = (name or "").lower()
    s = _SLUG_STRIP.sub("-", s).strip("-")
    # Collapse and truncate on a word boundary where possible.
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] or s[:maxlen]
    return s or "untitled"


def safe_relpath(*parts: str) -> Path:
    """Join path parts, guaranteeing the result stays relative and downward."""
    joined = Path(*[slugify(x) if os.sep in x or ".." in x else x for x in parts])
    resolved = Path(os.path.normpath(str(joined)))
    if resolved.is_absolute() or any(seg == ".." for seg in resolved.parts):
        raise ValueError(f"unsafe path components: {parts!r}")
    return resolved


# --------------------------------------------------------------------------
# Secret redaction (§6.1)
# --------------------------------------------------------------------------
# A multi-year archive of AI coding conversations reliably contains pasted
# credentials. sources/ is immutable and every run commits to git, so anything
# ingested is retained permanently — redaction must happen before the first
# write, not after. Patterns are ordered most-specific-first.

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key-block",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    # Connection strings: capture the password segment only, keep the rest
    # legible so the conversation still makes sense after redaction.
    ("db-dsn-password",
     re.compile(r"(?P<pre>\b(?:postgres|postgresql|mysql|mongodb|redis|amqp)(?:\+\w+)?://"
                r"[^\s:/@]+:)(?P<secret>[^\s@]{3,})(?P<post>@)")),
    ("bearer-token", re.compile(r"(?i)\b(?:authorization:\s*bearer|bearer)\s+[A-Za-z0-9_\-\.=]{20,}")),
    ("generic-assignment",
     re.compile(r"(?i)(?P<pre>\b(?:api[_-]?key|secret|passwd|password|access[_-]?token|"
                r"client[_-]?secret)\b\s*[=:]\s*['\"]?)(?P<secret>[A-Za-z0-9_\-\.\/\+]{8,})"
                r"(?P<post>['\"]?)")),
]


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return (redacted_text, {pattern_name: count}). Never raises."""
    counts: dict[str, int] = {}
    if not text:
        return text, counts
    for name, pat in _SECRET_PATTERNS:
        groups = pat.groupindex

        def _sub(m: re.Match, _n=name, _g=groups) -> str:
            counts[_n] = counts.get(_n, 0) + 1
            if "secret" in _g:
                return f"{m.group('pre')}[REDACTED:{_n}]{m.group('post')}"
            return f"[REDACTED:{_n}]"

        text = pat.sub(_sub, text)
    return text, counts


# --------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def render_frontmatter(fm: dict, body: str) -> str:
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True,
                            default_flow_style=False).rstrip("\n")
    return f"---\n{dumped}\n---\n\n{body.lstrip(chr(10))}"


def read_page(path: Path) -> tuple[dict, str]:
    return parse_frontmatter(path.read_text(encoding="utf-8"))


def write_page(path: Path, fm: dict, body: str) -> None:
    atomic_write(path, render_frontmatter(fm, body))


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def today() -> _dt.date:
    """Today's date. Overridable via DREAMER_TODAY for deterministic tests."""
    override = os.environ.get("DREAMER_TODAY")
    if override:
        return _dt.date.fromisoformat(override)
    return _dt.date.today()


def as_date(value: Any) -> _dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return _dt.datetime.strptime(s[: len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def go_live_date() -> _dt.date | None:
    return as_date(CFG["decay"].get("go_live_date"))


def log(msg: str, *, job: str = "dreamer") -> None:
    """Diagnostics go to stderr, never stdout.

    Several scripts use stdout as a data channel (the wrappers redirect it into
    a .json file and parse it). Logging to stdout corrupts that payload and
    simultaneously hides the message from the job log, which is how a run can
    report 'done' while having silently produced nothing.
    """
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    print(f"[{stamp}] [{job}] {msg}", flush=True, file=sys.stderr)
