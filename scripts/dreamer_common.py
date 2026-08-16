"""Shared helpers for Dreamer scripts (§6.1-6.9).

Deliberately dependency-light: stdlib + PyYAML only. Every module that
touches the vault goes through here so the write-safety rules (§6.3 rule 8)
are enforced in one place rather than re-implemented per script.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


CFG = load_config()


def p(key: str) -> Path:
    """Resolve a configured path by its key under `paths:`.

    Relative paths are anchored to the repo root (not the caller's cwd —
    cron invokes every job with cwd=$HOME) and `~` is expanded.
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


def read_json_strict(path: Path, default: Any = None) -> Any:
    """`default` covers a MISSING file only. An existing file that fails to
    parse raises — a read-merge-replace writer that fell back to `default`
    here would atomically replace the (recoverable) corrupt file with an
    empty state and silently wipe every cost, event, and deferral it held."""
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{path} exists but is not valid JSON ({exc}) — refusing to "
                f"treat it as empty; fix or move the file") from exc


# --------------------------------------------------------------------------
# Run-state writes (wiki-lock + read-merge-replace)
# --------------------------------------------------------------------------
# run-state.json has several writers: healthcheck.py, the bash record_*
# helpers in bin/_common.sh (via this module), and the watchdog. Atomic
# replace alone does not serialize read-merge-replace cycles — two writers
# reading the same snapshot means the later replace drops the earlier write
# (observed design flaw: ingest-cc.sh's record_* calls racing healthcheck's
# locked write). Every read-merge-replace therefore holds wiki.lock, the
# same advisory lock the bash jobs hold for their whole run.
#
# Re-entrancy: flock is per open-file-description, so a python child that
# re-flocks wiki.lock BLOCKS against its own parent shell's held lock (fd 9,
# bin/_common.sh acquire_lock). acquire_lock therefore exports
# DREAMER_LOCK_HELD=1, and state_lock skips its own flock when it is set —
# the parent's held lock already serializes everything run under it.


@contextlib.contextmanager
def state_lock(lockfile: Path, timeout_s: float = 6.0, poll_s: float = 0.2):
    """Advisory flock on wiki.lock: non-blocking with a short retry, matching
    scripts/healthcheck.py's vault_lock semantics (which delegates here). See
    the block comment above for the DREAMER_LOCK_HELD re-entrancy contract.
    Raises TimeoutError when the lock cannot be had inside `timeout_s`."""
    if os.environ.get("DREAMER_LOCK_HELD"):
        yield  # the calling job already holds wiki.lock on fd 9
        return
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lockfile, "a")
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire {lockfile} within {timeout_s}s "
                        f"(a job is running)")
                time.sleep(poll_s)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def append_event_deduped(state: dict, event: dict) -> bool:
    """Append `event` to state["events"] unless an identical
    (job, kind, detail) event is already pending. The digest that drains
    events is weekly while some writers fire nightly or per-entry, so an
    undeduped repeat failure stacks identical lines into one digest — the
    same contract healthcheck.write_health and the watchdog apply. Returns
    True if the event was actually appended."""
    seen = {(e.get("job"), e.get("kind"), e.get("detail"))
            for e in state.get("events") or []}
    key = (event.get("job"), event.get("kind"), event.get("detail"))
    if key in seen:
        return False
    state.setdefault("events", []).append(event)
    return True


def update_run_state(state_path: Path, mutate: Callable[[dict], Any]) -> dict:
    """Locked read-merge-atomic-replace on a run-state style JSON dict.

    The state is read INSIDE the locked section so a write that landed a
    moment earlier is merged, never clobbered. An existing-but-unparseable
    file raises (read_json_strict) rather than being wiped to {}."""
    with state_lock(state_path.parent / "wiki.lock"):
        state = read_json_strict(state_path, default={}) or {}
        mutate(state)
        atomic_write_json(state_path, state)
    return state


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


def hours_since(iso_ts) -> float | None:
    """Float hours elapsed since an ISO timestamp, or None when the value is
    missing or unparseable. Callers map None onto their own fallback (treat
    the record as stale, rescue nothing, ...) — this helper never guesses."""
    try:
        then = _dt.datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return None
    return (_dt.datetime.now() - then).total_seconds() / 3600.0


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
