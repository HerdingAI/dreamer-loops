#!/usr/bin/env python3
"""Frozen eval for the frontier judges.

Cases live in a directory of {tune,holdout}/*.json (default evals/frontier, which the
owner builds from their own corpus; --eval-dir points elsewhere, and
tests/fixtures/frontier/eval/ is a small synthetic set). Prompts are tuned against
`tune/` only; `holdout/` must pass before release and after any change to a
frontier PROMPT.md. Passing means: no known candidate is judged never-discussed,
every tactical candidate is judged tactical, every seeded bad lesson is rejected (and the
good-lesson controls pass: a reviewer that rejects everything proves nothing), and the
controlled transcript cases (shown-before, assistant-only, other-words) get their labelled verdict.

LIVE runs call the real model and real qmd. They are never run by the tests or CI;
`--dry-run` only validates the case files and the deterministic split.

  python3 scripts/frontier_eval.py --dry-run
  python3 scripts/frontier_eval.py --half holdout
  python3 scripts/frontier_eval.py --half both --eval-dir tests/fixtures/frontier/eval
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import ROOT  # noqa: E402

EVAL_DIR = ROOT / "evals" / "frontier"
HALVES = ("tune", "holdout")
KINDS = {"known": "check", "unowned": "check", "tactical": "consequence", "exposed": "check",
         "assistant_only": "check", "other_words": "check",
         "bad_lesson": "lesson-check", "good_lesson": "lesson-check"}
# Kinds judged against the owner's real corpus; every other check kind carries a fixture.
LIVE_KINDS = ("known", "unowned")
CHECK_EXPECT = {"owner_discussed_not", "owner_discussed_in", "exposed"}
OWNER_VERDICTS = ("none", "asked", "explained", "reconstructed")


class CaseError(ValueError):
    pass


def split_of(ids: list[str], index: int) -> str:
    """The deterministic split: within a kind, ids sorted, even positions go to
    tune and odd positions to holdout."""
    return "holdout" if index % 2 else "tune"


def load_cases(eval_dir: Path = EVAL_DIR) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for half in HALVES:
        out[half] = []
        for path in sorted((eval_dir / half).glob("*.json")):
            try:
                case = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise CaseError(f"{path}: not JSON: {exc}") from None
            if not isinstance(case, dict):
                raise CaseError(f"{path}: not an object")
            case["_path"] = path
            out[half].append(case)
    return out


def _need(case: dict, key: str, typ) -> object:
    v = case.get(key)
    if not isinstance(v, typ) or (isinstance(v, (str, dict, list)) and not v):
        raise CaseError(f"{case['_path']}: {key!r} must be a non-empty {typ.__name__}")
    return v


def validate(cases: dict[str, list[dict]]) -> list[str]:
    """Problems with the case files (empty when they are fine)."""
    problems: list[str] = []
    seen: set[str] = set()
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for half, lst in cases.items():
        for case in lst:
            try:
                cid = str(_need(case, "id", str))
                kind = str(_need(case, "kind", str))
                if cid in seen:
                    raise CaseError(f"{case['_path']}: duplicate id {cid}")
                seen.add(cid)
                if kind not in KINDS:
                    raise CaseError(f"{case['_path']}: kind {kind!r} is not one of {sorted(KINDS)}")
                if case.get("call") != KINDS[kind]:
                    raise CaseError(f"{case['_path']}: kind {kind} needs call {KINDS[kind]!r}")
                if case["_path"].stem != cid:
                    raise CaseError(f"{case['_path']}: file name must be the id")
                _need(case, "source", str)
                cand = _need(case, "candidate", dict)
                for k in ("title", "summary"):
                    _need({"_path": case["_path"], **cand}, k, str)
                expect = _need(case, "expect", dict)
                if KINDS[kind] == "lesson-check":
                    _need(case, "cause", str)
                    for k in ("title", "body"):
                        _need({"_path": case["_path"], **_need(case, "lesson", dict)}, k, str)
                    sources = _need(case, "sources", list)
                    ids = {s.get("id") for s in sources}
                    for c in _need(case, "claims", list):
                        for sp in c.get("spans") or []:
                            if sp.get("source") not in ids:
                                raise CaseError(f"{case['_path']}: a span names an unknown source")
                    if expect not in ({"verdict": "pass"}, {"verdict": "reject"}):
                        raise CaseError(f"{case['_path']}: expect must be {{verdict: pass|reject}}")
                    if (kind == "good_lesson") != (expect["verdict"] == "pass"):
                        raise CaseError(f"{case['_path']}: good lessons pass, bad lessons reject")
                elif KINDS[kind] == "consequence":
                    goal = _need(case, "goal", dict)
                    for k in ("id", "title", "finish_line", "why"):
                        _need({"_path": case["_path"], **goal}, k, str)
                    _need({"_path": case["_path"], **goal}, "plan", list)
                    _need(case, "cause", str)
                    if expect != {"verdict": "tactical"} and expect.get("verdict") not in ("consequential", "tactical"):
                        raise CaseError(f"{case['_path']}: expect.verdict must be consequential or tactical")
                else:
                    if not set(expect) or set(expect) - CHECK_EXPECT:
                        raise CaseError(f"{case['_path']}: expect keys must be within {sorted(CHECK_EXPECT)}")
                    for k in ("owner_discussed_not", "owner_discussed_in"):
                        if k in expect and (not isinstance(expect[k], list)
                                            or not set(expect[k]) <= set(OWNER_VERDICTS)):
                            raise CaseError(f"{case['_path']}: expect.{k} must list owner verdicts")
                    if kind == "unowned" and expect != {"owner_discussed_in": ["none"]}:
                        raise CaseError(f"{case['_path']}: an unowned case expects owner_discussed_in [none]")
                    if kind not in LIVE_KINDS:
                        fx = _need(case, "fixture", dict)
                        _need({"_path": case["_path"], **fx}, "transcript", str)
                        needle = _need({"_path": case["_path"], **fx}, "hit_line_contains", str)
                        if needle not in fx["transcript"]:
                            raise CaseError(f"{case['_path']}: hit_line_contains is not in the transcript")
                        if "## Human" not in fx["transcript"]:
                            raise CaseError(f"{case['_path']}: the fixture transcript has no Human turn")
                # A relabelled case keeps the half it was born in: the split groups it
                # under split_kind, so a label fixed after a run never moves a case
                # between tune and holdout. The relabel says why, with its evidence.
                split_kind = case.get("split_kind", kind)
                if "split_kind" in case:
                    if split_kind not in KINDS:
                        raise CaseError(f"{case['_path']}: split_kind {split_kind!r} is not a kind")
                    _need(case, "relabel", str)
                by_kind.setdefault(split_kind, []).append((cid, half))
            except CaseError as exc:
                problems.append(str(exc))
    for kind, lst in by_kind.items():
        for i, (cid, half) in enumerate(sorted(lst)):
            if half != split_of([c for c, _ in lst], i):
                problems.append(f"{cid}: kind {kind} puts it in {half}/ but the deterministic split says "
                                f"{split_of([c for c, _ in lst], i)}/")
    return problems


# --------------------------------------------------------------------------
# Live run
# --------------------------------------------------------------------------

class _StubQmd:
    """Retrieval for a fixture case: one hit on the fixture transcript, nothing else."""

    def __init__(self, hit: dict):
        self._hit = hit

    def query(self, text, collection, limit=None, lex_only=False):
        # The title search adds nothing: a fixture case has exactly one chat.
        return ([self._hit] if collection == "transcripts" and not lex_only else []), False


@contextlib.contextmanager
def _fixture_env(case: dict):
    import dreamer_common as dc
    tmp = Path(tempfile.mkdtemp(prefix="frontier-eval-"))
    saved = dict(dc.CFG["paths"])
    try:
        for k in ("vault", "sources", "conclusions", "frontier", "meta", "logs"):
            (tmp / k).mkdir(parents=True, exist_ok=True)
        (tmp / "frontier" / "items").mkdir(exist_ok=True)
        dc.CFG["paths"].update({k: str(tmp / k) for k in ("vault", "sources", "conclusions", "frontier", "meta")})
        dc.CFG["paths"]["logs"] = str(saved["logs"])
        path = tmp / "sources" / "2030" / "01" / f"2030-01-06--{case['id']}.md"
        path.parent.mkdir(parents=True)
        text = case["fixture"]["transcript"]
        path.write_text(text, encoding="utf-8")
        line = next(i for i, l in enumerate(text.splitlines(), 1)
                    if case["fixture"]["hit_line_contains"] in l)
        yield _StubQmd({"docid": "#eval", "file": f"transcripts/2030/01/{path.name}", "line": line,
                        "title": "", "score": 1.0, "snippet": ""})
    finally:
        dc.CFG["paths"].clear()
        dc.CFG["paths"].update(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def _judge_check(case: dict) -> dict:
    import frontier_gaps as FG
    cand = case["candidate"]
    if "fixture" in case:
        with _fixture_env(case) as qmd:
            return FG.check_candidate(cand["title"], cand["summary"], qmd)
    from qmd_client import QmdClient
    with QmdClient() as qmd:
        return FG.check_candidate(cand["title"], cand["summary"], qmd)


def _judge_consequence(case: dict) -> dict:
    import frontier_gaps as FG
    import goals as G
    g = case["goal"]
    goal = G.Goal(id=g["id"], title=g["title"], finish_line=g["finish_line"], why=g["why"])
    goal.body = G.default_goal_body(goal, g["plan"])
    cand = case["candidate"]
    return FG.judge_consequence(goal, case["cause"], cand["title"], cand["summary"])


def _judge_lesson(case: dict) -> dict:
    """A seeded lesson through the same two reviewers a real one meets: the independent
    check and the code gates. It is rejected when either says so."""
    import frontier_lessons as FL
    import verify_passages as VP
    by_id = {s["id"]: VP.Source(id=s["id"], kind=s["kind"], title=s["title"], text="",
                                url=s.get("url", ""), ref=s.get("ref", "")) for s in case["sources"]}
    n_of = FL._number_sources(case["claims"], by_id)
    lesson = case["lesson"]
    needs_owner = any(s["kind"] == "owner" for s in case["sources"])
    reply = FL.judge_lesson(case["candidate"]["title"], case["cause"], "new", needs_owner, "",
                            lesson["title"], lesson["body"], case["claims"], n_of, by_id)
    code = FL.gate_body(lesson["title"], lesson["body"], case["claims"],
                        {n: by_id[sid].kind for sid, n in n_of.items()}, needs_owner=needs_owner)
    rejected = reply["verdict"] != "pass" or bool(code)
    return {"verdict": "reject" if rejected else "pass",
            "reasons": reply["reasons"] + code}


def score(case: dict, got: dict) -> str | None:
    """None when the verdict meets the label, else why not."""
    expect = case["expect"]
    if case["call"] == "lesson-check":
        return None if got["verdict"] == expect["verdict"] else \
            f"judged {got['verdict']!r} ({'; '.join(got['reasons'])}), labelled {expect['verdict']!r}"
    if case["call"] == "consequence":
        return None if got["verdict"] == expect["verdict"] else \
            f"judged {got['verdict']!r} ({got['reason']}), labelled {expect['verdict']!r}"
    if got["status"] != "ok":
        return f"inconclusive: {got['reason']}"
    verdict = got["owner_discussed"]
    if "owner_discussed_not" in expect and verdict in expect["owner_discussed_not"]:
        return f"owner_discussed {verdict!r} is one the label forbids"
    if "owner_discussed_in" in expect and verdict not in expect["owner_discussed_in"]:
        return f"owner_discussed {verdict!r}, labelled one of {expect['owner_discussed_in']}"
    if expect.get("exposed") and not got["exposed"]:
        return "no exposure reported"
    return None


def format_table(results: list[tuple[dict, str | None]]) -> str:
    """One row per case: RESULT, KIND, CASE, then why it failed (blank when it passed)."""
    width_kind = max([len("KIND")] + [len(c["kind"]) for c, _ in results])
    width_id = max([len("CASE")] + [len(c["id"]) for c, _ in results])
    rows = [f"{'RESULT':<6} {'KIND':<{width_kind}} {'CASE':<{width_id}} DETAIL"]
    for case, why in results:
        rows.append(f"{'PASS' if why is None else 'FAIL':<6} {case['kind']:<{width_kind}} "
                    f"{case['id']:<{width_id}} {why or ''}".rstrip())
    return "\n".join(rows)


def run_live(cases: list[dict]) -> list[tuple[dict, str | None]]:
    import frontier_llm as L
    out = []
    for case in cases:
        try:
            got = {"check": _judge_check, "consequence": _judge_consequence,
                   "lesson-check": _judge_lesson}[case["call"]](case)
            why = score(case, got)
        except (L.ParseFailure, L.PromptTooLarge) as exc:
            why = f"unparseable: {exc}"
        out.append((case, why))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Frontier judge eval (live; never run in CI)")
    ap.add_argument("--half", choices=[*HALVES, "both"], default="holdout")
    ap.add_argument("--eval-dir", type=Path, default=EVAL_DIR,
                    help="directory holding tune/ and holdout/ case files (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="validate the case files only; no model, no qmd")
    args = ap.parse_args(argv)
    cases = load_cases(args.eval_dir)
    problems = validate(cases)
    counts = {h: len(v) for h, v in cases.items()}
    if not any(counts.values()):
        problems.append(f"no case files under {args.eval_dir}/(tune|holdout); build some or pass --eval-dir")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps({"ok": True, "cases": counts}))
        return 0
    chosen = [c for h in (HALVES if args.half == "both" else (args.half,)) for c in cases[h]]
    results = run_live(chosen)
    print(format_table(results))
    failed = [c["id"] for c, why in results if why]
    print(json.dumps({"half": args.half, "cases": len(results), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
