#!/usr/bin/env python3
"""Goal mechanics: state machine, catalog, detectors, plan revision (§3, §4).

Goals are committed intentions owner-authored into vault/goals/<id>.md.
Everything here is deterministic Python arithmetic and state transitions.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, as_date, atomic_write, atomic_write_json, log, p,
    read_page, today, write_page, state_lock,
)
import vault as V  # noqa: E402

GOAL_STATUSES = {"active", "paused", "achieved", "abandoned"}
CATALOG = "_catalog.md"
_GOAL_ID_RE = re.compile(r"^G(\d{4,})$")
ASSESSMENT_HEADING = "## Assessment (derived — hypothesis, not evidence)"
_ASSESSMENT_SECTION = re.compile(
    rf"^{re.escape(ASSESSMENT_HEADING)}\s*$(.*?)(?=^## |\Z)", re.M | re.S
)

_NUMERIC_PROGRESS_RE = re.compile(
    r"(?:progress|percentage|percent|pct|score|completion_rate|streak)\s*:\s*\d+",
    re.IGNORECASE,
)
_DONE_STEP_RE = re.compile(
    r"^-\s*\[[xX]\]\s*(?:(\d{4}-\d{2}-\d{2})\s*—\s*)?(.+?)(?:\s*—\s*(\[\[[^\]]+\]\]|assertion:[^\n]+))?$"
)


@dataclass
class Goal:
    id: str
    title: str
    status: str = "active"
    created: _dt.date | None = None
    finish_line: str = ""
    target_date: _dt.date | None = None
    why: str = ""
    authorized_by: dict = field(default_factory=dict)
    last_mention: _dt.date | None = None
    last_movement: _dt.date | None = None
    attachments: list[str] = field(default_factory=list)
    loops: list[str] = field(default_factory=list)
    plan_revision: int = 1
    tags: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None
    type: str = "goal"

    @classmethod
    def from_path(cls, path: Path) -> "Goal":
        fm, body = read_page(path)
        auth = fm.get("authorized_by") or {}
        if isinstance(auth, str):
            auth = {"quote": auth, "session": "", "verified": None}
        return cls(
            id=str(fm.get("id", "")),
            title=str(fm.get("title", "")),
            status=str(fm.get("status", "active")),
            created=as_date(fm.get("created")),
            finish_line=str(fm.get("finish_line", "")),
            target_date=as_date(fm.get("target_date")),
            why=str(fm.get("why", "")),
            authorized_by=dict(auth),
            last_mention=as_date(fm.get("last_mention")),
            last_movement=as_date(fm.get("last_movement")),
            attachments=list(fm.get("attachments") or []),
            loops=list(fm.get("loops") or []),
            plan_revision=int(fm.get("plan_revision") or 1),
            tags=list(fm.get("tags") or []),
            body=body,
            path=path,
            type=str(fm.get("type") or "goal"),
        )

    def frontmatter(self) -> dict:
        return {
            "type": "goal",
            "id": self.id,
            "status": self.status,
            "title": self.title,
            "created": self.created.isoformat() if self.created else None,
            "finish_line": self.finish_line,
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "why": self.why,
            "authorized_by": self.authorized_by,
            "last_mention": self.last_mention.isoformat() if self.last_mention else None,
            "last_movement": self.last_movement.isoformat() if self.last_movement else None,
            "attachments": self.attachments,
            "loops": self.loops,
            "plan_revision": self.plan_revision,
            "tags": self.tags,
        }

    def save(self) -> None:
        if self.status not in GOAL_STATUSES:
            raise ValueError(f"{self.id}: illegal goal status {self.status!r}")
        if self.path is None:
            self.path = goals_dir() / f"{self.id}.md"
        write_page(self.path, self.frontmatter(), self.body or default_goal_body(self))


def goals_dir() -> Path:
    d = p("goals")
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_goals() -> list[Goal]:
    out: list[Goal] = []
    for path in sorted(goals_dir().glob("*.md")):
        if path.name == CATALOG:
            continue
        try:
            g = Goal.from_path(path)
        except Exception as exc:  # noqa: BLE001
            log(f"LINT skip unreadable goal {path.name}: {exc}", job="goals")
            continue
        if not g.id or g.type != "goal":
            continue
        out.append(g)
    return out


def load_goal(goal_id: str) -> Goal | None:
    path = goals_dir() / f"{goal_id}.md"
    if not path.exists():
        return None
    try:
        g = Goal.from_path(path)
    except Exception as exc:  # noqa: BLE001
        log(f"LINT skip unreadable goal {path.name}: {exc}", job="goals")
        return None
    if g.id != goal_id or g.type != "goal":
        return None
    return g


def next_goal_id() -> str:
    highest = 0
    for path in goals_dir().glob("*.md"):
        if path.name == CATALOG:
            continue
        try:
            fm, _ = read_page(path)
            m = _GOAL_ID_RE.match(str(fm.get("id", "")))
            if m:
                highest = max(highest, int(m.group(1)))
        except Exception:  # noqa: BLE001
            continue
    return f"G{highest + 1:04d}"


def assessment_section(body: str) -> str | None:
    m = _ASSESSMENT_SECTION.search(body or "")
    return m.group(1).strip() if m else None


def replace_assessment_section(body: str, content: str) -> str:
    section = f"{ASSESSMENT_HEADING}\n\n{content.strip()}\n\n"
    if not body:
        return section
    if _ASSESSMENT_SECTION.search(body):
        return _ASSESSMENT_SECTION.sub(lambda _m: section, body, count=1)
    return body.rstrip() + f"\n\n{section}"


def format_plan_steps(steps: list[str]) -> str:
    lines = []
    for s in steps:
        s_clean = s.strip()
        if not s_clean.startswith("- "):
            s_clean = f"- [ ] {s_clean}"
        lines.append(s_clean)
    return "\n".join(lines)


def default_goal_body(goal: Goal, plan_steps: list[str] | None = None) -> str:
    steps_txt = format_plan_steps(plan_steps or ["Initial step"])
    return (
        f"# {goal.title}\n\n"
        "## Goal\n\n"
        f"{goal.title}\n\n"
        "## Finish line\n\n"
        f"{goal.finish_line or '_unspecified_'}\n\n"
        "## Why (stakes)\n\n"
        f"{goal.why or '_unspecified_'}\n\n"
        f"## Plan (revision {goal.plan_revision})\n\n"
        f"{steps_txt}\n\n"
        "## Plan history\n\n"
        "_No prior revisions._\n\n"
        "## Assessment (derived — hypothesis, not evidence)\n\n"
        f"**Now**\n\nGoal committed on {goal.created.isoformat() if goal.created else today().isoformat()}.\n\n"
        "**Trajectory**\n\n"
        f"- {goal.created.isoformat() if goal.created else today().isoformat()} — Goal committed — quote: \"{goal.authorized_by.get('quote', '')}\"\n\n"
        "## Owner assertions\n\n"
        f"- {goal.created.isoformat() if goal.created else today().isoformat()} — Goal authorized: \"{goal.authorized_by.get('quote', '')}\" (session: {goal.authorized_by.get('session', '')})\n"
    )


def active_goals_count() -> int:
    return sum(1 for g in load_goals() if g.status == "active")


def max_active_goals() -> int:
    cfg_g = CFG.get("goals") or {}
    return int(cfg_g.get("max_active", 4))


# --------------------------------------------------------------------------
# State transitions & operations
# --------------------------------------------------------------------------

def commit_goal(
    title: str,
    finish_line: str,
    target_date: _dt.date | str | None,
    why: str,
    plan_steps: list[str],
    owner_quote: str,
    session_id: str,
    tags: list[str] | None = None,
    loops: list[str] | None = None,
    ref: _dt.date | None = None,
) -> Goal:
    ref = ref or today()
    active_count = active_goals_count()
    max_active = max_active_goals()
    if active_count >= max_active:
        raise ValueError(
            f"Cannot commit goal: {active_count} active goals already reach the cap of {max_active}. "
            "Pause an existing goal first to make room."
        )

    t_date = as_date(target_date) if isinstance(target_date, str) else target_date
    goal = Goal(
        id=next_goal_id(),
        title=title.strip(),
        status="active",
        created=ref,
        finish_line=finish_line.strip(),
        target_date=t_date,
        why=why.strip(),
        authorized_by={"quote": owner_quote.strip(), "session": session_id.strip(), "verified": None},
        last_mention=ref,
        last_movement=ref,
        attachments=[],
        loops=loops or [],
        plan_revision=1,
        tags=tags or [],
    )
    goal.body = default_goal_body(goal, plan_steps)
    goal.save()
    regenerate_catalog()
    return goal


def set_goal_status(
    goal: Goal,
    new_status: str,
    owner_quote: str,
    session_id: str,
    ref: _dt.date | None = None,
) -> Goal:
    ref = ref or today()
    old_status = goal.status
    if new_status not in GOAL_STATUSES:
        raise ValueError(f"Illegal status: {new_status!r}")

    # Valid transitions (§3.5):
    # active -> paused | achieved | abandoned
    # paused -> active | abandoned
    # active/paused -> abandoned
    allowed = {
        "active": {"paused", "achieved", "abandoned"},
        "paused": {"active", "abandoned"},
        "achieved": set(),
        "abandoned": set(),
    }
    if new_status not in allowed.get(old_status, set()):
        raise ValueError(
            f"Illegal transition for {goal.id}: from {old_status!r} to {new_status!r}"
        )

    if new_status == "active" and old_status != "active":
        active_count = active_goals_count()
        max_active = max_active_goals()
        if active_count >= max_active:
            raise ValueError(
                f"Cannot resume goal {goal.id}: {active_count} active goals already reach the cap of {max_active}."
            )

    goal.status = new_status
    # Append assertion
    assertion_line = f"- {ref.isoformat()} — Status changed to {new_status}: \"{owner_quote}\" (session: {session_id})\n"
    if "## Owner assertions" in goal.body:
        goal.body = goal.body.rstrip() + f"\n{assertion_line}"
    else:
        goal.body = goal.body.rstrip() + f"\n\n## Owner assertions\n\n{assertion_line}"

    goal.save()
    regenerate_catalog()
    return goal


def revise_plan(
    goal: Goal,
    new_steps: list[str],
    owner_quote: str,
    session_id: str,
    ref: _dt.date | None = None,
) -> Goal:
    ref = ref or today()
    current_rev = goal.plan_revision
    next_rev = current_rev + 1

    # Extract current plan section
    plan_heading_re = re.compile(
        rf"^## Plan \(revision {current_rev}\)\s*$(.*?)(?=^## |\Z)", re.M | re.S
    )
    m = plan_heading_re.search(goal.body)
    old_plan_content = m.group(1).strip() if m else ""

    history_entry = (
        f"### Revision {current_rev} (superseded {ref.isoformat()})\n\n"
        f"{old_plan_content}\n\n"
        f"- Superseded by revision {next_rev}: \"{owner_quote}\" (session: {session_id})\n"
    )

    # Replace or append to plan history
    history_heading = "## Plan history"
    history_re = re.compile(
        rf"^{re.escape(history_heading)}\s*$(.*?)(?=^## |\Z)", re.M | re.S
    )
    hm = history_re.search(goal.body)
    if hm:
        current_history = hm.group(1).strip()
        if current_history == "_No prior revisions._":
            new_history = f"{history_heading}\n\n{history_entry}"
        else:
            new_history = f"{history_heading}\n\n{history_entry}\n\n{current_history}"
        body = history_re.sub(lambda _x: new_history + "\n\n", goal.body, count=1)
    else:
        body = goal.body.rstrip() + f"\n\n{history_heading}\n\n{history_entry}\n"

    # A step already done stays done when it is carried into the new plan,
    # even reworded: the date and citation move with it. Otherwise a routine
    # rewrite would silently discard cited progress.
    done = done_steps(goal)
    carried = []
    for step in new_steps:
        if re.match(r"^-\s*\[[xX]\]", step.strip()):
            carried.append(step)
            continue
        text = _step_text(step) if step.strip().startswith("- ") else step.strip()
        match = next((line for old, line in done if _same_step(old, text)), None)
        if match:
            date_part = re.match(r"^-\s*\[[xX]\]\s*(\d{4}-\d{2}-\d{2})", match)
            cit = re.search(r"—\s*(\[\[.+?\]\]|assertion:[^\n]+)$", match)
            if date_part and cit:
                carried.append(f"- [x] {date_part.group(1)} — {text} — {cit.group(1)}")
                continue
        carried.append(step)
    new_steps = carried

    # Replace plan section
    new_steps_txt = format_plan_steps(new_steps)
    new_plan_section = f"## Plan (revision {next_rev})\n\n{new_steps_txt}\n\n"
    if m:
        body = plan_heading_re.sub(lambda _x: new_plan_section, body, count=1)
    else:
        body = body.rstrip() + f"\n\n{new_plan_section}"

    goal.plan_revision = next_rev
    goal.body = body
    goal.save()
    regenerate_catalog()
    return goal


def complete_step(
    goal: Goal | str,
    step_num_or_text: int | str,
    citation: str,
    owner_quote: str,
    session_id: str,
    ref: _dt.date | None = None,
) -> Goal:
    """Mark a step done. Requires citation."""
    if isinstance(goal, str):
        g_obj = load_goal(goal)
        if g_obj is None:
            raise ValueError(f"Goal {goal} not found")
        goal = g_obj
    if not citation or not citation.strip():
        raise ValueError("A step is never marked done without a citation (§3.4, rule 8 applied to progress).")
    ref = ref or today()
    date_str = ref.isoformat()
    rev = goal.plan_revision

    plan_re = re.compile(
        rf"^## Plan \(revision {rev}\)\s*$(.*?)(?=^## |\Z)", re.M | re.S
    )
    pm = plan_re.search(goal.body)
    if not pm:
        raise ValueError(f"Plan (revision {rev}) not found in goal {goal.id}")

    plan_lines = pm.group(1).strip().splitlines()
    step_indices = [i for i, ln in enumerate(plan_lines) if ln.strip().startswith("- ")]
    target_idx = None

    if isinstance(step_num_or_text, int) or (isinstance(step_num_or_text, str) and step_num_or_text.strip().isdigit()):
        step_num = int(step_num_or_text)
        if 1 <= step_num <= len(step_indices):
            target_idx = step_indices[step_num - 1]
        else:
            raise ValueError(f"Step number {step_num} out of range (1..{len(step_indices)})")
    else:
        needle = str(step_num_or_text).lower().strip()
        for idx in step_indices:
            if needle in plan_lines[idx].lower():
                target_idx = idx
                break
        if target_idx is None:
            raise ValueError(f"Could not find step matching {step_num_or_text!r}")

    line = plan_lines[target_idx].strip()
    # Strip existing status checkbox
    clean_step = re.sub(r"^-\s*\[[ xX\-/]\]\s*", "", line)
    # Strip leading completion date if present
    dm = re.match(r"^(\d{4}-\d{2}-\d{2})\s*—\s*(.*)$", clean_step)
    if dm:
        clean_step = dm.group(2)
    # Strip trailing citation if present
    cit_m = re.search(r"\s*—\s*(\[\[.+?\]\]|assertion:[^\n]+)$", clean_step)
    if cit_m:
        clean_step = clean_step[:cit_m.start()]

    # Normalize citation format
    cit = citation.strip()
    if not (cit.startswith("[[") and cit.endswith("]]")) and not cit.startswith("assertion:"):
        cit = f"assertion: {cit}"

    new_line = f"- [x] {date_str} — {clean_step} — {cit}"
    plan_lines[target_idx] = new_line
    new_plan_content = f"## Plan (revision {rev})\n\n" + "\n".join(plan_lines) + "\n\n"

    goal.body = plan_re.sub(lambda _x: new_plan_content, goal.body, count=1)
    # Update last_movement and last_mention monotonically
    if goal.last_movement is None or ref > goal.last_movement:
        goal.last_movement = ref
    if goal.last_mention is None or ref > goal.last_mention:
        goal.last_mention = ref

    # Append assertion
    assertion_line = f"- {date_str} — Step completed: \"{clean_step}\" (citation: {cit}) — quote: \"{owner_quote}\" (session: {session_id})\n"
    if "## Owner assertions" in goal.body:
        goal.body = goal.body.rstrip() + f"\n{assertion_line}"
    else:
        goal.body = goal.body.rstrip() + f"\n\n## Owner assertions\n\n{assertion_line}"

    goal.save()
    regenerate_catalog()
    return goal


def _step_text(line: str) -> str:
    """A plan line stripped of its checkbox, completion date and citation."""
    text = re.sub(r"^-\s*(\[[ xX\-/]\]\s*)?", "", line.strip())
    dm = re.match(r"^\d{4}-\d{2}-\d{2}\s*—\s*(.*)$", text)
    if dm:
        text = dm.group(1)
    return re.sub(r"\s*—\s*(\[\[.+?\]\]|assertion:[^\n]+)$", "", text).strip()


def _same_step(a: str, b: str) -> bool:
    """Same step, allowing for it having been shortened or extended."""
    a, b = a.lower().rstrip(".: "), b.lower().rstrip(".: ")
    short, long_ = sorted((a, b), key=len)
    return a == b or (len(short) >= 15 and long_.startswith(short))


def done_steps(goal: Goal) -> list[tuple[str, str]]:
    """(text, full line) for each completed step of the current plan."""
    pm = re.search(rf"^## Plan \(revision {goal.plan_revision}\)\s*$(.*?)(?=^## |\Z)",
                   goal.body, re.M | re.S)
    if not pm:
        return []
    return [(_step_text(ln), ln.strip()) for ln in pm.group(1).splitlines()
            if re.match(r"^-\s*\[[xX]\]", ln.strip())]


def open_steps(goal: Goal) -> list[tuple[int, str]]:
    """(1-based step number, text) for every step of the current plan that is
    neither done nor dropped — the numbers complete_step accepts."""
    pm = re.search(rf"^## Plan \(revision {goal.plan_revision}\)\s*$(.*?)(?=^## |\Z)",
                   goal.body, re.M | re.S)
    if not pm:
        return []
    out = []
    steps = [ln.strip() for ln in pm.group(1).splitlines() if ln.strip().startswith("- ")]
    for n, line in enumerate(steps, start=1):
        box = re.match(r"^-\s*\[(.)\]\s*(.*)$", line)
        if box and box.group(1) in "xX-":
            continue
        out.append((n, box.group(2) if box else line[2:].strip()))
    return out


def record_assertion(
    goal: Goal,
    assertion: str,
    kind: str,
    when: _dt.date,
    owner_quote: str,
    session_id: str,
) -> Goal:
    """Record something the owner says happened outside any captured
    conversation (spec §5.2). Evidence tier: it moves a clock and lands under
    `## Owner assertions`; it never touches the plan or the status."""
    if kind not in ("mention", "movement"):
        raise ValueError(f"kind must be 'mention' or 'movement', not {kind!r}")
    if when > today():
        raise ValueError(f"date {when.isoformat()} is in the future")
    if goal.last_mention is None or when > goal.last_mention:
        goal.last_mention = when
    if kind == "movement" and (goal.last_movement is None or when > goal.last_movement):
        goal.last_movement = when
    line = (f"- {when.isoformat()} — Owner update ({kind}): \"{assertion.strip()}\" — "
            f"quote: \"{owner_quote.strip()}\" (session: {session_id})\n")
    if "## Owner assertions" in goal.body:
        goal.body = goal.body.rstrip() + f"\n{line}"
    else:
        goal.body = goal.body.rstrip() + f"\n\n## Owner assertions\n\n{line}"
    goal.save()
    regenerate_catalog()
    return goal


# --------------------------------------------------------------------------
# Catalog (§3.1, §4.3)
# --------------------------------------------------------------------------

def regenerate_catalog() -> Path:
    goals = sorted(load_goals(), key=lambda g: g.id)
    lines = [
        "---",
        "type: catalog",
        "generated: auto — do not hand-edit; regenerated at the end of every job",
        f"count: {len(goals)}",
        "---",
        "",
        "# Goal catalog",
        "",
        "One line per active and tracked goal. Agents read this FIRST (CLAUDE.md rule 11).",
        "",
        "| id | title | status | target_date | last_mention | last_movement | plan_rev | loops |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for g in goals:
        title = g.title.replace("|", "\\|")
        loops_str = ", ".join(g.loops) if g.loops else "—"
        lines.append(
            f"| {g.id} | {title} | {g.status} | {g.target_date or '—'} | "
            f"{g.last_mention or '—'} | {g.last_movement or '—'} | "
            f"{g.plan_revision} | {loops_str} |"
        )
    path = goals_dir() / CATALOG
    atomic_write(path, "\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# The Three Detectors (§4.4)
# --------------------------------------------------------------------------

def silence_detector(goal: Goal, ref: _dt.date | None = None) -> bool:
    """silence(G) := status == active and (today - last_mention) >= goals.silence_ping_days"""
    if goal.status != "active" or goal.last_mention is None:
        return False
    ref = ref or today()
    limit = int((CFG.get("goals") or {}).get("silence_ping_days", 10))
    return (ref - goal.last_mention).days >= limit


def stale_detector(goal: Goal, ref: _dt.date | None = None) -> bool:
    """stale(G) := status == active and (today - last_movement) >= goals.movement_stale_days"""
    if goal.status != "active" or goal.last_movement is None:
        return False
    ref = ref or today()
    limit = int((CFG.get("goals") or {}).get("movement_stale_days", 21))
    return (ref - goal.last_movement).days >= limit


def drift_detector(ref: _dt.date | None = None) -> list[tuple[V.Loop, float]]:
    """drift(L) := L is open, L.id in no active goal's loops list, and
    L.recency_weighted_score() >= goals.drift_recurrence_min.

    Open only: a paused, decision-only or resolved loop already has its
    answer on file, so asking "what is this for?" about it is noise."""
    ref = ref or today()
    limit = float((CFG.get("goals") or {}).get("drift_recurrence_min", 3))
    active_goals = [g for g in load_goals() if g.status == "active"]
    attached_loops = {lid for g in active_goals for lid in g.loops}

    out: list[tuple[V.Loop, float]] = []
    for loop in V.load_loops():
        if loop.status != "open" or loop.id in attached_loops:
            continue
        score = loop.recency_weighted_score(ref)
        if score >= limit:
            out.append((loop, score))
    out.sort(key=lambda item: (-item[1], item[0].id))
    return out


# --------------------------------------------------------------------------
# Lint (§9.2)
# --------------------------------------------------------------------------

def lint(vocabulary: set[str] | None = None) -> list[str]:
    problems: list[str] = []
    goals = load_goals()
    seen_ids: dict[str, Path] = {}
    active_count = 0
    max_active = max_active_goals()
    t = today()

    if vocabulary is None:
        try:
            import propose_tags
            vocabulary = propose_tags.vocabulary()
        except Exception:  # noqa: BLE001
            vocabulary = None

    for g in goals:
        where = g.path.name if g.path else g.id
        if not _GOAL_ID_RE.match(g.id):
            problems.append(f"{where}: malformed goal id {g.id!r}")
        if g.id in seen_ids:
            problems.append(f"{where}: duplicate goal id {g.id}")
        seen_ids[g.id] = g.path or Path(where)

        if g.status not in GOAL_STATUSES:
            problems.append(f"{where}: illegal goal status {g.status!r}")
        if g.status == "active":
            active_count += 1

        if not g.title.strip():
            problems.append(f"{where}: empty title")
        if not g.finish_line.strip():
            problems.append(f"{where}: empty finish_line")
        if not g.why.strip():
            problems.append(f"{where}: empty why")

        if vocabulary is not None:
            for tag in g.tags:
                if tag not in vocabulary:
                    problems.append(f"{where}: out-of-vocabulary tag {tag!r}")

        # Clock sanity
        if g.last_mention and g.last_movement:
            if g.last_movement > g.last_mention:
                problems.append(
                    f"{where}: last_movement ({g.last_movement}) > last_mention ({g.last_mention})"
                )
        if g.last_mention and g.last_mention > t:
            problems.append(f"{where}: last_mention in the future ({g.last_mention})")
        if g.last_movement and g.last_movement > t:
            problems.append(f"{where}: last_movement in the future ({g.last_movement})")
        if g.created:
            if g.last_mention and g.last_mention < g.created:
                problems.append(f"{where}: last_mention ({g.last_mention}) predates created ({g.created})")
            if g.last_movement and g.last_movement < g.created:
                problems.append(f"{where}: last_movement ({g.last_movement}) predates created ({g.created})")

        # Numeric progress prohibition
        raw_text = g.path.read_text(encoding="utf-8") if g.path and g.path.exists() else g.body
        if _NUMERIC_PROGRESS_RE.search(raw_text):
            problems.append(f"{where}: numeric progress field detected (forbidden by §3.2)")

        # Step citations on done steps
        plan_re = re.compile(r"^## Plan \(revision \d+\)\s*$(.*?)(?=^## |\Z)", re.M | re.S)
        pm = plan_re.search(g.body)
        if pm:
            for step_line in pm.group(1).splitlines():
                step_line = step_line.strip()
                if re.match(r"^-\s*\[[xX]\]", step_line):
                    m = _DONE_STEP_RE.match(step_line)
                    if not m or not m.group(3):
                        problems.append(f"{where}: done step lacks citation: {step_line!r}")

    if active_count > max_active:
        problems.append(
            f"active goals count {active_count} exceeds goals.max_active {max_active}"
        )

    # Catalog freshness
    catalog_path = goals_dir() / CATALOG
    if catalog_path.exists():
        try:
            cfm, _ = read_page(catalog_path)
            cat_count = int(cfm.get("count") or 0)
            if cat_count != len(goals):
                problems.append(f"goals catalog count {cat_count} != goals on disk {len(goals)}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"unreadable goals catalog: {exc}")
    elif goals:
        problems.append("goals catalog does not exist")

    return problems


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["catalog", "lint", "detectors"])
    args = ap.parse_args()
    if args.command == "catalog":
        print(regenerate_catalog())
    elif args.command == "lint":
        probs = lint()
        for p_err in probs:
            print(f"LINT: {p_err}")
        print(f"{len(probs)} problem(s)")
        raise SystemExit(1 if probs else 0)
    elif args.command == "detectors":
        for g in load_goals():
            print(f"{g.id} ({g.title}): silence={silence_detector(g)}, stale={stale_detector(g)}")
        drifts = drift_detector()
        print(f"Drifting loops ({len(drifts)}): {[(l.id, round(s, 2)) for l, s in drifts]}")
