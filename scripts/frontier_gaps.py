#!/usr/bin/env python3
"""Knowledge-frontier pre-mortem, never-discussed check and consequence judge.

Per active goal that is due (`due_goals`):
  1. premortem   a sealed call: "it is a year from now and the goal failed, why?"
                 -> ranked failure causes, each with the knowledge whose absence
                 produces it. Inputs: the goal page and map item titles only.
  2. check       per candidate, a sealed judge over passages retrieved by meaning
                 from frontier, transcripts, vault and conclusions, by the
                 title's words, and by the title's phrases in owner turns. Only a
                 verified owner quote makes a verdict `asked`/`explained`; a degraded
                 search or an unverifiable quote DEFERS the candidate, it is never an
                 unknown unknown.
  3. apply       under wiki.lock only: a matched item gets the evidence and the goal
                 relation; owner discussion lands where its evidence
                 derives; none creates an unknown-unknown item, origin premortem.
  4. consequence a sealed judge with a fixed rubric, ONLY for unknown-unknown and
                 unknown-known survivors. Tactical ones stay in the map and
                 are recorded, never queued. A verdict is cached per item until
                 the goal's plan_revision changes.
  5. record      vault/frontier/premortems/<goal>/<date>.md lists the plan revision,
                 the ranked causes and every candidate's verdicts, reason and item id.

A candidate that cannot be finished (degraded search, unverifiable quote, unparseable
reply, busy lock, usage limit) is recorded `deferred` and picked up by the next run
for that goal without a new pre-mortem. The caller holds frontier.lock.

`recheck <item>...` (operator, takes frontier.lock itself) runs the check again for
items a pre-mortem already made, after the check changes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, append_event_deduped, as_date, atomic_write_json, log, p, read_json,
    read_page, state_lock, today, write_page,
)
import frontier as F  # noqa: E402
import frontier_llm as L  # noqa: E402
import goals as G  # noqa: E402
import owner_text as OT  # noqa: E402
import qmd_client as QC  # noqa: E402

JOB = "frontier-gaps"
SURVIVORS = ("unknown_unknown", "unknown_known")
_ITEM_ID = re.compile(r"K\d{4,}")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_RECORD = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.md$")
_PLAN = re.compile(r"^## Plan \(revision \d+\)[ \t]*\n(.*?)(?=^## |\Z)", re.M | re.S)


def _cfg(key: str):
    return CFG["frontier"][key]


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).replace("|", "/")).strip()


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _event(kind: str, detail: str, at: str) -> None:
    """A run-state event, deduped. Never fatal: a busy vault only costs the event."""
    path = p("meta") / "run-state.json"
    event = {"at": at, "job": JOB, "kind": kind, "detail": detail}
    try:
        with state_lock(path.parent / "wiki.lock", timeout_s=float(_cfg("vault_lock_wait_seconds"))):
            state = read_json(path, default={}) or {}
            append_event_deduped(state, event)
            atomic_write_json(path, state)
    except (TimeoutError, OSError, ValueError) as exc:
        log(f"WARN could not record event ({detail}): {exc}", job=JOB)


# --------------------------------------------------------------------------
# Records: vault/frontier/premortems/<goal>/<date>[-n].md
# --------------------------------------------------------------------------

def records_dir(goal_id: str) -> Path:
    return F.frontier_dir() / _cfg("premortems_dir") / goal_id


def _s(v) -> str:
    return "" if v is None else (v.isoformat() if isinstance(v, (_dt.date, _dt.datetime)) else str(v))


def _read_record(path: Path) -> dict:
    fm, _ = read_page(path)
    cands = []
    for c in fm.get("candidates") or []:
        cands.append({
            "cause_rank": int(c.get("cause_rank") or 0), "title": _s(c.get("title")),
            "summary": _s(c.get("summary")), "status": _s(c.get("status")) or "done",
            "reason": _s(c.get("reason")), "stage": _s(c.get("stage")),
            "attempts": int(c.get("attempts") or 0), "check": _s(c.get("check")),
            "quote": _s(c.get("quote")), "transcript": _s(c.get("transcript")),
            "item": _s(c.get("item")), "quadrant": _s(c.get("quadrant")),
            "consequence": _s(c.get("consequence")),
            "consequence_reason": _s(c.get("consequence_reason")),
            "cached": bool(c.get("cached")), "exposed": [_s(x) for x in c.get("exposed") or []]})
    return {"goal": _s(fm.get("goal")), "date": _s(fm.get("date")),
            "plan_revision": int(fm.get("plan_revision") or 0),
            "causes": [{"rank": int(c.get("rank") or 0), "cause": _s(c.get("cause"))}
                       for c in fm.get("causes") or []],
            "candidates": cands, "_path": path}


def records(goal_id: str) -> list[dict]:
    """All records of a goal, oldest first."""
    d = records_dir(goal_id)
    found = []
    for path in d.glob("*.md") if d.exists() else []:
        m = _RECORD.match(path.name)
        if m:
            found.append(((m.group(1), int(m.group(2) or 0)), path))
    return [_read_record(path) for _, path in sorted(found)]


def latest_record(goal_id: str) -> dict | None:
    recs = records(goal_id)
    return recs[-1] if recs else None


def write_record(goal_id: str, rec: dict) -> Path:
    """Write `rec` (date, plan_revision, causes, candidates). A record that was read
    back (carries `_path`) is replaced in place; a new one never overwrites another."""
    path = rec.get("_path")
    if path is None:
        d = records_dir(goal_id)
        path = d / f"{rec['date']}.md"
        n = 1
        while path.exists():
            n += 1
            path = d / f"{rec['date']}-{n}.md"
    blank = _new_candidate(0, {"title": "", "summary": ""})
    cands = [{**blank, **c} for c in rec["candidates"]]
    rec = {**rec, "candidates": cands}
    fm = {"type": "frontier-premortem", "goal": goal_id, "date": rec["date"],
          "plan_revision": rec["plan_revision"], "causes": rec["causes"],
          "candidates": cands}
    lines = [f"# Pre-mortem {goal_id} {rec['date']} (plan revision {rec['plan_revision']})", "",
             "## Failure causes, most likely first", ""]
    lines += [f"{c['rank']}. {c['cause']}" for c in rec["causes"]] or ["(none)"]
    lines += ["", "## Candidates", ""]
    for c in rec["candidates"]:
        verdict = c["consequence"] or "not judged"
        item = c["item"] or "no item"
        lines.append(f"- [{c['status']}] {c['title']} (cause {c['cause_rank']}): "
                     f"check {c['check'] or '-'}, consequence {verdict}, {item}"
                     + (f" ({c['reason']})" if c["reason"] else "")
                     + (f". {c['consequence_reason']}" if c["consequence_reason"] else ""))
    write_page(path, fm, "\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# Due
# --------------------------------------------------------------------------

def due_goals(today: _dt.date | None = None) -> list["G.Goal"]:
    """Active goals due for a pre-mortem: no record, a record older than
    frontier.premortem_every_days, a goal created or re-planned since the record,
    or (a retry, not a new pre-mortem) a deferred candidate at the current revision."""
    ref = today or globals()["today"]()
    every = int(_cfg("premortem_every_days"))
    due = []
    for g in G.load_goals():
        if g.status != "active":
            continue
        rec = latest_record(g.id)
        if rec is None:
            due.append(g)
            continue
        rdate = as_date(rec["date"])
        if (rdate is None or (ref - rdate).days > every or g.plan_revision > rec["plan_revision"]
                or (g.created and rdate and g.created > rdate)
                or (g.plan_revision == rec["plan_revision"]
                    and any(c["status"] == "deferred" for c in rec["candidates"]))):
            due.append(g)
    return due


# --------------------------------------------------------------------------
# Prompt bodies
# --------------------------------------------------------------------------

def goal_block(goal: "G.Goal") -> str:
    """The goal page as the judges see it: title, finish line, why, target date and
    the current plan. The derived assessment and owner quotes are left out."""
    m = _PLAN.search(goal.body or "")
    plan = [f"  {l}" for l in (m.group(1).strip().splitlines() if m else []) if l.strip()]
    lines = ["## Goal", f"- id: {goal.id}", f"- title: {_one_line(goal.title)}",
             f"- finish line: {_one_line(goal.finish_line)}", f"- why: {_one_line(goal.why)}",
             f"- target date: {goal.target_date.isoformat() if goal.target_date else 'none'}",
             f"- plan (revision {goal.plan_revision}):"]
    return "\n".join(lines + (plan or ["  (no plan steps)"]))


def _premortem_body(goal: "G.Goal", map_items: list[dict]) -> str:
    lines = [goal_block(goal), "", "## Limits",
             f"- at most {_cfg('premortem_max_causes')} causes",
             f"- at most {_cfg('max_candidates_per_cause')} candidates per cause", "",
             "## Map: what Dreamer already holds about this goal (title | quadrant)", ""]
    lines += [f"- {_one_line(i['title'])} | {i['quadrant']}" for i in map_items] or ["(none)"]
    return "\n".join(lines)


def _map_items(goal: "G.Goal", qmd) -> list[dict] | None:
    """Items retrieved with the goal text, best first; None when retrieval is degraded."""
    items = F.items_dir()
    if not items.exists() or not any(items.glob("K*.md")):
        return []
    hits, degraded = qmd.query(f"{goal.title}\n{goal.finish_line}", "frontier",
                               limit=int(_cfg("premortem_map_items")))
    if degraded:
        return None
    out: list[dict] = []
    for h in hits:
        m = _ITEM_ID.search(Path(h["file"]).stem)
        path = F.item_path(m.group(0)) if m else None
        if not path or not path.exists() or any(o["id"] == m.group(0) for o in out):
            continue
        it = F.Item.from_path(path)
        out.append({"id": it.id, "title": it.title, "quadrant": it.quadrant})
    return out[:int(_cfg("premortem_map_items"))]


# --------------------------------------------------------------------------
# The never-discussed check
# --------------------------------------------------------------------------

def _inconclusive(reason: str, counted: bool = False) -> dict:
    """`counted`: the model's answer was unusable (it uses up an attempt, like an
    unparseable reply); otherwise the cause is transient and retries are free."""
    return {"status": "inconclusive", "reason": reason, "counted": counted}


def _transcript_rel(path: Path) -> str | None:
    """`sources/transcripts/<y>/<m>/<slug>` for a file under paths.sources, else None."""
    try:
        sub = path.resolve().relative_to(p("sources").resolve())
    except ValueError:
        return None
    return "sources/transcripts/" + str(sub.with_suffix(""))


def _transcript_date(path: Path) -> str:
    fm, _ = read_page(path)
    d = _s(fm.get("date"))
    if d:
        return d[:10]
    m = _DATE.search(path.name)
    return m.group(1) if m else ""


def _resolve(collection: str, file: str) -> Path | None:
    """The file behind a qmd hit, or None when it is gone. `file` is qmd's display
    path ("<collection>/<handelized path>"), so the real name is matched by
    qmd_client.resolve_display_path. The roots mirror .qmd/index.yml: the `vault`
    collection is vault/loops only (conclusions are kept out of it, rule 13)."""
    base = {"transcripts": p("sources"), "vault": p("loops"),
            "conclusions": p("conclusions")}.get(collection)
    if base is None:
        return None
    rest = file.split("/", 1)[1] if file.startswith(collection + "/") else file
    return QC.resolve_display_path(base, rest)


def _render_check(title: str, summary: str, entries: list[dict]) -> str:
    owner, derived, pages, items = [], [], [], []
    for e in entries:
        if e["kind"] == "item":
            items.append(f"- {e['id']} | {_one_line(e['title'])} | {e['quadrant']} | {_one_line(e['summary'])}")
            continue
        for q in e["passages"]:
            if not e["rel"]:  # a loop or conclusion: derived, and no chat, so never in exposed_in
                pages.append(f"=== PAGE {e['name']} line {q['line']} ===\n{q['text']}\n=== END ===")
                continue
            tag = {"owner": "OWNER", "owner-reconstructed": "OWNER (reconstructed)"}.get(q["role"], "DERIVED")
            block = f"=== {tag} {e['rel']} ({e['date']}) line {q['line']} ===\n{q['text']}\n=== END ==="
            (derived if tag == "DERIVED" else owner).append(block)
    out = ["## Candidate", f"- candidate title: {_one_line(title)}", f"- summary: {_one_line(summary)}", "",
           "## Owner passages (the owner's own words)", ""]
    out += owner or ["(none)"]
    out += ["", "## Assistant passages (derived: they can show exposure, never what the owner knows)", ""]
    out += derived or ["(none)"]
    out += ["", "## Dreamer pages (derived, not chats: never the owner's words, never in exposed_in)", ""]
    out += pages or ["(none)"]
    out += ["", "## Map items (derived)", ""]
    out += items or ["(none)"]
    return "\n".join(out)


def title_phrases(title: str) -> list[str]:
    """The title split at its joints (punctuation and check_phrase_split_words), each
    part trimmed of function words at its ends and kept when it holds at least
    check_phrase_min_words content words: lower-cased words joined by a space."""
    stop = set(_cfg("check_owner_stopwords"))
    joints = set(_cfg("check_phrase_split_words"))
    min_chars = int(_cfg("check_owner_min_word_chars"))

    def content(w: str) -> bool:
        return w not in stop and len(w) >= min_chars

    phrases: list[str] = []

    def close(part: list[str]) -> None:
        while part and not content(part[0]):
            part.pop(0)
        while part and not content(part[-1]):
            part.pop()
        if sum(map(content, part)) >= int(_cfg("check_phrase_min_words")):
            phrases.append(" ".join(part))

    for piece in re.split(r"[,;:/()\[\]]|\s[-–—]\s", title.lower()):
        part: list[str] = []
        for w in re.findall(r"[a-z0-9']+|&", piece):
            if w in joints:
                close(part)
                part = []
            else:
                part.append(w)
        close(part)
    return phrases


def _phrase_pattern(phrase: str) -> re.Pattern:
    """Each word as its stem (check_owner_stem_chars letters, after dropping a plural s)
    with any ending, the words apart by anything but letters and digits."""
    n = int(_cfg("check_owner_stem_chars"))
    stems = [re.escape((w[:-1] if len(w) > 3 and w.endswith("s") else w)[:n]) + r"\w*"
             for w in phrase.split()]
    return re.compile(r"\b" + r"[\W_]+".join(stems), re.I)


def _phrase_hits(title: str) -> list[tuple[Path, list[int]]]:
    """The chats where the owner said a phrase of the title, as (path, lines): the most
    phrases first, then the newest, at most check_phrase_max_chats."""
    patterns = [_phrase_pattern(ph) for ph in title_phrases(title)]
    base = p("sources")
    if not patterns or not base.exists():
        return []
    found = []
    for path in sorted(base.rglob("*.md")):
        lines = OT.owner_phrase_lines(path, patterns)
        if lines:
            found.append((len(lines), _transcript_date(path), path, sorted(set(lines.values()))))
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)  # stable: path order breaks ties
    return [(path, lines) for _, _, path, lines in found[:int(_cfg("check_phrase_max_chats"))]]


def check_candidate(title: str, summary: str, qmd) -> dict:
    """The never-discussed check for one candidate. `qmd` is an entered QmdClient.

    Returns {"status": "ok", owner_discussed, quote, item, transcript, date,
    exposed: [{"rel", "date"}]} or {"status": "inconclusive", "reason"}. Raises
    L.UsageLimit and L.ParseFailure for the caller to apply the defer policy."""
    query = f"{title}\n{summary}"
    top_k = int(_cfg("check_top_k"))
    title_k = int(_cfg("check_title_lex_k"))
    terms = re.findall(r"[A-Za-z0-9']+", title)
    found: list[tuple[float, str, dict]] = []
    titled: list[tuple[float, str, dict]] = []
    for coll in _cfg("check_collections"):
        hits, degraded = qmd.query(query, coll, limit=top_k)
        if degraded:
            return _inconclusive(f"retrieval degraded ({coll})")
        found += [(h["score"], coll, h) for h in hits]
        # The title's words alone, as keywords: qmd ANDs lex terms, so the
        # hybrid query's lex leg over title + summary rarely matches anything.
        hits, degraded = qmd.query(title, coll, limit=title_k, lex_only=True)
        if degraded:
            return _inconclusive(f"retrieval degraded ({coll} title search)")
        titled += [(h["score"], coll, h) for h in hits]
    found.sort(key=lambda t: -t[0])  # stable: collection order breaks ties
    titled.sort(key=lambda t: -t[0])
    # Appended, never fused: rank fusion let keyword hits push out the meaning
    # search's best chats. A chat both found keeps one entry (by_path below), and
    # the prompt cap drops title-only chats first.
    found += titled

    entries: list[dict] = []
    by_path: dict[Path, dict] = {}
    missing = 0
    for score, coll, h in found:
        if coll == "frontier":
            m = _ITEM_ID.search(Path(h["file"]).stem)
            path = F.item_path(m.group(0)) if m else None
            if not path or not path.exists() or any(e.get("id") == m.group(0) for e in entries):
                continue
            it = F.Item.from_path(path)
            entries.append({"kind": "item", "id": it.id, "title": it.title,
                            "quadrant": it.quadrant, "summary": it.summary})
            continue
        path = _resolve(coll, h["file"])
        if path is None:
            log(f"check: hit {h['file']!r} ({coll}) is not on disk; skipped", job=JOB)
            missing += 1
            continue
        if path in by_path:
            by_path[path]["lines"].append(h["line"])
            continue
        rel = _transcript_rel(path)
        e = {"kind": "file", "path": path, "rel": rel, "name": f"{coll}:{h['file']}",
             "date": _transcript_date(path) if rel else "", "lines": [h["line"]]}
        by_path[path] = e
        entries.append(e)
    if missing and not by_path:
        # Search found text but none of it is readable: the index is out of step
        # with the disk. Judging on map items alone would read as "never discussed"
        # and pass the candidate as new, so this is retrieval failing, not an answer.
        return _inconclusive(f"retrieval degraded: {missing} search hit(s) not on disk")
    # The owner's own phrases, appended last like the title search. A title joining two
    # ideas ANDs both halves in that search, so a chat where the owner said one half in
    # passing was never shown and the candidate passed as never discussed.
    for path, lines in _phrase_hits(title):
        if path in by_path:
            by_path[path]["lines"] += lines
            continue
        e = {"kind": "file", "path": path, "rel": _transcript_rel(path), "name": f"phrase:{path.name}",
             "date": _transcript_date(path), "lines": lines}
        by_path[path] = e
        entries.append(e)
    limit = int(_cfg("check_passage_chars"))
    for e in entries:
        if e["kind"] == "file":
            ps = OT.passages(e["path"], e["lines"], limit, terms=terms)
            if not e["rel"]:  # not a chat: every line of it is derived text (rule 13)
                ps = [{**q, "role": "derived"} for q in ps]
            else:  # the owner's own words in the same chat, which the hit lines rarely land on
                seen = {q["line"] for q in ps}
                ps = sorted(ps + [q for q in OT.owner_passages(e["path"], e["lines"], title, summary)
                                  if q["line"] not in seen], key=lambda q: q["line"])
            e["passages"] = ps
    entries = [e for e in entries if e["kind"] == "item" or e["passages"]]
    if not entries:
        return {"status": "ok", "owner_discussed": "none", "quote": "", "item": None,
                "transcript": "", "date": "", "exposed": []}

    cap = int(_cfg("prompt_max_chars"))
    entries = L.trim_to_cap(
        lambda kept: L.render_prompt("check", _render_check(title, summary, kept)), entries, cap)
    shown_items = {e["id"] for e in entries if e["kind"] == "item"}
    shown = {e["rel"]: e for e in entries if e["kind"] == "file" and e["rel"]}
    reply = L.call_json("check", _render_check(title, summary, entries),
                        lambda t: L.parse_check(t, shown_items, set(shown)))

    verdict, quote = reply["owner_discussed"], reply["quote"]
    exposed = [{"rel": r, "date": shown[r]["date"]} for r in reply["exposed_in"]]
    out = {"status": "ok", "owner_discussed": verdict, "quote": quote, "item": reply["matches_item"],
           "transcript": "", "date": "", "exposed": exposed}
    if verdict == "none":
        return out
    # An owner verdict stands only on a quote found verbatim in an owner turn of a
    # transcript whose owner passages were shown (assistant text never counts).
    owner_files = [e for e in shown.values()
                   if any(q["role"] in ("owner", "owner-reconstructed") for q in e["passages"])]
    for e in owner_files:
        hit = OT.find_owner_quote(e["path"], quote)
        if hit is None:
            continue
        if hit["reconstructed"] != (verdict == "reconstructed"):
            return _inconclusive(f"quote is {'reconstructed' if hit['reconstructed'] else 'verbatim'} "
                                 f"but the verdict is {verdict}", counted=True)
        out.update(transcript=e["rel"], date=e["date"])
        return out
    return _inconclusive("quote not found in an owner turn", counted=True)


# --------------------------------------------------------------------------
# Consequence
# --------------------------------------------------------------------------

def judge_consequence(goal: "G.Goal", cause: str, title: str, summary: str) -> dict:
    body = "\n".join([goal_block(goal), "", "## Failure cause", _one_line(cause), "",
                      "## Candidate", f"- candidate: {_one_line(title)}",
                      f"- summary: {_one_line(summary)}"])
    return L.call_json("consequence", body, L.parse_consequence)


# --------------------------------------------------------------------------
# Apply (the caller holds wiki.lock)
# --------------------------------------------------------------------------

def _apply(goal: "G.Goal", cand: dict, res: dict, date: str, at: str,
           cause: str = "") -> "F.Item":
    pages = {path.stem: path for path in sorted(F.items_dir().glob("K*.md"))} \
        if F.items_dir().exists() else {}
    target = res["item"] if res["item"] in pages else None
    if target is None:  # an index that has not caught up must not breed duplicates
        want = _norm_title(cand["title"])
        target = next((i for i, path in pages.items()
                       if _norm_title(F.Item.from_path(path).title) == want), None)
    if target:
        item = F.Item.from_path(pages[target])
    else:
        item = F.Item(id=F.next_item_id(), title=cand["title"], summary=cand["summary"],
                      origin="premortem")
    if res["owner_discussed"] != "none":
        item.add_evidence(res["owner_discussed"], res["transcript"], res["date"], res["quote"])
    for e in res["exposed"]:
        if e["rel"] != res["transcript"]:
            item.add_evidence("exposure", e["rel"], e["date"], "")
    # No relation to this goal yet means the owner never tied it to the goal.
    if not any(g["goal"] == goal.id for g in item.goals):
        item.set_goal(goal.id, "unconnected")
    F.apply_quadrant(item, cause or f"premortem {goal.id} {date}", F.active_goal_ids(), at=at)
    item.save()
    return item


# --------------------------------------------------------------------------
# One goal
# --------------------------------------------------------------------------

def _new_candidate(rank: int, c: dict) -> dict:
    return {"cause_rank": rank, "title": _one_line(c["title"]), "summary": _one_line(c["summary"]),
            "status": "deferred", "reason": "", "stage": "check", "attempts": 0, "check": "",
            "quote": "", "transcript": "", "item": "", "quadrant": "", "consequence": "",
            "consequence_reason": "", "cached": False, "exposed": []}


def _defer(c: dict, reason: str, stage: str) -> None:
    c.update(status="deferred", reason=reason, stage=stage)


def _attempt_failed(c: dict, stage: str, reason: str, detail: str, goal_id: str, at: str) -> None:
    """A model answer that could not be used: defer, or skip once attempts run out."""
    c["attempts"] += 1
    if c["attempts"] >= int(_cfg("max_parse_attempts")):
        c.update(status="skipped", reason=reason, stage=stage)
        _event("degraded", f"frontier gaps: skipped candidate {c['title']!r} of {goal_id} after "
                           f"{c['attempts']} unusable {stage} replies ({detail[:120]})", at)
    else:
        _defer(c, reason, stage)
    log(f"{stage} reply unusable for {c['title']!r}: {detail[:120]}", job=JOB)


def _parse_failed(c: dict, stage: str, exc: "L.ParseFailure", goal_id: str, at: str) -> None:
    _attempt_failed(c, stage, "parse", exc.reason, goal_id, at)


def _process(goal: "G.Goal", cause: str, c: dict, qmd, cache: dict, date: str, at: str) -> None:
    """Finish one candidate as far as it can go. Raises L.UsageLimit."""
    if c["stage"] != "consequence":
        try:
            res = check_candidate(c["title"], c["summary"], qmd)
        except L.ParseFailure as exc:
            return _parse_failed(c, "check", exc, goal.id, at)
        except L.PromptTooLarge as exc:
            return _defer(c, f"prompt too large: {exc}", "check")
        if res["status"] != "ok":
            if res["counted"]:
                return _attempt_failed(c, "check", res["reason"], res["reason"], goal.id, at)
            return _defer(c, res["reason"], "check")
        try:
            with state_lock(p("meta") / "wiki.lock", timeout_s=float(_cfg("vault_lock_wait_seconds"))):
                item = _apply(goal, c, res, date, at)
        except TimeoutError:
            return _defer(c, "wiki.lock busy", "check")
        c.update(check=res["owner_discussed"], quote=res["quote"], transcript=res["transcript"],
                 item=item.id, quadrant=item.quadrant, exposed=[e["rel"] for e in res["exposed"]],
                 attempts=0)
        if item.quadrant not in SURVIVORS:
            c.update(status="done", reason="", stage="")
            return
        c["stage"] = "consequence"
    cached = cache.get(c["item"])
    if cached:
        c.update(consequence=cached[0], consequence_reason=cached[1], cached=True,
                 status="done", reason="", stage="")
        return
    try:
        verdict = judge_consequence(goal, cause, c["title"], c["summary"])
    except L.ParseFailure as exc:
        return _parse_failed(c, "consequence", exc, goal.id, at)
    except L.PromptTooLarge as exc:
        return _defer(c, f"prompt too large: {exc}", "consequence")
    c.update(consequence=verdict["verdict"], consequence_reason=_one_line(verdict["reason"]),
             cached=False, status="done", reason="", stage="")
    cache[c["item"]] = (c["consequence"], c["consequence_reason"])


def run_goal(goal: "G.Goal", qmd_factory=None, at: str | None = None) -> dict:
    """Pre-mortem one goal (or finish its deferred candidates) and write the record.
    Status: done, stopped (usage limit), deferred (map retrieval degraded), failed
    (unparseable pre-mortem), skipped (the goal is no longer active)."""
    if qmd_factory is None:
        from qmd_client import QmdClient
        qmd_factory = QmdClient
    now = at or _dt.datetime.now().isoformat(timespec="seconds")
    date = now[:10]
    goal = G.load_goal(goal.id)
    if goal is None or goal.status != "active":
        return {"status": "skipped", "reason": "goal is not active"}  # a paused or finished goal is not pre-mortemed

    prior = [r for r in records(goal.id) if r["plan_revision"] == goal.plan_revision]
    last = latest_record(goal.id)
    resume = last if (last and last["plan_revision"] == goal.plan_revision
                      and any(c["status"] == "deferred" for c in last["candidates"])) else None
    # Verdicts are cached per item until the plan revision changes.
    cache = {c["item"]: (c["consequence"], c["consequence_reason"])
             for r in prior for c in r["candidates"] if c["item"] and c["consequence"]}

    with qmd_factory() as qmd:
        if resume:
            rec = resume
        else:
            try:
                map_items = _map_items(goal, qmd)
            except OSError as exc:
                log(f"retrieval unavailable: {exc}", job=JOB)
                map_items = None
            if map_items is None:
                _event("degraded", f"frontier gaps: map retrieval degraded; pre-mortem for {goal.id} deferred", now)
                return {"status": "deferred", "reason": "retrieval degraded"}
            cap = int(_cfg("prompt_max_chars"))
            kept = L.trim_to_cap(
                lambda k: L.render_prompt("premortem", _premortem_body(goal, k)), map_items, cap)
            try:
                causes = L.call_json(
                    "premortem", _premortem_body(goal, kept),
                    lambda t: L.parse_premortem(t, int(_cfg("max_candidates_per_cause")),
                                                int(_cfg("premortem_max_causes"))))
            except L.UsageLimit as exc:
                log(f"premortem: {exc}; stopping, nothing recorded", job=JOB)
                return {"status": "stopped", "reason": str(exc)}
            except (L.ParseFailure, L.PromptTooLarge) as exc:
                _event("degraded", f"frontier gaps: pre-mortem for {goal.id} unusable "
                                   f"({getattr(exc, 'reason', str(exc))[:120]}); retried next run", now)
                return {"status": "failed", "reason": str(exc)[:200]}
            rec = {"date": date, "plan_revision": goal.plan_revision,
                   "causes": [{"rank": i, "cause": _one_line(c["cause"])}
                              for i, c in enumerate(causes, 1)],
                   "candidates": [_new_candidate(i, k) for i, c in enumerate(causes, 1)
                                  for k in c["candidates"]]}

        cause_of = {c["rank"]: c["cause"] for c in rec["causes"]}
        status = "done"
        for n, c in enumerate(rec["candidates"]):
            if c["status"] != "deferred":
                continue
            try:
                _process(goal, cause_of.get(c["cause_rank"], ""), c, qmd, cache, date, now)
            except L.UsageLimit as exc:
                log(f"{exc}; stopping with the rest of {goal.id} deferred", job=JOB)
                for rest in rec["candidates"][n:]:
                    if rest["status"] == "deferred":
                        rest["reason"] = "usage limit"
                status = "stopped"
                break
    # The record is a new file under this goal's own directory, written atomically by
    # the caller that holds frontier.lock; it does not need wiki.lock.
    path = write_record(goal.id, rec)
    counts = {s: sum(1 for c in rec["candidates"] if c["status"] == s)
              for s in ("done", "deferred", "skipped")}
    return {"status": status, "record": str(path), "resumed": bool(resume), **counts}


def run(qmd_factory=None, at: str | None = None) -> dict:
    """Run every due goal, stopping at the first usage limit."""
    ref = _dt.date.fromisoformat(at[:10]) if at else today()
    results = []
    for g in due_goals(ref):
        r = run_goal(g, qmd_factory=qmd_factory, at=at)
        results.append({"goal": g.id, **r})
        if r["status"] == "stopped":
            break
    return {"goals": results}


def _made_by(item_id: str) -> tuple["G.Goal", dict, dict] | None:
    """The active goal, its latest record and the finished candidate that made `item_id`."""
    for g in G.load_goals():
        rec = latest_record(g.id) if g.status == "active" else None
        for c in rec["candidates"] if rec else []:
            if c["item"] == item_id and c["status"] == "done":
                return g, rec, c
    return None


def recheck(item_ids: list[str], qmd_factory=None, at: str | None = None) -> list[dict]:
    """The never-discussed check again for items a pre-mortem made, once the check itself
    has changed. Owner discussion found now becomes evidence on the
    SAME item, even when the judge names another (no merge), which moves by the cause
    `recheck <goal> <date>`, and the record's candidate is updated. A check that finds
    nothing, or cannot finish, writes nothing; a usage limit stops at that item and
    reports it and every one after it as `stopped`."""
    if qmd_factory is None:
        from qmd_client import QmdClient
        qmd_factory = QmdClient
    now = at or _dt.datetime.now().isoformat(timespec="seconds")
    date = now[:10]
    out: list[dict] = []
    with qmd_factory() as qmd:
        for n, iid in enumerate(item_ids):
            made = _made_by(iid)
            if made is None:
                out.append({"item": iid, "status": "skipped",
                            "reason": "no finished pre-mortem candidate of an active goal made it"})
                continue
            goal, rec, c = made
            try:
                res = check_candidate(c["title"], c["summary"], qmd)
            except L.UsageLimit as exc:
                out += [{"item": i, "status": "stopped", "reason": f"usage limit: {exc}"}
                        for i in item_ids[n:]]
                break
            except (L.ParseFailure, L.PromptTooLarge) as exc:
                res = _inconclusive(str(exc))
            if res["status"] != "ok":
                out.append({"item": iid, "goal": goal.id, "status": "deferred", "reason": res["reason"]})
                continue
            before = F.Item.from_path(F.item_path(iid)).quadrant
            entry = {"item": iid, "goal": goal.id, "check": res["owner_discussed"], "from": before}
            if res["owner_discussed"] == "none":
                out.append({**entry, "status": "unchanged", "to": before, "matched": res["item"]})
                continue
            try:
                with state_lock(p("meta") / "wiki.lock", timeout_s=float(_cfg("vault_lock_wait_seconds"))):
                    item = _apply(goal, c, {**res, "item": iid}, date, now, cause=f"recheck {goal.id} {date}")
            except TimeoutError:
                out.append({"item": iid, "goal": goal.id, "status": "deferred", "reason": "wiki.lock busy"})
                continue
            c.update(check=res["owner_discussed"], quote=res["quote"], transcript=res["transcript"],
                     quadrant=item.quadrant, exposed=[e["rel"] for e in res["exposed"]])
            write_record(goal.id, rec)
            out.append({**entry, "status": "moved" if item.quadrant != before else "unchanged",
                        "to": item.quadrant, "matched": res["item"]})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Knowledge frontier pre-mortem")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("due", help="ids of the goals due for a pre-mortem")
    sub.add_parser("run", help="pre-mortem every due goal; prints a JSON status")
    rc = sub.add_parser("recheck", help="the never-discussed check again for pre-mortem items, "
                                        "under frontier.lock; prints a JSON status")
    rc.add_argument("items", nargs="+", help="map item ids, e.g. K2001")
    args = ap.parse_args(argv)
    if args.cmd == "due":
        print(json.dumps([g.id for g in due_goals()]))
    elif args.cmd == "recheck":
        # An operator command outside bin/frontier.sh, so it takes the run's lock itself.
        lock = p("meta") / _cfg("lock_file")
        try:
            with state_lock(lock, timeout_s=0):
                print(json.dumps({"rechecked": recheck(args.items)}))
        except TimeoutError:
            print(json.dumps({"error": f"{lock.name} is held by a frontier run; try again later"}))
            return 1
    else:
        print(json.dumps(run()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
