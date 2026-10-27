#!/usr/bin/env python3
"""Model calls for the knowledge frontier.

Every frontier call has a prompt with a unique first-line marker, an input
contract, an output schema and a strict parser. A reply is accepted only when
it is exactly the schema's JSON, optionally inside ONE ```json fence: prose
around it, an empty reply, a tool call written as text (what a sealed model
does when it "wants" a tool), missing or extra keys, unknown enum values and
ids that name nothing are all a ParseFailure.

Failure policy (the callers apply it, this module raises):
  UsageLimit    a nonzero exit from run_claude (rule 9): stop the run, leave the
                ledger unmarked. Never retried.
  ParseFailure  after `frontier.parse_retries` retries: the caller defers the
                item as deferred(parse) and counts the attempt.

Later units add their calls to CALLS with their own parsers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, ROOT, atomic_write, log, p  # noqa: E402


@dataclass(frozen=True)
class CallSpec:
    marker: str      # exact first line of the prompt
    prompt: str      # path of the PROMPT.md under the repo root
    turns_key: str   # budget.<key>: max turns (sealed calls clamp at 4)
    mode: str = "restricted"   # run_claude tool mode: restricted (sealed) or gated (WebSearch only)


CALLS: dict[str, CallSpec] = {
    "extract": CallSpec("# Frontier extract", "skills/frontier-extract/PROMPT.md",
                        "max_turns_frontier_extract"),
    "match": CallSpec("# Frontier match", "skills/frontier-match/PROMPT.md",
                      "max_turns_frontier_match"),
    "premortem": CallSpec("# Frontier premortem", "skills/frontier-premortem/PROMPT.md",
                          "max_turns_frontier_premortem"),
    "check": CallSpec("# Frontier check", "skills/frontier-check/PROMPT.md",
                      "max_turns_frontier_check"),
    "consequence": CallSpec("# Frontier consequence", "skills/frontier-consequence/PROMPT.md",
                            "max_turns_frontier_consequence"),
    # Lessons. research is the one GATED call: WebSearch behind the egress hook.
    "queries": CallSpec("# Frontier queries", "skills/frontier-queries/PROMPT.md",
                        "max_turns_frontier_queries"),
    "research": CallSpec("# Frontier research", "skills/frontier-research/PROMPT.md",
                         "max_turns_frontier_research", mode="gated"),
    "claims": CallSpec("# Frontier claims", "skills/frontier-claims/PROMPT.md",
                       "max_turns_frontier_claims"),
    "compose": CallSpec("# Frontier compose", "skills/frontier-compose/PROMPT.md",
                        "max_turns_frontier_compose"),
    "lesson-check": CallSpec("# Frontier lesson-check", "skills/frontier-lesson-check/PROMPT.md",
                             "max_turns_frontier_lesson_check"),
}

EXTRACT_KINDS = ("explained", "decided", "applied", "asked")
RELATIONS = ("connected", "unconnected")
MATCH_DECISIONS = ("attach", "new", "batch_ref")
OWNER_DISCUSSED = ("none", "asked", "explained", "reconstructed")
CONSEQUENCE_VERDICTS = ("consequential", "tactical")
CLAIM_KINDS = ("fact", "action")
CHECK_VERDICTS = ("pass", "reject")
# The lesson reject list, as the lesson check names what failed.
CHECK_FAILS = ("uncited", "instruction", "not_tied", "padding")

RETRY_NOTE = ("\n\nYour previous reply could not be used: it was not exactly the JSON "
              "object described in the output contract. Reply again with only that JSON "
              "object: no prose, no fences, no tool calls.\n")


class UsageLimit(RuntimeError):
    """run_claude exited nonzero: a usage limit or other deferral (rule 9)."""

    def __init__(self, rc: int):
        super().__init__(f"claude exited {rc}")
        self.rc = rc


class ParseFailure(ValueError):
    def __init__(self, reason: str, attempts: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class PromptTooLarge(ValueError):
    pass


def _cfg(key: str):
    return CFG["frontier"][key]


# --------------------------------------------------------------------------
# Prompts and the bash call
# --------------------------------------------------------------------------

def render_prompt(name: str, body: str) -> str:
    spec = CALLS[name]
    template = (ROOT / spec.prompt).read_text(encoding="utf-8").rstrip()
    if template.split("\n", 1)[0].strip() != spec.marker:
        raise ValueError(f"{spec.prompt}: first line is not the marker {spec.marker!r}")
    return f"{template}\n\n{body.rstrip()}\n"


def run_model(name: str, prompt: str, env: dict | None = None) -> str:
    """One call through bin/_common.sh run_claude, in the call's tool mode (sealed
    `restricted`, or `gated` for research, which needs `env` to carry
    DREAMER_EGRESS_APPROVED and DREAMER_EGRESS_LOG). Raises UsageLimit on a
    nonzero exit. Returns the reply text (possibly empty)."""
    spec = CALLS[name]
    logs = p("logs")
    logs.mkdir(parents=True, exist_ok=True)
    prompt_file = logs / f".prompt-frontier-{name}.md"
    out_file = logs / f".result-frontier-{name}.json"
    out_file.unlink(missing_ok=True)
    atomic_write(prompt_file, prompt)
    turns = str(int(CFG["budget"][spec.turns_key]))
    # META is pinned to the configured meta dir: run_claude records a deferral
    # there, and it must be the same vault this process is working on.
    script = ('source "$FRONTIER_ROOT/bin/_common.sh"; META="$FRONTIER_META"; '
              'run_claude "$1" "$2" "$3" "$4"')
    env = {**os.environ, "FRONTIER_ROOT": str(ROOT),
           "FRONTIER_META": str(p("meta")), "JOB": f"frontier-{name}", **(env or {})}
    proc = subprocess.run(["bash", "-c", script, "bash", str(prompt_file), turns, str(out_file), spec.mode],
                          env=env, stdout=subprocess.PIPE, text=True, check=False)
    if proc.stdout:  # run_claude's log lines: keep stdout clean for the callers' JSON
        print(proc.stdout, end="", file=sys.stderr)
    if proc.returncode != 0:
        raise UsageLimit(proc.returncode)
    try:
        return out_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def call_json(name: str, body: str, parse: Callable[[str], object],
              runner: Callable[[str], str] | None = None):
    """Render, run and parse one call, retrying a malformed reply."""
    prompt = render_prompt(name, body)
    cap = int(_cfg("prompt_max_chars"))
    if len(prompt) > cap:
        raise PromptTooLarge(f"{name}: prompt is {len(prompt)} chars, frontier.prompt_max_chars is {cap}")
    run = runner or (lambda pr: run_model(name, pr))
    attempts = 1 + int(_cfg("parse_retries"))
    reason = ""
    for n in range(1, attempts + 1):
        text = run(prompt if n == 1 else prompt + RETRY_NOTE)
        try:
            return parse(text)
        except ParseFailure as exc:
            reason = exc.reason
            log(f"{name}: unusable reply (attempt {n}/{attempts}): {reason}", job="frontier")
    raise ParseFailure(reason, attempts=attempts)


def trim_to_cap(render: Callable[[list], str], ranked: list, cap: int) -> list:
    """Drop the lowest-ranked entries (the end of `ranked`) until render(kept)
    fits `cap` characters. Returns the entries kept."""
    kept = list(ranked)
    while kept and len(render(kept)) > cap:
        kept.pop()
    return kept


# --------------------------------------------------------------------------
# Strict parsing
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*)\n```", re.S)


def load_json_object(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        raise ParseFailure("empty reply")
    m = _FENCE.fullmatch(t)
    if m:
        t = m.group(1).strip()
    try:
        obj = json.loads(t)
    except ValueError as exc:
        raise ParseFailure(f"not JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise ParseFailure("reply is not a JSON object")
    return obj


def _exact(obj: dict, keys: set[str], where: str) -> None:
    if set(obj) != keys:
        missing, extra = sorted(keys - set(obj)), sorted(set(obj) - keys)
        raise ParseFailure(f"{where}: missing {missing} extra {extra}")


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ParseFailure(f"{where}: expected a non-empty string")
    return value.strip()


def _int(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParseFailure(f"{where}: expected an integer")
    return value


def parse_extract(text: str, transcripts: set[str], goals: set[str]) -> list[dict]:
    """The extract reply: {"concepts": [{title, summary, kind, quote, transcript,
    goals: [{goal, relation}]}]}. `transcripts` and `goals` are the ids the prompt named."""
    obj = load_json_object(text)
    _exact(obj, {"concepts"}, "reply")
    if not isinstance(obj["concepts"], list):
        raise ParseFailure("concepts is not a list")
    out = []
    for i, c in enumerate(obj["concepts"]):
        where = f"concepts[{i}]"
        if not isinstance(c, dict):
            raise ParseFailure(f"{where}: not an object")
        _exact(c, {"title", "summary", "kind", "quote", "transcript", "goals"}, where)
        concept = {k: _text(c[k], f"{where}.{k}") for k in ("title", "summary", "quote")}
        if c["kind"] not in EXTRACT_KINDS:
            raise ParseFailure(f"{where}.kind: {c['kind']!r} is not one of {EXTRACT_KINDS}")
        if c["transcript"] not in transcripts:
            raise ParseFailure(f"{where}.transcript: {c['transcript']!r} names no transcript in the batch")
        if not isinstance(c["goals"], list):
            raise ParseFailure(f"{where}.goals: not a list")
        gl = []
        for j, g in enumerate(c["goals"]):
            gw = f"{where}.goals[{j}]"
            if not isinstance(g, dict):
                raise ParseFailure(f"{gw}: not an object")
            _exact(g, {"goal", "relation"}, gw)
            if g["goal"] not in goals:
                raise ParseFailure(f"{gw}.goal: {g['goal']!r} is not an active goal")
            if g["relation"] not in RELATIONS:
                raise ParseFailure(f"{gw}.relation: {g['relation']!r} is not one of {RELATIONS}")
            gl.append({"goal": g["goal"], "relation": g["relation"]})
        concept.update(kind=c["kind"], transcript=c["transcript"], goals=gl)
        out.append(concept)
    return out


def parse_match(text: str, neighbours: dict[int, set[str]]) -> dict[int, dict]:
    """The match reply: {"decisions": [{concept, decision, item, batch_ref,
    justification}]}, one per key of `neighbours` (concept number -> the item ids
    listed for it). Returns {concept: decision}."""
    obj = load_json_object(text)
    _exact(obj, {"decisions"}, "reply")
    if not isinstance(obj["decisions"], list):
        raise ParseFailure("decisions is not a list")
    out: dict[int, dict] = {}
    for i, d in enumerate(obj["decisions"]):
        where = f"decisions[{i}]"
        if not isinstance(d, dict):
            raise ParseFailure(f"{where}: not an object")
        _exact(d, {"concept", "decision", "item", "batch_ref", "justification"}, where)
        idx = _int(d["concept"], f"{where}.concept")
        if idx not in neighbours:
            raise ParseFailure(f"{where}.concept: {idx} names no concept to decide")
        if idx in out:
            raise ParseFailure(f"{where}.concept: {idx} decided twice")
        kind = d["decision"]
        if kind not in MATCH_DECISIONS:
            raise ParseFailure(f"{where}.decision: {kind!r} is not one of {MATCH_DECISIONS}")
        item, ref = d["item"], d["batch_ref"]
        if kind == "attach":
            if item not in neighbours[idx] or ref is not None:
                raise ParseFailure(f"{where}: attach needs an item among concept {idx}'s neighbours and no batch_ref")
        elif kind == "batch_ref":
            if item is not None or _int(ref, f"{where}.batch_ref") not in range(0, idx):
                raise ParseFailure(f"{where}: batch_ref needs an earlier concept number and no item")
        elif item is not None or ref is not None:
            raise ParseFailure(f"{where}: new takes no item and no batch_ref")
        if not isinstance(d["justification"], str):
            raise ParseFailure(f"{where}.justification: expected a string")
        out[idx] = {"decision": kind, "item": item, "batch_ref": ref,
                    "justification": d["justification"].strip()}
    missing = sorted(set(neighbours) - set(out))
    if missing:
        raise ParseFailure(f"no decision for concepts {missing}")
    return out


def parse_premortem(text: str, max_candidates: int, max_causes: int) -> list[dict]:
    """The premortem reply: {"causes": [{cause, candidates: [{title, summary}]}]},
    ranked most likely first. Returns the causes in that order."""
    obj = load_json_object(text)
    _exact(obj, {"causes"}, "reply")
    causes = obj["causes"]
    if not isinstance(causes, list) or not 1 <= len(causes) <= max_causes:
        raise ParseFailure(f"causes: expected a list of 1 to {max_causes}")
    out = []
    for i, c in enumerate(causes):
        where = f"causes[{i}]"
        if not isinstance(c, dict):
            raise ParseFailure(f"{where}: not an object")
        _exact(c, {"cause", "candidates"}, where)
        cands = c["candidates"]
        if not isinstance(cands, list) or not 1 <= len(cands) <= max_candidates:
            raise ParseFailure(f"{where}.candidates: expected a list of 1 to {max_candidates}")
        parsed = []
        for j, k in enumerate(cands):
            kw = f"{where}.candidates[{j}]"
            if not isinstance(k, dict):
                raise ParseFailure(f"{kw}: not an object")
            _exact(k, {"title", "summary"}, kw)
            parsed.append({"title": _text(k["title"], f"{kw}.title"),
                           "summary": _text(k["summary"], f"{kw}.summary")})
        out.append({"cause": _text(c["cause"], f"{where}.cause"), "candidates": parsed})
    return out


def parse_check(text: str, items: set[str], transcripts: set[str]) -> dict:
    """The check reply: {owner_discussed, quote, matches_item, exposed_in}.
    `items` and `transcripts` are the ids the prompt showed. A quote is required
    for every verdict but `none` and forbidden for `none`; the caller verifies it."""
    obj = load_json_object(text)
    _exact(obj, {"owner_discussed", "quote", "matches_item", "exposed_in"}, "reply")
    verdict = obj["owner_discussed"]
    if verdict not in OWNER_DISCUSSED:
        raise ParseFailure(f"owner_discussed: {verdict!r} is not one of {OWNER_DISCUSSED}")
    quote = obj["quote"]
    if not isinstance(quote, str):
        raise ParseFailure("quote: expected a string")
    if (verdict == "none") != (not quote.strip()):
        raise ParseFailure("quote: required for a discussed verdict, empty for none")
    item = obj["matches_item"]
    if item is not None and item not in items:
        raise ParseFailure(f"matches_item: {item!r} names no item shown")
    exposed = obj["exposed_in"]
    if not isinstance(exposed, list) or any(e not in transcripts for e in exposed):
        raise ParseFailure("exposed_in: expected a list of transcripts that were shown")
    return {"owner_discussed": verdict, "quote": quote.strip(), "matches_item": item,
            "exposed_in": list(dict.fromkeys(exposed))}


def parse_consequence(text: str) -> dict:
    """The consequence reply: {verdict: consequential|tactical, reason: one line}."""
    obj = load_json_object(text)
    _exact(obj, {"verdict", "reason"}, "reply")
    if obj["verdict"] not in CONSEQUENCE_VERDICTS:
        raise ParseFailure(f"verdict: {obj['verdict']!r} is not one of {CONSEQUENCE_VERDICTS}")
    return {"verdict": obj["verdict"], "reason": _text(obj["reason"], "reason")}


def parse_queries(text: str, max_queries: int) -> list[str]:
    """The queries reply: {"queries": [question, ...]}, 1 to max_queries."""
    obj = load_json_object(text)
    _exact(obj, {"queries"}, "reply")
    qs = obj["queries"]
    if not isinstance(qs, list) or not 1 <= len(qs) <= max_queries:
        raise ParseFailure(f"queries: expected a list of 1 to {max_queries}")
    return [_text(q, f"queries[{i}]") for i, q in enumerate(qs)]


def parse_research(text: str, max_results: int) -> list[dict]:
    """The research reply: {"results": [{url, title}]}, 0 to max_results."""
    obj = load_json_object(text)
    _exact(obj, {"results"}, "reply")
    rs = obj["results"]
    if not isinstance(rs, list) or len(rs) > max_results:
        raise ParseFailure(f"results: expected a list of at most {max_results}")
    out = []
    for i, r in enumerate(rs):
        where = f"results[{i}]"
        if not isinstance(r, dict):
            raise ParseFailure(f"{where}: not an object")
        _exact(r, {"url", "title"}, where)
        out.append({"url": _text(r["url"], f"{where}.url"), "title": _text(r["title"], f"{where}.title")})
    return out


def parse_claims(text: str, source_ids: set[str], max_claims: int) -> list[dict]:
    """The claims reply: {"claims": [{text, kind, sources: [{source, passage}]}]}.
    `source_ids` are the ids the prompt showed. A claim with no sources parses (code
    drops it); a source id that was not shown does not."""
    obj = load_json_object(text)
    _exact(obj, {"claims"}, "reply")
    cs = obj["claims"]
    if not isinstance(cs, list) or len(cs) > max_claims:
        raise ParseFailure(f"claims: expected a list of at most {max_claims}")
    out = []
    for i, c in enumerate(cs):
        where = f"claims[{i}]"
        if not isinstance(c, dict):
            raise ParseFailure(f"{where}: not an object")
        _exact(c, {"text", "kind", "sources"}, where)
        if c["kind"] not in CLAIM_KINDS:
            raise ParseFailure(f"{where}.kind: {c['kind']!r} is not one of {CLAIM_KINDS}")
        if not isinstance(c["sources"], list):
            raise ParseFailure(f"{where}.sources: not a list")
        srcs = []
        for j, s in enumerate(c["sources"]):
            sw = f"{where}.sources[{j}]"
            if not isinstance(s, dict):
                raise ParseFailure(f"{sw}: not an object")
            _exact(s, {"source", "passage"}, sw)
            if s["source"] not in source_ids:
                raise ParseFailure(f"{sw}.source: {s['source']!r} names no source shown")
            srcs.append({"source": s["source"], "passage": _text(s["passage"], f"{sw}.passage")})
        out.append({"text": _text(c["text"], f"{where}.text"), "kind": c["kind"], "sources": srcs})
    return out


def parse_compose(text: str) -> dict:
    """The compose reply: {teachable, title, body}. Not teachable means the verified
    claims cannot teach the concept: title and body are then empty."""
    obj = load_json_object(text)
    _exact(obj, {"teachable", "title", "body"}, "reply")
    if not isinstance(obj["teachable"], bool):
        raise ParseFailure("teachable: expected true or false")
    for k in ("title", "body"):
        if not isinstance(obj[k], str):
            raise ParseFailure(f"{k}: expected a string")
    title, body = obj["title"].strip(), obj["body"].strip()
    if obj["teachable"] != bool(title and body) or (not obj["teachable"] and (title or body)):
        raise ParseFailure("title and body are required when teachable and empty when not")
    return {"teachable": obj["teachable"], "title": title, "body": body}


def parse_lesson_check(text: str) -> dict:
    """The lesson-check reply: {verdict: pass|reject, failed: [reject-list item], reasons: [str]}.
    A reject names what failed and why; a pass names nothing."""
    obj = load_json_object(text)
    _exact(obj, {"verdict", "failed", "reasons"}, "reply")
    if obj["verdict"] not in CHECK_VERDICTS:
        raise ParseFailure(f"verdict: {obj['verdict']!r} is not one of {CHECK_VERDICTS}")
    failed, reasons = obj["failed"], obj["reasons"]
    if not isinstance(failed, list) or any(f not in CHECK_FAILS for f in failed):
        raise ParseFailure(f"failed: expected a list drawn from {CHECK_FAILS}")
    if not isinstance(reasons, list):
        raise ParseFailure("reasons: expected a list")
    reasons = [_text(r, "reasons[]") for r in reasons]
    if (obj["verdict"] == "reject") != bool(failed and reasons):
        raise ParseFailure("a reject names what failed and why; a pass names neither")
    return {"verdict": obj["verdict"], "failed": list(dict.fromkeys(failed)), "reasons": reasons}
