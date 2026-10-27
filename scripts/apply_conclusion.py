#!/usr/bin/env python3
"""Apply a weekly-dream result: write the conclusion page, transition the loop.

Deterministic half of Job 2. Enforces the two rules the LLM cannot be trusted to
self-police: every claim must carry a citation (§6.3 rule 8), and web-sourced
text must land inside the untrusted block (§6.3 rule 10).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, log, p, read_page, today, write_page  # noqa: E402
import digest as G  # noqa: E402
import spine as SP  # noqa: E402
import vault as V  # noqa: E402

# The digest's "next:" line is one line; a synthesis with no sentence break
# must not put a paragraph there.
NEXT_STEP_MAX = 240

VALID_ROUTES = {"wisdom", "web", "past-reasoning", "decision-only", "mixed"}
# Evidence grades from the vendored autoresearch contract. Citation is not
# quality: without a grade, a marketing blog and a peer-reviewed study render
# identically to the reader.
VALID_SUPPORT = {"accepted", "provisional", "contested", "unsupported"}
SUPPORT_MARK = {"accepted": "✓ accepted", "provisional": "~ provisional",
                "contested": "! contested", "unsupported": "✗ unsupported"}

# Derived-tier citations (CLAUDE.md rule 13): Dreamer's own prior output.
# A conclusion citing a conclusion is the self-reinforcing-bias engine — L0023
# built a 4-generation accepted-graded chain in two days before this existed.
# Resurfacing notes are MCP-client input, loop pages are Dreamer distillations;
# none of them are evidence.
_DERIVED_CITE = re.compile(
    r"\[\[\s*(conclusions/|sources/resurfacings/|loops/)", re.I)
# A living-thread citation renders as "([[sources/transcripts/...]] via
# thread)" (rule 15). The link target is primary, but the sentence citing it
# is Dreamer's own fold output — the marker, not the target, carries the tier
# (rule 13), so `via thread` adjacent to the wikilink grades derived.
_VIA_THREAD_CITE = re.compile(r"\]\]\s*via thread\b", re.I)

def _is_derived(citation: str) -> bool:
    c = citation or ""
    return bool(_DERIVED_CITE.search(c) or _VIA_THREAD_CITE.search(c))


def _neutralize_structure(text: str, problems: list[str]) -> str:
    """Line-start structure in model output must not become PAGE structure.

    Decision framing lands inside the loop page's '## Decision framing'
    section; a line opening with a heading marker or an HR/frontmatter fence
    would end that section and plant one the applier does not own (a second
    Thread heading is the canonical bad case). Headings are prefix-escaped to
    plain text (leading #s stripped), fence lines are dropped, and the edit
    is logged — the words survive, the structure does not (the same safety
    valve apply_thread._flatten provides for fold output).
    """
    out: list[str] = []
    changed = 0
    for line in (text or "").splitlines():
        if re.match(r"^#{1,6}\s", line):
            out.append(re.sub(r"^#{1,6}\s+", "", line))
            changed += 1
        elif re.match(r"^---\s*$", line):
            changed += 1  # dropped: a bare fence carries no words to keep
        else:
            out.append(line)
    if changed:
        problems.append(f"decision framing contained {changed} structural "
                        f"line(s) (headings/fences) — neutralized to plain "
                        f"text before insertion")
    return "\n".join(out).strip()


def _one_line(text) -> str:
    """A field that is rendered on a single bullet line must BE a single line.
    A claim, citation, sub-question, query or URL carrying a newline would end
    its bullet and could open a `## Heading` of its own — web text planting a
    section of the page (rule 10). Whitespace runs collapse to one space."""
    return " ".join(str(text or "").split())


def _slug(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].rsplit("-", 1)[0] or s[:maxlen]) or "conclusion"


def _claims(items, kind: str, problems: list[str]) -> list[dict]:
    """Drop uncited claims rather than writing them. A bad citation is worse
    than a missing claim, because the page is later retrieved as fact."""
    out = []
    for i, c in enumerate(items or []):
        if not isinstance(c, dict):
            problems.append(f"{kind}[{i}]: not an object")
            continue
        claim = _one_line(c.get("claim"))
        citation = _one_line(c.get("citation"))
        if not claim:
            continue
        if not citation:
            problems.append(f"{kind}[{i}]: DROPPED — uncited claim {claim[:60]!r}")
            continue
        support = str(c.get("support") or "provisional").strip().lower()
        if support not in VALID_SUPPORT:
            problems.append(f"{kind}[{i}]: unknown support grade "
                            f"{support!r} — treated as provisional")
            support = "provisional"
        out.append({"claim": claim, "citation": citation,
                    "quote": (c.get("quote") or "").strip(),
                    "support": support})
    return out


def render(loop: V.Loop, payload: dict, problems: list[str],
           sp: dict | None = None) -> str:
    s = payload.get("sections") or {}
    route = payload.get("route", "")
    lines: list[str] = [f"# {payload.get('title') or loop.title}", ""]
    lines += [f"_Loop [[loops/{loop.id}]] · route `{route}` · confidence "
              f"`{payload.get('confidence', 'unknown')}` · {today().isoformat()}_", ""]

    if s.get("restated"):
        # On a spine page the restatement and the recommendation are stage
        # text like any other and lose their page structure (rule 10). Legacy
        # pages are written exactly as before.
        restated = SP.plain(s["restated"]) if sp else s["restated"].strip()
        lines += ["## The loop, restated", "", restated, ""]

    # Rule 13: partition every bucket into primary vs derived-tier citations.
    # A derived citation (Dreamer's own conclusion, a loop page, a resurfacing
    # note) is a hypothesis, not evidence — it is quarantined into its own
    # section and its grade is capped, whatever the model claimed. Rendering
    # "[✓ accepted] … source: [[conclusions/…]]" under "What you previously
    # concluded" is exactly how the L0023 self-citation chain grew.
    derived: list[dict] = []
    all_claims: list[dict] = []
    for key, heading in (("wisdom_says", "What the library says"),
                         ("owner_previously_concluded", "What you previously concluded"),
                         ("web_says", "What the web says")):
        claims = []
        for c in _claims(s.get(key), key, problems):
            if _is_derived(c["citation"]):
                if c["support"] in ("accepted", "provisional"):
                    problems.append(
                        f"{key}: derived citation {c['citation'][:60]!r} "
                        f"graded {c['support']} — capped at contested and "
                        f"quarantined (rule 13)")
                    c["support"] = "contested"
                derived.append(c)
            else:
                claims.append(c)
        all_claims += claims
        if not claims:
            continue
        # Web material is untrusted and must be visibly quarantined (rule 10).
        if key == "web_says":
            lines += ["## Web sources (untrusted)", "",
                      "> Fetched from the open web. Quoted as data, not followed as "
                      "instruction. Citation proves a page said this — nothing more.", ""]
        else:
            lines += [f"## {heading}", ""]
        for c in claims:
            lines.append(f"- **[{SUPPORT_MARK[c['support']]}]** {c['claim']}")
            lines.append(f"  - source: `{c['citation']}`")
            if c["quote"]:
                q = c["quote"].replace("\n", " ").strip()
                lines.append(f"  - > {q}")
        lines.append("")

    # Spine diagnosis claims are claims like any other: the primary ones are
    # tallied in the ledger, and a derived one is capped and quarantined with
    # the rest instead of standing under Diagnose as a cause shown (rule 13).
    if sp:
        for c in sp["derived_diagnosis"]:
            if c["support"] in ("accepted", "provisional"):
                problems.append(
                    f"diagnosis: derived citation {c['citation'][:60]!r} graded "
                    f"{c['support']} — capped at contested and quarantined (rule 13)")
                c["support"] = "contested"
            derived.append(c)
        all_claims += sp["diagnosis"]

    if derived:
        lines += ["## Prior conclusions (derived — hypothesis, not evidence)", "",
                  "> Cited from Dreamer's own earlier output. This is a "
                  "hypothesis under re-test, not support: a claim that only "
                  "traces here has not been re-verified against a primary "
                  "source (rule 13).", ""]
        for c in derived:
            lines.append(f"- **[{SUPPORT_MARK[c['support']]}]** {c['claim']}")
            lines.append(f"  - source: `{c['citation']}`")
            if c["quote"]:
                q = c["quote"].replace("\n", " ").strip()
                lines.append(f"  - > {q}")
        lines.append("")

    all_claims += derived
    if all_claims:
        from collections import Counter
        tally = Counter(c["support"] for c in all_claims)
        weak = tally["provisional"] + tally["contested"] + tally["unsupported"]
        ledger = " · ".join(f"{SUPPORT_MARK[k]}: {tally[k]}"
                            for k in ("accepted", "provisional", "contested",
                                      "unsupported") if tally[k])
        lines += ["## Evidence ledger", "", ledger, ""]
        if weak > tally["accepted"]:
            lines += ["> Most of this rests on provisional or weaker evidence. "
                      "Treat the synthesis as a working position, not a finding.",
                      ""]

    if sp:
        lines += SP.render_stages(sp, SUPPORT_MARK)
    synthesis = SP.plain(s.get("synthesis")) if sp else s.get("synthesis")
    if synthesis:
        lines += ["## Synthesis", ""] + (SP.synthesis_note(sp) if sp else []) \
            + [synthesis.strip(), ""]
    elif sp and "recommend" in sp["missing"]:
        lines += ["## Synthesis", "", SP.MISSING, ""]
    if sp:
        lines += SP.render_check(sp)

    subs = s.get("open_sub_questions") or []
    if subs:
        lines += ["## Open sub-questions", ""] + [f"- {_one_line(x)}" for x in subs] + [""]

    if payload.get("decision_framing"):
        lines += ["## Decision framing", "", payload["decision_framing"].strip(), ""]

    queries = payload.get("web_queries") or []
    urls = payload.get("fetched_urls") or []
    if queries or urls:
        lines += ["## Egress record", "",
                  "Everything this conclusion sent off the machine.", ""]
        for q in queries:
            lines.append(f"- query: `{_one_line(q)}`")
        for u in urls:
            lines.append(f"- fetched: `{_one_line(u)}`")
        lines.append("")
    return "\n".join(lines)


def salvage_split_reply(raw: str) -> tuple[dict | None, str]:
    """Rejoin a reply the model split with one stray closing brace.

    The known malformed shape: the model closes the root object early, then
    carries on with `,"web_queries":[…],"fetched_urls":[…]}`. The first object
    is valid and the remainder is the rest of the same object, so the two are
    rejoined — those trailing keys are the egress record (rule 12) and must
    not be lost by keeping only the first half. Anything else is refused: a
    remainder that is not a comma followed by object members, or a key
    present in both halves, means this is not that shape and nothing is
    guessed."""
    text = (raw or "").strip()
    try:
        head, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None, ""
    rest = text[end:].strip()
    if not isinstance(head, dict) or not rest.startswith(",") or not rest.endswith("}"):
        return None, ""
    try:
        tail = json.loads("{" + rest[1:])
    except ValueError:
        return None, ""
    if not isinstance(tail, dict) or set(tail) & set(head):
        return None, ""
    return ({**head, **tail},
            f"reply had a stray closing brace; rejoined {len(tail)} trailing field(s)")


def apply(loop_id: str, payload: dict) -> dict:
    loops = {l.id: l for l in V.load_loops()}
    loop = loops.get(loop_id)
    if loop is None:
        raise SystemExit(f"no such loop: {loop_id}")

    # Rule 14 (conclusion stability): the dream may judge that nothing new has
    # been said since the existing conclusion and serve it instead of writing a
    # superseding page. Zero research, zero new pages — that is a success, and
    # the digest records it so a silent serve is still visible.
    if (payload.get("action") or "").strip() == "serve":
        if not loop.conclusion:
            raise SystemExit(f"{loop.id}: serve action but no conclusion exists")
        loop.status = "paused"
        # A serve answers a re-research request too: it was considered.
        # Leaving the mark would re-select the loop on every cycle.
        loop.reresearch_requested = ""
        loop.save()
        reason = " ".join((payload.get("reason") or "").split())[:300]
        G.stage("served", {"loop": loop.id, "title": loop.title,
                           "conclusion": loop.conclusion,
                           "recurrence": loop.recurrence_count,
                           "reason": reason or "dream judged nothing new"})
        log(f"served existing conclusion for {loop.id}: {reason}",
            job="conclusion")
        return {"loop": loop.id, "route": loop.route, "status": loop.status,
                "conclusion": loop.conclusion, "served": True, "problems": []}

    problems: list[str] = []
    route = (payload.get("route") or "").strip()
    if route not in VALID_ROUTES:
        problems.append(f"invalid route {route!r} — defaulting to decision-only")
        route = "decision-only"

    # A decision-only route that performed research is a contract violation.
    if route == "decision-only" and (payload.get("web_queries")
                                     or payload.get("fetched_urls")):
        problems.append("decision-only route issued web calls — recorded, "
                        "conclusion still written")

    loop.route = route

    # The solution spine (rule 5b), only while solving.enabled is true: with
    # the switch off every payload is a legacy payload, whatever it carries.
    # None means legacy and nothing below changes for it. A defective spine
    # lowers confidence; it never raises it.
    sp = (SP.check(payload, problems, _claims, route, occurrences=loop.occurrences,
                   is_derived=_is_derived) if SP.enabled() else None)
    if SP.enabled() and sp is None and payload.get("problem_type"):
        G.stage("events", {"kind": "degraded", "loop": loop.id,
                           "detail": "research returned an unknown problem_type; "
                                     "the conclusion was written in the legacy layout"})
    # A legacy conclusion clears a type recorded by an earlier spine run, so
    # the loop page never claims a type its current conclusion does not have.
    loop.problem_type = sp["type"] if sp else ""
    loop.reresearch_requested = ""   # this conclusion is the answer to it
    # Lower only: an undeclared or unknown confidence is not turned into `low`,
    # which would read as a grade the research never gave.
    if sp and sp["cap_low"] and payload.get("confidence") in ("high", "medium"):
        payload = dict(payload, confidence="low")

    if route == "decision-only":
        if (payload.get("now") or "").strip():
            problems.append("decision-only route returned a thread 'now' — "
                            "ignored: zero research is its defining property "
                            "and a rebuilt Now claims research happened")
        framing = (SP.framing(sp) if sp else "") or _neutralize_structure(
            (payload.get("decision_framing") or "").strip(), problems)
        # Surgical, never default_body: rebuilding the whole body destroyed
        # the Thread and Theme sections on the decision-only transition
        # (observed live: L0003's thread vanished) — the same body-wipe class
        # merge_loops and add_occurrence already fixed. Replace or append
        # ONLY the '## Decision framing' section.
        body = loop.body or V.default_body(loop)
        if framing:
            body = V.replace_named_section(body, "## Decision framing", framing)
        loop.body = body
        loop.status = "decision-only"
        loop.save()
        result = {"loop": loop.id, "route": route, "status": loop.status,
                  "conclusion": None, "problems": problems,
                  "problem_type": sp["type"] if sp else None}
    else:
        text = render(loop, payload, problems, sp)
        name = f"{today():%Y-%m-%d}--{_slug(payload.get('title') or loop.title)}.md"
        dest = p("conclusions") / name
        fm = {"type": "conclusion", "loop": loop.id, "route": route,
              "confidence": payload.get("confidence", "unknown"),
              "created": today().isoformat(),
              "title": payload.get("title") or loop.title}
        if sp:
            fm["problem_type"] = sp["type"]
            if sp["provisional"]:
                fm["provisional"] = True
        # Re-researching a loop used to overwrite `conclusion`, orphaning the
        # previous page: still on disk, linked from nothing, invisible to lint's
        # navigation and to anyone reading the vault. The reasoning history is
        # the asset here, so a superseded conclusion stays reachable.
        previous = loop.conclusion
        if previous and previous != f"conclusions/{dest.stem}":
            fm["supersedes"] = previous
            prev_path = p("conclusions") / f"{Path(previous).name}.md"
            if prev_path.exists():
                try:
                    prev_fm, prev_body = read_page(prev_path)
                    prev_fm["superseded_by"] = f"conclusions/{dest.stem}"
                    write_page(prev_path, prev_fm,
                               prev_body.rstrip() + "\n\n---\n\n"
                               f"_Superseded by [[conclusions/{dest.stem}]] "
                               f"on {today().isoformat()}._\n")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"could not mark {previous} superseded: {exc}")

            # Record it on the LOOP too. Stamping only the conclusion pages
            # makes the chain discoverable from a page you already found, but
            # the loop is the entry point — without this every re-run mints a
            # fresh orphan and the history is reachable only by luck.
            marker = "## Superseded conclusions"
            body = loop.body or V.default_body(loop)
            if marker not in body:
                body = body.rstrip() + f"\n\n{marker}\n\n- [[{previous}]]\n"
            elif f"[[{previous}]]" not in body:
                body = body.rstrip() + f"\n- [[{previous}]]\n"
            loop.body = body

        write_page(dest, fm, text)
        loop.conclusion = f"conclusions/{dest.stem}"
        loop.status = "paused"
        loop.save()

        # Drift correction (rules 13/15): a dream that re-derived the loop
        # from primary occurrences may return a rebuilt thread `now`.
        # apply_thread.replace_now swaps ONLY the Now paragraph (trajectory
        # untouched) under the same validation contract as a fold. A refusal
        # is a problem, never a failure — the conclusion above is already
        # written and the stale derived Now merely stays visibly derived.
        now_text = (payload.get("now") or "").strip()
        if now_text:
            import apply_thread as AT
            try:
                AT.replace_now(loop.id, now_text)
                log(f"thread Now rebuilt for {loop.id} from the re-research",
                    job="conclusion")
            except AT.FoldError as exc:
                problems.append(f"thread 'now' rebuild refused: {exc}")
        staged = {"path": loop.conclusion, "loop": loop.id, "title": fm["title"],
                  "route": route, "confidence": fm["confidence"]}
        if sp:
            synthesis = _one_line((payload.get("sections") or {}).get("synthesis"))
            first = re.split(r"(?<=[.!?])\s", synthesis, maxsplit=1)[0]
            if len(first) > NEXT_STEP_MAX:
                first = first[:NEXT_STEP_MAX - 1].rstrip() + "…"
            staged.update(problem_type=sp["type"], next_step=first,
                          provisional=sp["provisional"],
                          spine_defects=[pr.removeprefix("spine: ") for pr in problems
                                         if pr.startswith("spine: ")])
        G.stage("conclusions", staged)
        n_derived = len(re.findall(r"source: `\s*\[\[\s*(?:conclusions/|"
                                   r"sources/resurfacings/|loops/)", text))
        if n_derived:
            G.stage("derived_citations", {"loop": loop.id,
                                          "path": loop.conclusion,
                                          "count": n_derived})
            # The digest line is a report; the question is the exit (§5.6).
            import questions as Q
            Q.note_derived_claims(loop.id, loop.conclusion, n_derived)
        result = {"loop": loop.id, "route": route, "status": loop.status,
                  "conclusion": loop.conclusion, "problems": problems,
                  "problem_type": sp["type"] if sp else None}

    # Asked only once the page (or the framing) is written: a question about
    # a conclusion that failed to land would point at nothing.
    if sp and sp["provisional"]:
        import questions as Q
        Q.enqueue_question(
            "criteria",
            f"I could not find what matters most to you in this choice: "
            f"{loop.title} — what should the options be judged on?",
            loop_id=loop.id)

    for q in payload.get("web_queries") or []:
        G.stage("web_queries", q)
    for t in payload.get("proposed_tags") or []:
        G.stage("proposed_tags", t)

    for prob in problems:
        log(f"WARN {prob}", job="conclusion")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", required=True)
    ap.add_argument("--input", default="-")
    args = ap.parse_args()
    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    from apply_extraction import _extract_json
    payload = _extract_json(raw)
    if payload is None:
        payload, note = salvage_split_reply(raw)
        if payload is not None:
            log(f"WARN {note}", job="conclusion")
    if payload is None:
        # The dream leg sets status=researching before running, so an
        # unparseable reply strands the loop there: `researching` never decays
        # (rule 3) and is skipped by selection, so the loop would sit invisible
        # until someone ran `vault.py recover`. Hand it back to `open` here so
        # the very next cycle can retry it.
        #
        # Observed live on L0012 (2026-08-02): the model noticed the loop had
        # already been researched that day and said so in prose instead of
        # emitting the JSON contract — a sensible judgement delivered in a shape
        # the pipeline cannot accept.
        log("FATAL: no JSON in dream output", job="conclusion")
        try:
            loop = next((l for l in V.load_loops() if l.id == args.loop), None)
            if loop is not None and loop.status == "researching":
                loop.status = "open"
                loop.save()
                log(f"reset {args.loop} researching -> open so it is retried, "
                    f"not stranded", job="conclusion")
            # Keep the prose: it is the only record of what the model actually
            # decided, and a run that spent research budget should not vanish
            # from the digest just because its reply broke the contract.
            G.stage("events", {
                "kind": "contract",
                "loop": args.loop,
                "detail": ("dream reply was not valid JSON; loop returned to "
                           "open and will be retried. First 300 chars: "
                           + " ".join(raw.split())[:300]),
            })
        except Exception as exc:  # never let cleanup mask the original failure
            log(f"WARN could not reset {args.loop}: {exc}", job="conclusion")
        return 1
    print(json.dumps(apply(args.loop, payload), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
