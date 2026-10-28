#!/usr/bin/env python3
"""Deterministic web-egress gate.

`Gate.check(query, private_texts=...)` decides whether a research query may
leave the machine. It never rewrites a query: a failing query is dropped by the
caller and logged as blocked. Every failure to LOAD a list (blocked names, the
dictionary) fails closed.

This module stays stdlib-only at import time because scripts/egress_hook.py
imports it inside a PreToolUse hook, where an ImportError must not escape.
"""

from __future__ import annotations

import json
import os
import re
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Unicode letters, so an accented name is a token and not two ASCII fragments.
_TOKEN_RE = re.compile(r"[^\W\d_]+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_DIGITS_RE = re.compile(r"\d+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 7+ digits/separators starting and ending on a digit: phone numbers, with or
# without a country code or parentheses.
_PHONE_RE = re.compile(r"(?<![\w])\+?\(?\d[\d\s().-]{5,}\d(?![\w])")
_CURRENCY_RE = re.compile(
    r"[$€£¥]\s?\d"
    r"|\d[\d,.]*\s?(?:k|m|bn)?\s?(?:usd|eur|gbp|dollars?|euros?|pounds?|bucks)\b",
    re.IGNORECASE)


# An http(s) URL inside prose or serialised JSON: runs to whitespace, a quote, an
# angle bracket or a backslash (JSON escapes).
URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Comparison form for URL provenance: scheme and host lowercased, fragment
    dropped, trailing slash ignored. Returns "" for anything that is not http(s)
    with a host."""
    try:
        parts = urlsplit(url.strip())
        host = parts.netloc.lower()
        if parts.scheme.lower() not in ("http", "https") or not host:
            return ""
        path = parts.path.rstrip("/")
    except ValueError:
        return ""
    return f"{parts.scheme.lower()}://{host}{path}" + (f"?{parts.query}" if parts.query else "")


def normalize_query(query: str) -> str:
    """The one normalisation the approved-query writer and the hook share."""
    return " ".join(query.split()).casefold()


def write_approved(path: Path, queries: list[str]) -> None:
    """One normalised query per line, written atomically."""
    body = "".join(normalize_query(q) + "\n" for q in queries if q.strip())
    tmp = Path(f"{path}.tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_approved(path: str | Path) -> set[str]:
    return {normalize_query(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()}


def log_decision(path: str | Path, **fields) -> None:
    """Append one JSON line. Raises OSError if the log cannot be written: the
    caller decides what an unloggable egress means (the hook denies)."""
    entry = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), **fields}
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


@dataclass
class CheckResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


class Gate:
    def __init__(self, *, dictionary_path, blocked_names_path, allowed_proper_nouns,
                 phrase_run_words: int, year_min: int, year_max: int):
        self.allowed = {w.casefold() for w in allowed_proper_nouns}
        self.run_words = int(phrase_run_words)
        self.year_min, self.year_max = int(year_min), int(year_max)
        self.load_errors: list[str] = []
        self.blocked_terms: list[re.Pattern] = self._load_blocked(Path(blocked_names_path))
        self.dict_lower: set[str] = set()
        self.dict_caps: set[str] = set()
        self.dict_ok = self._load_dictionary(Path(dictionary_path))

    @classmethod
    def from_config(cls, cfg: dict) -> "Gate":
        e = cfg["frontier"]["egress"]
        return cls(dictionary_path=Path(e["dictionary_path"]).expanduser(),
                   blocked_names_path=Path(e["blocked_names_path"]).expanduser(),
                   allowed_proper_nouns=e["allowed_proper_nouns"] or [],
                   phrase_run_words=e["phrase_run_words"],
                   year_min=e["year_min"], year_max=e["year_max"])

    # -- loading (all fail closed) -----------------------------------------

    def _load_blocked(self, path: Path) -> list[re.Pattern]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self.load_errors.append(f"blocked-names file unreadable ({path}): {exc}")
            return []
        terms = [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if not terms:
            self.load_errors.append(f"blocked-names file has no terms ({path})")
        return [re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, t.split())) + r"(?!\w)",
                           re.IGNORECASE) for t in terms]

    def _load_dictionary(self, path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Queries are all-lowercase, so without the dictionary the lowercase-name
            # check would pass everything: block every query rather than weaken silently.
            self.load_errors.append(f"dictionary unreadable ({path}): {exc}")
            return False
        for word in text.split():
            if word[:1].isupper():
                self.dict_caps.add(word.casefold())
            else:
                self.dict_lower.add(word.casefold())
        if not self.dict_lower:
            self.load_errors.append(f"dictionary has no words ({path})")
        return bool(self.dict_lower)

    # -- the check ----------------------------------------------------------

    def _generic(self, token: str) -> bool:
        low = token.casefold()
        if low in self.allowed:
            return True
        # A dictionary entry never proves a capitalised token generic: only a
        # lowercase-listed word that has no capitalised listing counts.
        return self.dict_ok and low in self.dict_lower and low not in self.dict_caps

    def _run_set(self, text: str) -> set[tuple[str, ...]]:
        w = [x.casefold() for x in _WORD_RE.findall(text)]
        n = self.run_words
        return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}

    def check(self, query: str, private_texts: list[str] | None = None) -> CheckResult:
        reasons = list(self.load_errors)
        for pat in self.blocked_terms:
            if pat.search(query):
                reasons.append(f"blocked name: {pat.pattern}")
        for m in _TOKEN_RE.finditer(query):
            tok = m.group(0)
            if len(tok) < 2 or tok.casefold() in self.allowed:
                continue
            if not tok.isascii():
                reasons.append(f"non-ascii token: {tok}")
            elif tok[0].isupper():
                if not self._generic(tok):
                    reasons.append(f"capitalised token not known generic: {tok}")
            elif tok.casefold() in self.dict_caps and tok.casefold() not in self.dict_lower:
                # queries are written all lowercase, so a name hides there; a word the
                # dictionary lists only capitalised is a name. Absence from the dictionary
                # proves nothing (technical terms), so it is not blocked.
                reasons.append(f"capitalised in the dictionary, written lowercase: {tok}")
        if _EMAIL_RE.search(query):
            reasons.append("email pattern")
        if _PHONE_RE.search(query):
            reasons.append("phone pattern")
        if _CURRENCY_RE.search(query):
            reasons.append("currency amount")
        for m in _DIGITS_RE.finditer(query):
            run = m.group(0)
            if not (len(run) == 4 and self.year_min <= int(run) <= self.year_max):
                reasons.append(f"digit run: {run}")
        if private_texts:
            mine = self._run_set(query)
            if mine and any(mine & self._run_set(t) for t in private_texts):
                reasons.append(f"{self.run_words}-word run copied from the goal or evidence text")
        return CheckResult(ok=not reasons, reasons=reasons)

    def check_url(self, url: str, private_texts: list[str] | None = None) -> CheckResult:
        """Backstop for a URL the research call returned: refuses a
        blocked name, a `phrase_run_words`-word run copied from the private texts,
        or an email address in the percent-decoded URL. It is NOT a genericity
        test: digits, capitals and phone-like runs in ordinary article URLs pass."""
        reasons = list(self.load_errors)
        text = unquote(unquote(url))
        spaced = re.sub(r"[-_+/.=&?:]+", " ", text)  # slugs and query strings read as words
        for pat in self.blocked_terms:
            if pat.search(text) or pat.search(spaced):
                reasons.append(f"blocked name: {pat.pattern}")
        if _EMAIL_RE.search(text):
            reasons.append("email pattern")
        if private_texts:
            mine = self._run_set(spaced)
            if mine and any(mine & self._run_set(t) for t in private_texts):
                reasons.append(f"{self.run_words}-word run copied from the goal or evidence text")
        return CheckResult(ok=not reasons, reasons=reasons)
