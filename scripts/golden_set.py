#!/usr/bin/env python3
"""Golden-set runner for DoD 6.6 — replay owner-labelled pairs through Stage B.

Before this existed, `golden-set.json` was a scaffold with no scorer: the owner
could label all 20 pairs and the DoD still could not close, because nothing read
the labels. A fixture with no runner cannot regress anything.

Two judges, deliberately:

  llm      — the real Stage-B judgment via `claude -p`. This is what the DoD
             grades, because it is what runs in production.
  lexical  — token-overlap on titles, no model. Not a substitute; a control.
             Running both answers open question Q9: if the lexical baseline
             matches the LLM judge on the golden set, the judge is not earning
             its cost on this distribution.

Accuracy is reported stratified by `source` (sampled vs adversarial), because
pairs sampled from the extractor's own output cannot contain the cases it never
generated — they measure the easy region and read high. The spec requires >=5
owner-authored adversarial pairs for exactly this reason.

Usage:
    golden_set.py validate                 # can this even be scored?
    golden_set.py run --judge lexical
    golden_set.py run --judge llm
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, atomic_write_json, log, p, read_json, today  # noqa: E402
import vault as V  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GATE = 0.80
MIN_ADVERSARIAL = 5
LEXICAL_THRESHOLD = 0.55  # same band merge_proposals uses


def load() -> dict:
    data = read_json(p("meta") / "golden-set.json", default=None)
    if not data:
        raise SystemExit("no golden-set.json — run `calibrate.py golden` first")
    return data


def pair_titles(pair: dict) -> tuple[str, str]:
    """Resolve each side to a title. Hand-written adversarial pairs carry
    titles with no loop id; sampled pairs carry both."""
    loops = {l.id: l for l in V.load_loops(include_archived=True)}

    def side(key: str) -> str:
        lid = (pair.get(key) or "").strip()
        if lid and lid in loops:
            return loops[lid].title
        return (pair.get(f"{key}_title") or "").strip()

    return side("a"), side("b")


# --------------------------------------------------------------------------
# Validation — refuse to produce a number that would be misleading
# --------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"<[^>]{1,60}>")


def _is_template(pair: dict) -> bool:
    """True if either side still carries scaffold placeholder text.

    Checking `startswith('<')` is not enough: the archetype rows embed the
    placeholder mid-string ("Should we adopt <X>?"), which would otherwise be
    counted as owner-authored and let an unedited scaffold pass the gate.
    """
    for key in ("a_title", "b_title"):
        if _TEMPLATE_RE.search(str(pair.get(key, ""))):
            return True
    return False


def validate(data: dict) -> tuple[bool, list[str]]:
    pairs = data.get("pairs") or []
    problems: list[str] = []

    labelled = [x for x in pairs
                if str(x.get("owner_label", "")).strip().lower() in ("same", "distinct")]
    unlabelled = len(pairs) - len(labelled)
    if unlabelled:
        problems.append(f"{unlabelled} of {len(pairs)} pairs are unlabelled "
                        f"(owner_label must be 'same' or 'distinct')")

    if len(pairs) < 20:
        problems.append(f"{len(pairs)} pairs; the DoD specifies 20")

    same = sum(1 for x in labelled if x["owner_label"].lower() == "same")
    distinct = len(labelled) - same
    if labelled and (same < 8 or distinct < 8):
        problems.append(f"balance is {same} same / {distinct} distinct; "
                        f"the DoD specifies 10 and 10")

    adversarial = [x for x in pairs if x.get("source") == "adversarial"]
    hand_written = [x for x in adversarial if not _is_template(x)]
    if len(hand_written) < MIN_ADVERSARIAL:
        problems.append(
            f"{len(hand_written)} hand-written adversarial pairs; the DoD "
            f"specifies >={MIN_ADVERSARIAL}. Placeholder rows still contain the "
            f"'<hand-written near-miss>' template text.")

    for i, x in enumerate(pairs):
        a, b = pair_titles(x)
        if not a or not b or _is_template({"a_title": a, "b_title": b}):
            problems.append(f"pair {i}: one or both sides is empty or still "
                            f"carries scaffold placeholder text")

    return (not problems), problems


# --------------------------------------------------------------------------
# Judges
# --------------------------------------------------------------------------

def judge_lexical(a: str, b: str) -> dict:
    from merge_proposals import similarity
    s = similarity(a, b)
    return {"verdict": "same" if s >= LEXICAL_THRESHOLD else "distinct",
            "confidence": "high" if abs(s - LEXICAL_THRESHOLD) > 0.2 else "low",
            "reason": f"token overlap {s:.0%}"}


def judge_llm(a: str, b: str) -> dict:
    template = (ROOT / "skills" / "judge" / "PROMPT.md").read_text(encoding="utf-8")
    prompt = f"{template}\n\n- **A**: {a}\n- **B**: {b}\n\nReturn the JSON now."
    fake = os.environ.get("DREAMER_FAKE_CLAUDE")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
        tf.write(prompt)
        prompt_path = tf.name
    try:
        if fake:
            r = subprocess.run([fake, prompt_path], capture_output=True,
                               text=True, timeout=120)
            raw = r.stdout
        else:
            r = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json",
                 "--max-turns", "4"],
                capture_output=True, text=True, timeout=600, cwd=str(ROOT))
            if r.returncode != 0:
                return {"verdict": "error", "confidence": "low",
                        "reason": f"claude exited {r.returncode}"}
            try:
                raw = json.loads(r.stdout).get("result", "")
            except json.JSONDecodeError:
                raw = r.stdout
    finally:
        os.unlink(prompt_path)

    from apply_extraction import _extract_json
    obj = _extract_json(raw) or {}
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in ("same", "distinct"):
        return {"verdict": "error", "confidence": "low",
                "reason": f"unparseable verdict {verdict!r}"}
    return {"verdict": verdict,
            "confidence": obj.get("confidence", "unknown"),
            "reason": obj.get("reason", "")}


JUDGES = {"lexical": judge_lexical, "llm": judge_llm}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def run(judge_name: str, *, strict: bool = True) -> dict:
    data = load()
    ok, problems = validate(data)
    if strict and not ok:
        for x in problems:
            log(f"BLOCKED: {x}", job="golden")
        return {"scored": False, "problems": problems,
                "note": "refusing to report an accuracy number from an "
                        "incomplete golden set — a partial score would read as "
                        "a result"}

    judge = JUDGES[judge_name]
    rows, errors = [], 0
    for pair in data["pairs"]:
        label = str(pair.get("owner_label", "")).strip().lower()
        if label not in ("same", "distinct"):
            continue
        a, b = pair_titles(pair)
        out = judge(a, b)
        if out["verdict"] == "error":
            errors += 1
            continue
        rows.append({"a": a[:70], "b": b[:70], "label": label,
                     "verdict": out["verdict"], "agree": out["verdict"] == label,
                     "source": pair.get("source", "sampled"),
                     "reason": out.get("reason", "")})

    def acc(subset):
        return (sum(1 for r in subset if r["agree"]) / len(subset)) if subset else None

    sampled = [r for r in rows if r["source"] != "adversarial"]
    adversarial = [r for r in rows if r["source"] == "adversarial"]
    overall = acc(rows)

    # Confusion, in the direction that matters: a false merge is the dangerous
    # error, because nothing downstream detects it.
    false_merge = sum(1 for r in rows
                      if r["label"] == "distinct" and r["verdict"] == "same")
    false_split = sum(1 for r in rows
                      if r["label"] == "same" and r["verdict"] == "distinct")

    result = {
        "scored": True, "judge": judge_name, "date": today().isoformat(),
        "n": len(rows), "errors": errors,
        "accuracy_overall": overall,
        "accuracy_sampled": acc(sampled),
        "accuracy_adversarial": acc(adversarial),
        "n_sampled": len(sampled), "n_adversarial": len(adversarial),
        "false_merge": false_merge, "false_split": false_split,
        "gate": GATE,
        "passes": bool(overall is not None and overall >= GATE),
        "rows": rows,
    }
    atomic_write_json(p("meta") / f"golden-set-result-{judge_name}.json", result)
    return result


def report(result: dict) -> None:
    if not result.get("scored"):
        print("NOT SCOREABLE")
        for x in result["problems"]:
            print(f"  - {x}")
        print(f"\n{result['note']}")
        return
    pct = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
    print(f"judge: {result['judge']}   pairs scored: {result['n']}"
          f"   errors: {result['errors']}")
    print(f"  overall     {pct(result['accuracy_overall'])}  "
          f"(gate {result['gate']:.0%}) "
          f"{'PASS' if result['passes'] else 'FAIL'}")
    print(f"  sampled     {pct(result['accuracy_sampled'])}  "
          f"n={result['n_sampled']}")
    print(f"  adversarial {pct(result['accuracy_adversarial'])}  "
          f"n={result['n_adversarial']}   <- the number that matters")
    print(f"  false merges {result['false_merge']}  "
          f"(dangerous: nothing downstream detects these)")
    print(f"  false splits {result['false_split']}  "
          f"(self-healing via merge proposals)")
    for r in result["rows"]:
        if not r["agree"]:
            print(f"  MISS [{r['source']}] label={r['label']} "
                  f"verdict={r['verdict']}\n       A: {r['a']}\n       B: {r['b']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["validate", "run"])
    ap.add_argument("--judge", default="lexical", choices=sorted(JUDGES))
    ap.add_argument("--allow-partial", action="store_true",
                    help="score anyway; result is indicative, not a gate")
    args = ap.parse_args()

    if args.command == "validate":
        ok, problems = validate(load())
        print("READY TO SCORE" if ok else "NOT SCOREABLE")
        for x in problems:
            print(f"  - {x}")
        return 0 if ok else 1

    result = run(args.judge, strict=not args.allow_partial)
    report(result)
    return 0 if result.get("passes") else 1


if __name__ == "__main__":
    raise SystemExit(main())
