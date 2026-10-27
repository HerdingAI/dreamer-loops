#!/usr/bin/env python3
"""Owner-text reader.

Deterministic access to the owner's own words in a transcript, and to what the
assistant turns showed them. Roles, by heading:

  `## Human`                      -> "owner"                (verbatim words)
  `## Human (reconstructed)`      -> "owner-reconstructed"  (paraphrase: proves the
                                     topic was discussed, never a known known)
  `## Assistant` and `(reconstructed)` -> "derived"         (never the owner's words)

A turn runs from its exact heading to the next one; any other markdown heading
inside it is content. Pasted material (fenced blocks, `>`-quoted lines) is not
the owner's writing and is removed from owner text.
"""

from __future__ import annotations

import re
from pathlib import Path

from dreamer_common import CFG
from questions import normalize_quote

CUT_MARKER = "[... cut: long turn, middle omitted ...]"

_HEADINGS = {
    "## Human": "owner",
    "## Human (reconstructed)": "owner-reconstructed",
    "## Assistant": "derived",
    "## Assistant (reconstructed)": "derived",
}
_DIVIDER = "[NEW TURNS START HERE"
_FENCE = re.compile(r"^\s*(```|~~~)")
_WORD = re.compile(r"[a-z0-9']+")


def _cfg(key: str):
    return CFG["frontier"][key]


def _read(path: Path) -> list[str]:
    return Path(path).read_text(encoding="utf-8").splitlines()


def _split(lines: list[str]) -> list[dict]:
    """Turns as dicts: role, heading, start_line (1-based, the heading line),
    end_line, text (body without the heading)."""
    out: list[dict] = []
    cur: dict | None = None
    for i, line in enumerate(lines, 1):
        role = _HEADINGS.get(line.rstrip())
        if role:
            if cur:
                cur["end_line"] = i - 1
                out.append(cur)
            cur = {"role": role, "heading": line.rstrip(), "start_line": i,
                   "end_line": i, "_body": []}
        elif cur is not None:
            cur["_body"].append(line)
    if cur:
        cur["end_line"] = len(lines)
        out.append(cur)
    for t in out:
        t["text"] = "\n".join(t.pop("_body")).strip("\n")
    return out


def turns(path: Path) -> list[dict]:
    """Split a transcript into turns. A file with no role headings has none."""
    return _split(_read(path))


def _strip_pasted(text: str) -> str:
    """Drop fenced blocks (an unterminated fence runs to the end of the turn)
    and `>`-quoted lines."""
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _cap(text: str) -> str:
    limit = _cfg("owner_turn_max_chars")
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half].rstrip()}\n{CUT_MARKER}\n{text[-half:].lstrip()}"


def _after_divider(lines: list[str], ts: list[dict]) -> list[dict]:
    """Turns added after the `[NEW TURNS START HERE` divider make_batch/the
    converter writes. A divider inside a turn keeps that turn's tail. No
    divider means every turn is new."""
    div = next((i for i, l in enumerate(lines, 1) if _DIVIDER in l), None)
    if div is None:
        return ts
    out = []
    for t in ts:
        if t["start_line"] > div:
            out.append(t)
        elif t["end_line"] > div:
            tail = lines[div:t["end_line"]]
            out.append({**t, "text": "\n".join(tail).strip("\n")})
    return out


def owner_text(path: Path, new_turns_only: bool = False,
               include_reconstructed: bool = False, skip_turns: int = 0) -> str:
    """The owner's words, pasted material removed, long turns cut to head and
    tail. Reconstructed turns are excluded unless asked for. `skip_turns`
    drops that many leading turns of any role: a caller with its own boundary
    (the map ledger's turn count) uses it instead of the divider."""
    lines = _read(path)
    ts = _split(lines)[max(0, skip_turns):]
    if new_turns_only:
        ts = _after_divider(lines, ts)
    roles = {"owner"} | ({"owner-reconstructed"} if include_reconstructed else set())
    parts = [_cap(_strip_pasted(t["text"])) for t in ts if t["role"] in roles]
    return "\n\n".join(p for p in parts if p)


def find_owner_quote(path: Path, quote: str) -> dict | None:
    """The Human turn a quote sits in, or None.

    Returns {"turn": <turn text, pasted material removed>, "reconstructed": bool}.
    A verbatim `## Human` match wins over a reconstructed one; an assistant
    turn never matches. Matching ignores case, whitespace and curly quotes."""
    needle = normalize_quote(quote)
    if not needle:
        return None
    reconstructed_hit = None
    for t in turns(path):
        if t["role"] == "derived":
            continue
        # Match what owner_text() would show, not the uncapped turn: a long turn is cut to
        # head and tail precisely because a paste inside it can pass for the owner's words.
        body = _cap(_strip_pasted(t["text"]))
        if needle not in normalize_quote(body):
            continue
        if t["role"] == "owner":
            return {"turn": body, "reconstructed": False}
        reconstructed_hit = reconstructed_hit or {"turn": body, "reconstructed": True}
    return reconstructed_hit


def owner_phrase_lines(path: Path, patterns: list[re.Pattern]) -> dict[int, int]:
    """For each pattern found in the owner's words, the 1-based line it first matches
    on, keyed by the pattern's index. The owner's words are read as find_owner_quote
    verifies them (pasted material removed, long turns cut to head and tail), so a
    quote lifted from a match verifies. A match spanning lines falls back to the
    turn's first line."""
    src = _read(path)
    found: dict[int, int] = {}
    for t in _split(src):
        if t["role"] == "derived":
            continue
        body = _cap(_strip_pasted(t["text"]))
        for i, rx in enumerate(patterns):
            if i in found or not rx.search(body):
                continue
            # A line counts only if the owner's words hold it whole: not pasted, not cut.
            found[i] = next((n for n in range(t["start_line"] + 1, t["end_line"] + 1)
                             if rx.search(src[n - 1]) and src[n - 1].strip() in body),
                            t["start_line"] + 1)
    return found


def _paragraphs(lines: list[str], first: int, last: int) -> list[dict]:
    """Blank-line separated paragraphs within lines first..last (1-based)."""
    out: list[dict] = []
    buf: list[str] = []
    start = first
    for n in range(first, last + 1):
        if lines[n - 1].strip():
            if not buf:
                start = n
            buf.append(lines[n - 1])
        elif buf:
            out.append({"line": start, "end": n - 1, "text": "\n".join(buf)})
            buf = []
    if buf:
        out.append({"line": start, "end": last, "text": "\n".join(buf)})
    return out


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def passages(path: Path, lines: list[int], limit_chars: int,
             terms: list[str] | None = None) -> list[dict]:
    """Paragraphs around each qmd hit's matched line (1-based), labelled by turn.

    Returns a list, in file order, of
      {"line": first line of the paragraph, "role": "owner" |
       "owner-reconstructed" | "derived", "heading": the turn heading (or
       "" before the first turn), "text": the paragraph}.
    Budget: the paragraph holding each hit comes first (in hit order); what is
    left goes to neighbours within `frontier.passage_window_paragraphs`, those
    sharing the most words with `terms` first, nearest first on ties. A
    paragraph that does not fit whole is cut to the remaining budget, around
    where `terms` cluster (`_densest_at`), only when it is a hit paragraph;
    neighbours are skipped instead."""
    src = _read(path)
    ts = _split(src)
    window = _cfg("passage_window_paragraphs")
    wanted = {t.lower() for t in (terms or [])}
    weights = {s: 1 for s in _stems(" ".join(terms or []))}

    def locate(n: int) -> tuple[dict, list[dict], int] | None:
        """(turn-ish span, its paragraphs, index of the paragraph holding n)."""
        turn = next((t for t in ts if t["start_line"] <= n <= t["end_line"]), None)
        if turn is None:
            if ts and n >= ts[0]["start_line"]:
                return None
            span = {"role": "derived", "heading": "", "start_line": 1,
                    "end_line": ts[0]["start_line"] - 1 if ts else len(src)}
            first = 1
        else:
            span = turn
            first = turn["start_line"] + 1
        paras = _paragraphs(src, first, span["end_line"])
        if not paras:
            return None
        idx = next((i for i, q in enumerate(paras) if q["line"] <= n <= q["end"]), None)
        if idx is None:  # blank line or heading: nearest following paragraph
            idx = next((i for i, q in enumerate(paras) if q["line"] > n), len(paras) - 1)
        return span, paras, idx

    chosen: dict[tuple[int, int], dict] = {}
    neighbours: list[tuple[int, int, tuple[int, int], dict]] = []
    used = 0

    def label(span: dict, para: dict, text: str) -> dict:
        return {"line": para["line"], "role": span["role"],
                "heading": span["heading"], "text": text}

    located = []
    for n in lines:
        if not 1 <= n <= len(src):
            continue
        loc = locate(n)
        if loc:
            located.append(loc)
    for span, paras, idx in located:
        key = (span["start_line"], paras[idx]["line"])
        if key in chosen:
            continue
        room = limit_chars - used
        if room <= 0:
            break
        text = paras[idx]["text"]
        if len(text) > room:  # cut where the candidate's words cluster, marked as cut
            size = max(1, room - 2 * len(ELLIPSIS))
            text = _window(text, _densest_at(text, weights, size), size)
        chosen[key] = label(span, paras[idx], text)
        used += len(text)
    for span, paras, idx in located:
        for d in range(1, window + 1):
            for j in (idx - d, idx + d):
                if 0 <= j < len(paras):
                    key = (span["start_line"], paras[j]["line"])
                    if key in chosen or any(key == x[2] for x in neighbours):
                        continue
                    overlap = len(wanted & _tokens(paras[j]["text"]))
                    neighbours.append((-overlap, d, key, label(span, paras[j], paras[j]["text"])))
    for _, _, key, item in sorted(neighbours, key=lambda x: x[:2]):
        if used + len(item["text"]) <= limit_chars:
            chosen[key] = item
            used += len(item["text"])
    return sorted(chosen.values(), key=lambda p: p["line"])


# --------------------------------------------------------------------------
# Owner passages for the never-discussed check
# --------------------------------------------------------------------------

ELLIPSIS = "…"
_OWNER_ROLES = ("owner", "owner-reconstructed")


def _stems(text: str) -> dict[str, int]:
    """Content-word stems of `text` -> the offset of each stem's first word.
    A stem is a word's first `check_owner_stem_chars` letters, so "kiln" and
    "kilns" meet; stopwords and words shorter than `check_owner_min_word_chars`
    never count."""
    stop = set(_cfg("check_owner_stopwords"))
    size, shortest = int(_cfg("check_owner_stem_chars")), int(_cfg("check_owner_min_word_chars"))
    out: dict[str, int] = {}
    for m in _WORD.finditer(text.lower()):
        word = m.group(0).strip("'")
        if len(word) >= shortest and word not in stop:
            out.setdefault(word[:size], m.start())
    return out


def _densest_at(text: str, weights: dict[str, int], size: int) -> int:
    """The offset to centre a `size`-character window of `text` on: the occurrence
    of a weighted stem whose window holds the most weight in distinct stems
    (earliest on a tie). Centring on the first shared word alone cut off the
    owner's sentence on the concept when one of its words also came early."""
    stop = set(_cfg("check_owner_stopwords"))
    stem, shortest = int(_cfg("check_owner_stem_chars")), int(_cfg("check_owner_min_word_chars"))
    occ = []
    for m in _WORD.finditer(text.lower()):
        word = m.group(0).strip("'")
        if len(word) >= shortest and word not in stop and word[:stem] in weights:
            occ.append((m.start(), word[:stem]))
    best, best_at = -1, 0
    for at, _ in occ:
        lo = max(0, min(at - size // 2, len(text) - size))  # the window _window cuts
        score = sum(weights[s] for s in {s for o, s in occ if lo <= o < lo + size})
        if score > best:
            best, best_at = score, at
    return best_at


# owner_passages() / _owner_paragraphs() stay uncapped on purpose: they only feed the
# never-discussed judge, where extra owner text errs toward "discussed", the conservative side.
def _owner_paragraphs(src: list[str], turn: dict) -> list[dict]:
    """A turn's paragraphs with pasted material (fences, `>` quotes) blanked out,
    so line numbers stay true and pasted text never reads as the owner's."""
    lines = list(src)
    in_fence = False
    for n in range(turn["start_line"] + 1, turn["end_line"] + 1):
        line = lines[n - 1]
        if _FENCE.match(line):
            in_fence = not in_fence
            lines[n - 1] = ""
        elif in_fence or line.lstrip().startswith(">"):
            lines[n - 1] = ""
    return _paragraphs(lines, turn["start_line"] + 1, turn["end_line"])


def _window(text: str, at: int, size: int) -> str:
    """`text` cut to about `size` characters centred on offset `at`, on word
    boundaries, with an ellipsis where it was cut."""
    if len(text) <= size:
        return text
    start = max(0, min(at - size // 2, len(text) - size))
    end = start + size
    if start > 0:
        nxt = text.find(" ", start)
        start = nxt + 1 if 0 <= nxt < at else start
    if end < len(text):
        prev = text.rfind(" ", start, end)
        end = prev if prev > at else end
    return (ELLIPSIS if start > 0 else "") + text[start:end].strip() + (ELLIPSIS if end < len(text) else "")


def owner_passages(path: Path, lines: list[int], title: str, summary: str) -> list[dict]:
    """The owner's paragraphs in a chat that a hit list points near but passages()
    does not show: qmd's best line usually falls in a long assistant reply, and
    passages() stays inside that turn, so the check saw only derived text.

    Picks, in priority order, within `frontier.check_owner_chars`:
      1. for each hit in an assistant turn, the owner turn just before it (the
         question the reply answered): its paragraph sharing most words with the
         candidate, else its first;
      2. up to `check_owner_paragraphs` owner paragraphs anywhere in the chat that
         share content words with the candidate, scoring each shared title stem
         `check_owner_title_weight` and each other summary stem 1, kept only at
         `check_owner_min_score` or more, best first.
    A paragraph longer than `check_owner_paragraph_chars` is cut where its shared
    words cluster most (`_densest_at`). Returns the passages() shape, in file order."""
    src = _read(path)
    ts = _split(src)
    if not any(t["role"] in _OWNER_ROLES for t in ts):
        return []
    title_stems = _stems(title)
    summary_stems = {s for s in _stems(summary) if s not in title_stems}
    weight = int(_cfg("check_owner_title_weight"))
    size = int(_cfg("check_owner_paragraph_chars"))
    weights = {**{s: 1 for s in summary_stems}, **{s: weight for s in title_stems}}

    paras: list[tuple[int, dict, dict, int, int]] = []  # (turn index, turn, para, score, match offset)
    for ti, t in enumerate(ts):
        if t["role"] not in _OWNER_ROLES:
            continue
        for q in _owner_paragraphs(src, t):
            mine = _stems(q["text"])
            score = (weight * sum(1 for s in mine if s in title_stems)
                     + sum(1 for s in mine if s in summary_stems))
            at = _densest_at(q["text"], weights, size) if len(q["text"]) > size else 0
            paras.append((ti, t, q, score, at))

    picks: list[tuple[int, dict, dict, int, int]] = []
    for n in lines:
        ti = next((i for i, t in enumerate(ts) if t["start_line"] <= n <= t["end_line"]), None)
        if ti is None or ts[ti]["role"] in _OWNER_ROLES:
            continue
        before = next((i for i in range(ti - 1, -1, -1) if ts[i]["role"] in _OWNER_ROLES), None)
        own = [x for x in paras if x[0] == before]
        if own:
            picks.append(max(own, key=lambda x: (x[3], -x[2]["line"])))
    floor = int(_cfg("check_owner_min_score"))
    ranked = sorted((x for x in paras if x[3] >= floor), key=lambda x: (-x[3], x[2]["line"]))
    picks += ranked[:int(_cfg("check_owner_paragraphs"))]

    budget = int(_cfg("check_owner_chars"))
    chosen: dict[int, dict] = {}
    used = 0
    for _, t, q, _, at in picks:
        if q["line"] in chosen:
            continue
        text = _window(q["text"], at, size)
        if used + len(text) > budget:
            continue
        chosen[q["line"]] = {"line": q["line"], "role": t["role"], "heading": t["heading"], "text": text}
        used += len(text)
    return sorted(chosen.values(), key=lambda p: p["line"])
