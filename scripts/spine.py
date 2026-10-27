#!/usr/bin/env python3
"""The solution spine (CLAUDE.md rule 5b).

Route says WHERE the research leg looks; problem type says what KIND of answer
a loop needs. Every type walks the same six stages, at a depth the table below
sets. This module is the single source for that table: the prompt is rendered
from it, the page is validated against it, and CLAUDE.md rule 5b is tested
against it.

The model proposes; check() verifies. Nothing here calls a model or writes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG  # noqa: E402
import questions as Q  # noqa: E402
import vault as V  # noqa: E402

TYPES = ("factual", "diagnostic", "design", "decision", "dilemma")
STAGES = ("define", "diagnose", "options", "evaluate", "recommend", "check")
# `recommend` keeps the page's existing heading: scripts/board_publish.py
# publishes `## Synthesis` and refuses a page that renames it.
HEADINGS = {"define": "Define", "diagnose": "Diagnose", "options": "Options",
            "evaluate": "Evaluate", "recommend": "Synthesis", "check": "Check"}

_F, _B, _N = "full", "brief", None
DEPTH = {
    "factual":    dict(zip(STAGES, (_B, _N, _N, _N, _F, _B))),
    "diagnostic": dict(zip(STAGES, (_F, _F, _B, _B, _F, _F))),
    "design":     dict(zip(STAGES, (_F, _B, _F, _F, _F, _B))),
    "decision":   dict(zip(STAGES, (_B, _N, _F, _F, _F, _B))),
    "dilemma":    dict(zip(STAGES, (_F, _B, _F, _B, _F, _F))),
}
ASKS = {
    "factual": "What is true? An answer exists somewhere.",
    "diagnostic": "Why does this keep happening?",
    "design": "What should I build or set up?",
    "decision": "Which of these should I choose?",
    "dilemma": "How do I hold two things that pull against each other?",
}
# Types whose recommendation is a choice among options, and so needs the
# owner's own criteria to be more than a guess.
NEEDS_CRITERIA = ("design", "decision")
VERDICTS = ("meets", "partly", "fails")
MISSING = "*Stage missing.*"
# Every key the spine reads. `synthesis` and `open_sub_questions` are not
# here: they are existing Step 3 fields and stay inside `sections`.
STAGE_FIELDS = ("current_state", "desired_state", "gap", "why_it_matters", "define",
                "diagnosis", "assumptions", "options", "evaluation", "criteria",
                "assessment", "premortem", "recommended_option", "success_signal")
PROVISIONAL = "> Provisional — your criteria for this choice are not on record."
_DEFINE_LABELS = (("current", "Now"), ("desired", "Wanted"), ("gap", "Gap"),
                  ("why_it_matters", "Why it matters"))


# --------------------------------------------------------------------------
# Config

def _cfg(key: str):
    block = CFG.get("solving")
    if not isinstance(block, dict) or key not in block:
        raise SystemExit(f"config.yaml is missing required key solving.{key} — "
                         f"refusing to guess a default")
    return block[key]


def enabled() -> bool:
    """Off unless config says on. A config with no `solving` block (one written
    before the spine existed) means off, not an error: absence must never
    stop the research prompt from rendering."""
    block = CFG.get("solving")
    return bool(block.get("enabled")) if isinstance(block, dict) else False


def min_options() -> int:
    return int(_cfg("min_options"))


def min_quote_chars() -> int:
    """A criterion quote shorter than this proves nothing: 'a' is in every
    Human turn. The floor is the one owner writes already use (rule 16)."""
    block = CFG.get("mcp")
    if not isinstance(block, dict) or "owner_write_quote_min_chars" not in block:
        raise SystemExit("config.yaml is missing required key "
                         "mcp.owner_write_quote_min_chars — refusing to guess a default")
    return int(block["owner_write_quote_min_chars"])


# A decision-only loop does no research, so it has no cause to show and no
# recommendation to make: it frames the decision and stops (rule 5).
DECISION_ONLY_SKIPS = ("diagnose", "recommend")


def required(ptype: str, route: str = "") -> list[str]:
    skip = DECISION_ONLY_SKIPS if route == "decision-only" else ()
    return [s for s in STAGES if DEPTH[ptype][s] and s not in skip]


def depth_table() -> str:
    head = "| | " + " | ".join(HEADINGS[s] if s != "recommend" else "Recommend"
                               for s in STAGES) + " |"
    rule = "|---|" + "---|" * len(STAGES)
    rows = ["| `{}` | {} |".format(t, " | ".join(DEPTH[t][s] or "—" for s in STAGES))
            for t in TYPES]
    return "\n".join([head, rule] + rows)


# --------------------------------------------------------------------------
# Validation

def _line(text) -> str:
    """One line, always. Stage text is rendered after a label or a bullet; a
    newline in it could otherwise open a heading the writer does not own."""
    return " ".join(str(text or "").split())


def _is_leave(name: str) -> bool:
    return _line(name).lower().rstrip(".!") == "leave it"


_HUMAN_MARK = re.compile(r"^(##\s+Human\b|Human:)", re.I | re.M)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,}|---\s*$)")


def _list(value) -> list:
    """Model output is untrusted in shape as well as content: a field that
    should be a list and is not is treated as empty, never iterated."""
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def plain(text) -> str:
    """Multi-paragraph stage text (the recommendation, the restatement) with
    its page structure removed: heading markers are stripped and fence lines
    dropped, so the words survive and cannot open a section or swallow one."""
    out = []
    for line in str(text if isinstance(text, str) else "").splitlines():
        if _FENCE.match(line):
            continue
        out.append(re.sub(r"^\s*#{1,6}\s+", "", line))
    return "\n".join(out).strip()


def _transcript_of(citation) -> Path | None:
    """The transcript a wikilink points at, or None. An anchor or an alias
    (`[[x#Human|label]]`) is dropped before resolving. Anything that is not a
    file under sources/transcripts — a conclusion, a loop page, a file outside
    the vault — is Dreamer's output or nobody's (rule 13) and resolves to
    nothing here."""
    inner = _line(citation).strip("[]").split("|", 1)[0].split("#", 1)[0].strip()
    target = V._resolve_wikilink(f"[[{inner}]]") if inner else None
    if not target:
        return None
    try:
        path = Path(target).resolve()
        if not path.is_relative_to(Path(CFG["paths"]["sources"]).resolve()):
            return None
    except (OSError, ValueError):
        return None
    return path


def _canonical(path: Path) -> str:
    """`[[sources/transcripts/…]]` — one spelling, whatever the model wrote."""
    rel = path.relative_to(Path(CFG["paths"]["vault"]).resolve()).with_suffix("")
    return f"[[{rel.as_posix()}]]"


def _verify_criterion(c: dict, allowed: set | None) -> tuple[str | None, str]:
    """(canonical citation, "") when the criterion is the owner's and about
    this choice; (None, why) otherwise. It must quote a Human turn of a
    transcript, with a real phrase, and — when `allowed` is given — that
    transcript must be one of the loop's own conversations: something the
    owner said about a different question is not a criterion for this one."""
    path = _transcript_of(c.get("citation"))
    if path is None:
        return None, "it does not cite a transcript"
    if allowed is not None and path not in allowed:
        return None, "the transcript it cites is not one of this loop's conversations"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "its transcript cannot be read"
    if not _HUMAN_MARK.search(text):
        return None, "its transcript has no Human turn"
    quote = c.get("quote") if isinstance(c.get("quote"), str) else ""
    if len(Q.normalize_quote(quote)) < min_quote_chars():
        return None, "its quote is too short to prove anything"
    if not Q.quote_in(quote, Q.normalize_quote(Q.extract_human_turns(text))):
        return None, "its quote is not in a Human turn of that transcript"
    return _canonical(path), ""


def check(payload: dict, problems: list[str], claims, route: str = "",
          occurrences: list | None = None, is_derived=None) -> dict | None:
    """Validate the spine fields of a research payload.

    Returns None when the payload carries no usable problem_type — the caller
    then renders the legacy page. `claims` is apply_conclusion._claims, passed
    in so diagnosis entries get exactly the evidence rules every claim gets.
    """
    raw_type = payload.get("problem_type")
    if raw_type is None or raw_type == "":
        return None
    ptype = _line(raw_type).lower() if isinstance(raw_type, str) else repr(raw_type)[:40]
    if ptype not in TYPES:
        problems.append(f"spine: unknown problem_type {ptype!r} — page written "
                        f"in the legacy layout")
        return None

    # The model closes `sections` where the original Step 3 template ends and
    # writes the stage fields beside it, so that is where the prompt asks for
    # them. A field found inside `sections` is still read, and wins.
    s = dict(_dict(payload.get("sections")))
    for key in STAGE_FIELDS:
        if key not in s and key in payload:
            s[key] = payload[key]
    depth = DEPTH[ptype]
    needed = required(ptype, route)
    defects = False

    # Flat fields are what the prompt asks for; the nested `define` and
    # `evaluation` objects are still read, because a deeper structure is what
    # the model gets wrong (models often miscount braces in deep structures).
    nested_define = _dict(s.get("define"))
    define = {}
    for key, flat in (("current", "current_state"), ("desired", "desired_state"),
                      ("gap", "gap"), ("why_it_matters", "why_it_matters")):
        value = s.get(flat) if isinstance(s.get(flat), str) else nested_define.get(key)
        if isinstance(value, str) and _line(value):
            define[key] = _line(value)
    diagnosis = claims(_list(s.get("diagnosis")), "diagnosis", problems) if depth["diagnose"] else []
    # A derived citation (Dreamer's own page) is a hypothesis, never the cause
    # shown: it leaves Diagnose for the quarantine section (rule 13).
    derived_diagnosis = [c for c in diagnosis if is_derived and is_derived(c["citation"])]
    diagnosis = [c for c in diagnosis if c not in derived_diagnosis]
    allowed = None
    if occurrences is not None:
        allowed = {pth for pth in (_transcript_of(o) for o in occurrences) if pth}
    assumptions = [_line(a) for a in _list(s.get("assumptions"))
                   if isinstance(a, str) and _line(a)]

    options, seen = [], set()
    for o in _list(s.get("options")):
        name = _line(o.get("name")) if isinstance(o, dict) and isinstance(o.get("name"), str) else ""
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        options.append({"name": name, "description": _line(o.get("description"))})

    ev = _dict(s.get("evaluation"))
    ev = {key: s[key] if key in s else ev.get(key)
          for key in ("criteria", "assessment", "premortem")}
    criteria = []
    for i, c in enumerate(_list(ev.get("criteria"))):
        if not isinstance(c, dict) or not _line(c.get("criterion")):
            continue
        citation, why = _verify_criterion(c, allowed)
        if citation:
            criteria.append({"criterion": _line(c["criterion"]), "citation": citation,
                             "quote": _line(c.get("quote"))})
        else:
            problems.append(f"spine: criterion[{i}] DROPPED — {why}")
    names = {o["name"].lower() for o in options}
    kept = {c["criterion"].lower() for c in criteria}
    assessment = []
    for a in _list(ev.get("assessment")):
        if not isinstance(a, dict):
            continue
        verdict = _line(a.get("verdict")).lower()
        if (_line(a.get("option")).lower() in names
                and _line(a.get("criterion")).lower() in kept and verdict in VERDICTS):
            assessment.append({"option": _line(a["option"]),
                               "criterion": _line(a["criterion"]),
                               "verdict": verdict, "note": _line(a.get("note"))})
    premortem = _line(ev.get("premortem")) if isinstance(ev.get("premortem"), str) else ""
    success_signal = (_line(s.get("success_signal"))
                      if isinstance(s.get("success_signal"), str) else "")
    synthesis = s.get("synthesis").strip() if isinstance(s.get("synthesis"), str) else ""

    # A full Diagnose is the point of a diagnostic loop: it needs a cited
    # cause. Assumptions alone are enough only where Diagnose is brief.
    diagnosed = bool(diagnosis) if depth["diagnose"] == "full" else bool(diagnosis or assumptions)
    present = {"define": bool(define), "diagnose": diagnosed,
               "options": bool(options),
               "evaluate": bool(criteria or assessment or premortem),
               "recommend": bool(synthesis), "check": bool(success_signal)}
    missing = [st for st in needed if not present[st]]
    for st in missing:
        problems.append(f"spine: stage '{st}' is required for a {ptype} loop and "
                        f"is missing")

    if depth["options"] == "full" and options:
        real = [o for o in options if not _is_leave(o["name"])]
        if len(real) < min_options() or len(real) == len(options):
            defects = True
            problems.append(f"spine: a {ptype} loop needs at least {min_options()} "
                            f"options plus one named 'Leave it' — got "
                            f"{len(real)} and {len(options) - len(real)}")
    if depth["evaluate"] == "full" and not premortem:
        defects = True
        problems.append("spine: Evaluate has no premortem of the leading option")

    # The recommended option is named in its own field and matched to a
    # listed option exactly. Looking for a name inside the prose would accept
    # "do not choose X".
    wanted = _line(s.get("recommended_option")).lower() if isinstance(
        s.get("recommended_option"), str) else ""
    recommended = next((o["name"] for o in options if wanted and o["name"].lower() == wanted), "")

    provisional = False
    if ptype in NEEDS_CRITERIA:
        if not criteria:
            provisional = True
            problems.append("spine: no criterion traces to the owner's own words — "
                            "recommendation is provisional")
        if "recommend" in needed and not recommended:
            defects = True
            problems.append("spine: recommended_option does not name one of the "
                            "listed options")

    return {"type": ptype, "route": route, "define": define, "diagnosis": diagnosis,
            "assumptions": assumptions, "options": options, "criteria": criteria,
            "derived_diagnosis": derived_diagnosis, "recommended": recommended,
            "assessment": assessment, "premortem": premortem,
            "success_signal": success_signal, "missing": missing,
            "cap_low": bool(missing or defects or provisional),
            "provisional": provisional}


# --------------------------------------------------------------------------
# Rendering. Every line starts with a heading this module owns, a bullet, or
# a bold label — never with model text.

def _stage(sp: dict, stage: str, body: list[str]) -> list[str]:
    if not DEPTH[sp["type"]][stage]:
        return []
    if stage not in required(sp["type"], sp.get("route", "")) and not body:
        return []
    if stage in sp["missing"]:
        body = [MISSING]
    return [f"## {HEADINGS[stage]}", ""] + body + [""]


def render_stages(sp: dict, support_mark: dict) -> list[str]:
    """Define, Diagnose, Options, Evaluate — the sections that sit between the
    evidence ledger and `## Synthesis`."""
    define = [f"- **{label}.** {sp['define'][key]}"
              for key, label in _DEFINE_LABELS if sp["define"].get(key)]

    diagnose: list[str] = []
    for c in sp["diagnosis"]:
        diagnose.append(f"- **[{support_mark[c['support']]}]** {c['claim']}")
        diagnose.append(f"  - source: `{c['citation']}`")
        if c.get("quote"):
            diagnose.append(f"  - > {_line(c['quote'])}")
    if sp["assumptions"]:
        diagnose += ["", "Assumed, not shown — check these:"]
        diagnose += [f"- {a}" for a in sp["assumptions"]]

    options = [f"- **{o['name']}**" + (f" — {o['description']}" if o["description"] else "")
               for o in sp["options"]]

    evaluate: list[str] = []
    if sp["criteria"]:
        evaluate.append("Criteria, in your words:")
        for c in sp["criteria"]:
            evaluate.append(f"- **{c['criterion']}** — source: `{c['citation']}`")
            evaluate.append(f"  - > {c['quote']}")
    elif sp["type"] in NEEDS_CRITERIA:
        evaluate.append("- *No criteria on record.*")
    if sp["assessment"]:
        evaluate += ["", "Each option against them:"]
        for a in sp["assessment"]:
            evaluate.append(f"- {a['option']} / {a['criterion']}: **{a['verdict']}**"
                            + (f" — {a['note']}" if a["note"] else ""))
    if sp["premortem"]:
        evaluate += ["", f"**Premortem.** {sp['premortem']}"]
    if sp["recommended"]:
        evaluate += ["", f"**Recommended:** {sp['recommended']}"]

    return (_stage(sp, "define", define) + _stage(sp, "diagnose", diagnose)
            + _stage(sp, "options", options) + _stage(sp, "evaluate", evaluate))


def render_check(sp: dict) -> list[str]:
    return _stage(sp, "check", [f"**It worked if:** {sp['success_signal']}"])


def synthesis_note(sp: dict) -> list[str]:
    return [PROVISIONAL, ""] if sp["provisional"] else []


def framing(sp: dict) -> str:
    """The spine for a decision-only loop: labelled paragraphs for the loop
    page's `## Decision framing` section. No headings — that section belongs
    to the loop page, not to this module."""
    parts: list[str] = []
    for key, label in _DEFINE_LABELS:
        if sp["define"].get(key):
            parts.append(f"**{label}.** {sp['define'][key]}")
    if sp["options"]:
        parts.append("**Options.** " + "; ".join(
            o["name"] + (f" — {o['description']}" if o["description"] else "")
            for o in sp["options"]))
    if sp["criteria"]:
        parts.append("**Criteria, in your words.** " + "; ".join(
            f"{c['criterion']} (`{c['citation']}`)" for c in sp["criteria"]))
    elif sp["type"] in NEEDS_CRITERIA:
        parts.append("**Criteria.** None on record — what matters most to you here?")
    if sp["premortem"]:
        parts.append(f"**Premortem.** {sp['premortem']}")
    if sp["success_signal"]:
        parts.append(f"**It worked if:** {sp['success_signal']}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Prompt

def prompt_block() -> str:
    types = "\n".join(f"- `{t}` — {ASKS[t]}" for t in TYPES)
    return f"""## Step 1b — Problem type and the solution spine (CLAUDE.md rule 5b)

The route says where to look. Now decide what KIND of answer this loop needs —
exactly one of the five below. Decide it from the loop's title and the owner's
own words in the occurrences, before you research, and do not change it
afterwards: what you find changes the answer, not the kind of question. A
tension does not stop being a tension because you found a good way to live
with it.

{types}

A `design` problem has a solution once built. If the loop is a tension
between two things the owner values, and no option makes the tension go away,
it is a `dilemma`, not a design. On genuine uncertainty choose the type that
requires more stages: `dilemma` > `design` > `diagnostic` > `decision` >
`factual`.

Then work the loop through the stages its type requires:

{depth_table()}

`full` carries the effort. `brief` is one to three sentences. `—` means omit
the field.

1. **Define** — where things are now, where the owner wants them, the gap, and
   why it matters to them.
2. **Diagnose** — the cause, with evidence. Separate what is shown from what
   is assumed.
3. **Options** — genuinely different ways forward.
4. **Evaluate** — the owner's own criteria, each option against them, and how
   the leading option fails.
5. **Recommend** — one directive next step. This is the existing `synthesis`.
6. **Check** — what would show it worked (`success_signal`) and what would
   change the answer (`open_sub_questions`).

Add `"problem_type"` and these fields at the top level of the JSON object,
next to `"sections"` — not inside it. `"sections"` holds exactly the Step 3
fields and nothing else. The new fields are flat on purpose; do not wrap them
in another object:

```json
{{
  "current_state": "…",
  "desired_state": "…",
  "gap": "…",
  "why_it_matters": "…",
  "diagnosis": [
    {{"claim": "…", "citation": "…", "quote": "…", "support": "accepted"}}
  ],
  "assumptions": ["Something you could not show. Stated plainly."],
  "options": [
    {{"name": "Short name", "description": "…"}},
    {{"name": "Leave it", "description": "What happens if nothing changes."}}
  ],
  "criteria": [
    {{"criterion": "…", "citation": "[[sources/transcripts/…]]", "quote": "…"}}
  ],
  "assessment": [
    {{"option": "Short name", "criterion": "…", "verdict": "meets", "note": "…"}}
  ],
  "premortem": "How the leading option fails, and the earliest sign of it.",
  "recommended_option": "Short name",
  "success_signal": "What would show it worked."
}}
```

Close `sections` once, after `open_sub_questions`. Everything above and
`web_queries` then sit side by side at the top level.

Rules for these fields:

- Every `diagnosis` entry is a claim: it needs a citation and a `support`
  grade, under the same provenance rules as every other claim. `assumptions`
  are not claims and are never evidence.
- **A criterion is the owner's or it is absent.** Each one must quote the
  owner verbatim — a full phrase, not a word — from a `## Human` turn of one
  of this loop's occurrences listed below, and cite that transcript. What the
  owner said about a different question is not a criterion for this one. A loop page, a conclusion page or the
  thread is Dreamer's output and is never a source for a criterion. Never
  supply a criterion yourself, and never quote an `## Assistant` turn. If the
  owner has not said what matters, return no criteria: the pipeline marks the
  recommendation provisional and asks them.
- Where Options is `full`, give at least {min_options()} options and one more
  named exactly `Leave it` — those two words, nothing after them.
- `verdict` is exactly one of `meets`, `partly`, `fails`.
- Open `synthesis` with the recommendation itself, in one sentence. Notes on
  the route, on a corpus that came back empty, or on an escalation to the web
  (rule 5a) still belong in `synthesis`, after that first sentence.
- For `design` and `decision`, put the exact `name` of the option you
  recommend in `recommended_option`. It may be `Leave it`.
- For a `dilemma` there is no solution to recommend: recommend the smallest
  experiment that would teach the owner something.
- Each stage text is one paragraph. No headings, no lists inside a field.
- You recommend; the owner decides. Never say a loop is resolved.
- `decision-only` route: still zero search and zero web calls. These fields
  replace `decision_framing`: fill the Define fields, `options`, `criteria`,
  `premortem` and `success_signal` from the occurrences above. There is no
  cause to show and no recommendation to make, so omit `diagnosis` and leave
  `synthesis` empty.
"""
