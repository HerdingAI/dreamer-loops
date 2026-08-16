#!/usr/bin/env python3
"""Vault mechanics: loop state machine, catalog, decay, ranking, merge (§6.2).

Everything here is deterministic. The LLM decides *what* is a loop and *which*
loops match; this module decides where bytes go and enforces the state machine.
Keeping that split is what makes the §6.9 simulated-week acceptance test
meaningful rather than a coin flip.
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import (  # noqa: E402
    CFG, as_date, atomic_write, atomic_write_json, go_live_date, log, p,
    read_json, read_page, today, write_page,
)

STATUSES = {"open", "researching", "paused", "decision-only", "archived"}
TERMINAL = {"paused", "decision-only"}
CATALOG = "_catalog.md"

_ID_RE = re.compile(r"^L(\d{4,})$")


# --------------------------------------------------------------------------
# Loop model
# --------------------------------------------------------------------------

@dataclass
class Loop:
    id: str
    title: str
    status: str = "open"
    created: _dt.date | None = None
    first_seen: _dt.date | None = None
    last_seen: _dt.date | None = None
    recurrence_count: int = 0
    occurrences: list[str] = field(default_factory=list)
    route: str = ""
    conclusion: str = ""
    tags: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None
    type: str = "loop"

    # -- serialisation -----------------------------------------------------
    @classmethod
    def from_path(cls, path: Path) -> "Loop":
        fm, body = read_page(path)
        return cls(
            id=str(fm.get("id", "")),
            title=str(fm.get("title", "")),
            status=str(fm.get("status", "open")),
            created=as_date(fm.get("created")),
            first_seen=as_date(fm.get("first_seen")),
            last_seen=as_date(fm.get("last_seen")),
            recurrence_count=int(fm.get("recurrence_count") or 0),
            occurrences=list(fm.get("occurrences") or []),
            route=str(fm.get("route") or ""),
            conclusion=str(fm.get("conclusion") or ""),
            tags=list(fm.get("tags") or []),
            body=body,
            path=path,
            type=str(fm.get("type") or "loop"),
        )

    def frontmatter(self) -> dict:
        return {
            "type": "loop",
            "id": self.id,
            "status": self.status,
            "title": self.title,
            "created": self.created.isoformat() if self.created else None,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "recurrence_count": self.recurrence_count,
            "occurrences": self.occurrences,
            "route": self.route,
            "conclusion": self.conclusion,
            "tags": self.tags,
        }

    def save(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"{self.id}: illegal status {self.status!r}")
        if self.path is None:
            self.path = loops_dir() / f"{self.id}.md"
        write_page(self.path, self.frontmatter(), self.body or default_body(self))

    # -- ranking -----------------------------------------------------------
    def distinct_conversations(self) -> int:
        """recurrence_count is DEFINED as distinct conversations in the
        occurrence list (§6.6 merge arithmetic) — not a free-running counter."""
        return len({o.strip() for o in self.occurrences if o.strip()})

    def occurrence_dates(self) -> list[_dt.date]:
        out = []
        for occ in self.occurrences:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", occ)
            if m:
                d = as_date(m.group(1))
                if d:
                    out.append(d)
        return out

    def recency_weighted_score(self, ref: _dt.date | None = None) -> float:
        """Owner decision Q15: an idea coming up in a NEW transcript is what
        defines relevance, so a recent occurrence outweighs an equally-sized
        historical count. weight = 0.5 ** (age_days / half_life)."""
        ref = ref or today()
        half = float(CFG["matching"]["recency_half_life_days"])
        dates = self.occurrence_dates()
        if not dates:
            # No parseable dates: fall back to raw count, heavily discounted so
            # it never outranks a loop with real live evidence.
            return self.distinct_conversations() * 0.01
        return sum(0.5 ** (max((ref - d).days, 0) / half) for d in dates)

    # -- decay -------------------------------------------------------------
    def decay_deadline(self) -> _dt.date | None:
        """Decay clock = max(last_seen, GO_LIVE_DATE) + window (§6.2).

        Without the GO_LIVE floor, backfilled loops whose last_seen is months
        old would archive on day one, destroying the backfill's value.
        """
        if self.status == "archived":
            return None
        if self.status == "researching":
            return None  # never decay work in flight
        gld = go_live_date()
        if gld is None:
            return None  # automation not live: decay is inert
        base = self.last_seen or self.first_seen or gld
        anchor = max(base, gld)
        weeks = int(CFG["decay"]["decay_weeks"])
        if self.status in TERMINAL:
            weeks *= int(CFG["decay"]["terminal_multiplier"])
        return anchor + _dt.timedelta(weeks=weeks)

    def is_decayed(self, ref: _dt.date | None = None) -> bool:
        deadline = self.decay_deadline()
        if deadline is None:
            return False
        return (ref or today()) > deadline


def default_body(loop: Loop) -> str:
    return (
        f"# {loop.title}\n\n"
        "## Statement\n\n"
        f"{loop.title}\n\n"
        "## Occurrences\n\n"
        + ("\n".join(f"- {o}" for o in loop.occurrences) or "_none yet_")
        + "\n"
    )


_OCCURRENCES_SECTION = re.compile(
    r"^## Occurrences\s*$(.*?)(?=^## |\Z)", re.M | re.S)


def refresh_occurrences_section(loop: Loop) -> str:
    """Rewrite JUST the '## Occurrences' section of the existing body.

    add_occurrence() used to set loop.body = "" to force a refresh, and
    save() falls back to default_body() whenever body is falsy — which
    regenerates ONLY Statement + Occurrences and silently drops any other
    section a loop has grown, e.g. apply_conclusion.py's
    '## Superseded conclusions' list. Found live 2026-08-02: L0004 lost its
    superseded-conclusions section on the very next occurrence, and lint
    caught the resulting orphans. Surgical replacement instead of full
    regeneration.
    """
    listing = ("\n".join(f"- {o}" for o in loop.occurrences) or "_none yet_")
    if not loop.body:
        return default_body(loop)
    new_section = f"## Occurrences\n\n{listing}\n\n"
    if _OCCURRENCES_SECTION.search(loop.body):
        return _OCCURRENCES_SECTION.sub(new_section, loop.body, count=1)
    return loop.body.rstrip() + f"\n\n{new_section}"


# --------------------------------------------------------------------------
# Living thread section (CLAUDE.md rules 1/15)
# --------------------------------------------------------------------------
# One section per loop page, derived tier by declaration in its own heading.
# The applier (scripts/apply_thread.py) owns its content; these helpers own
# the sectioning so every reader and writer agrees on the boundaries — the
# same surgical approach refresh_occurrences_section uses, for the same
# reason: full-body regeneration silently deletes sibling sections.

THREAD_HEADING = "## Thread (derived — hypothesis, not evidence)"

_THREAD_SECTION = re.compile(
    rf"^{re.escape(THREAD_HEADING)}\s*$(.*?)(?=^## |\Z)", re.M | re.S)

_TRANSCRIPT_LINK = re.compile(r"\[\[\s*sources/transcripts/")


def thread_section(body: str) -> str | None:
    """The Thread section's inner content (stripped), or None if absent."""
    m = _THREAD_SECTION.search(body or "")
    return m.group(1).strip() if m else None


def replace_named_section(body: str, heading: str, content: str) -> str:
    """Replace (or append) EXACTLY the named `## `-level section of a body.

    Boundary convention shared by every section regex in this repo: the
    heading line up to the next '## ' or EOF. The lambda replacement keeps
    regex escapes in `content` inert — text containing a backslash must not
    corrupt the page.
    """
    section_re = re.compile(
        rf"^{re.escape(heading)}\s*$(.*?)(?=^## |\Z)", re.M | re.S)
    section = f"{heading}\n\n{content.strip()}\n\n"
    if not body:
        return section
    if section_re.search(body):
        return section_re.sub(lambda _m: section, body, count=1)
    return body.rstrip() + f"\n\n{section}"


def replace_thread_section(body: str, content: str) -> str:
    """Replace (or append) EXACTLY the Thread section of a loop body."""
    return replace_named_section(body, THREAD_HEADING, content)


# --------------------------------------------------------------------------
# Directory access
# --------------------------------------------------------------------------

def loops_dir() -> Path:
    d = p("loops")
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_dir() -> Path:
    d = p("archive")
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_loops(include_archived: bool = False) -> list[Loop]:
    out: list[Loop] = []
    for path in sorted(loops_dir().glob("*.md")):
        if path.name == CATALOG:
            continue
        try:
            loop = Loop.from_path(path)
        except Exception as exc:  # noqa: BLE001
            log(f"LINT skip unreadable loop {path.name}: {exc}", job="vault")
            continue
        if not loop.id:
            continue
        # A merge leaves a redirect at the retired path so inbound wikilinks
        # keep resolving. It is a signpost, not a loop: counting it would
        # resurrect the very duplicate the merge just removed.
        if loop.type == "loop-redirect":
            continue
        out.append(loop)
    if include_archived:
        for path in sorted(archive_dir().glob("*.md")):
            try:
                out.append(Loop.from_path(path))
            except Exception:  # noqa: BLE001
                continue
    return out


def load_loop(loop_id: str) -> Loop | None:
    """One non-archived loop by id, or None — the single-loop equivalent of
    `next((l for l in load_loops() if l.id == loop_id), None)` without the
    directory scan.

    save() keeps the filename == id invariant, so the page lives at
    loops_dir()/<id>.md. The same exclusions as load_loops() apply: an
    unreadable page, a page without an id, or a merge-redirect stub is not a
    loop.
    """
    path = loops_dir() / f"{loop_id}.md"
    if not path.exists():
        return None
    try:
        loop = Loop.from_path(path)
    except Exception as exc:  # noqa: BLE001
        log(f"LINT skip unreadable loop {path.name}: {exc}", job="vault")
        return None
    if loop.id != loop_id or loop.type == "loop-redirect":
        return None
    return loop


def next_loop_id() -> str:
    """Monotonic across active AND archived pages, so an id is never reused."""
    highest = 0
    for d in (loops_dir(), archive_dir()):
        for path in d.glob("*.md"):
            fm, _ = read_page(path)
            m = _ID_RE.match(str(fm.get("id", "")))
            if m:
                highest = max(highest, int(m.group(1)))
    return f"L{highest + 1:04d}"


def create_loop(title: str, occurrence: str, date: _dt.date,
                tags: list[str] | None = None) -> Loop:
    loop = Loop(
        id=next_loop_id(),
        title=title.strip(),
        status="open",
        created=today(),
        first_seen=date,
        last_seen=date,
        occurrences=[occurrence],
        tags=tags or [],
    )
    loop.recurrence_count = loop.distinct_conversations()
    loop.save()
    # Living thread (rule 15): the thread starts from the FIRST occurrence,
    # so creation enqueues exactly like add_occurrence does. Gate on the link,
    # not the caller: apply_extraction (the only production caller) always
    # passes a transcript, but a resurfacing must never fold regardless of who
    # passed it (rule 13). After save(), so a failed write never leaves a
    # phantom entry.
    if _TRANSCRIPT_LINK.match(occurrence):
        enqueue_fold_pending(loop.id, occurrence)
    return loop


def _occurrence_sort_key(occ: str) -> tuple[str, str]:
    """Sort occurrences chronologically; undated ones sort last, not away."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", occ)
    return (m.group(1) if m else "9999-99-99", occ)


def add_occurrence(loop: Loop, occurrence: str, date: _dt.date) -> bool:
    """Append an occurrence and apply the reopening rule. Returns True if the
    loop changed. Idempotent: re-adding the same occurrence is a no-op, which
    is what makes nightly re-runs safe (§9 idempotency)."""
    if occurrence in loop.occurrences:
        return False
    loop.occurrences.append(occurrence)
    # Chronological order. A loop's occurrence list is the commit history of an
    # idea: the value is seeing how the owner's position moved between the
    # first time they raised it and the last. Append order is INGESTION order,
    # which after a backfill interleaves 2026 and 2025 arbitrarily and destroys
    # exactly that reading. Undated occurrences sort last, keeping them visible
    # rather than silently dropped.
    loop.occurrences.sort(key=_occurrence_sort_key)
    loop.recurrence_count = loop.distinct_conversations()
    loop.last_seen = max(loop.last_seen, date) if loop.last_seen else date
    if loop.first_seen is None or date < loop.first_seen:
        loop.first_seen = date
    # Reopening rule: a resurfaced topic returns to open from any terminal
    # state. Reopening *is* recurrence — one mechanism (Principle 4).
    if loop.status in TERMINAL or loop.status == "archived":
        loop.status = "open"
        if loop.path and loop.path.parent == archive_dir():
            loop.path.unlink(missing_ok=True)
            loop.path = loops_dir() / f"{loop.id}.md"
    loop.body = refresh_occurrences_section(loop)
    loop.save()
    # Living thread (rule 15): every NEW transcript occurrence queues exactly
    # one incremental fold. The check is on the wikilink prefix, not the
    # caller: resurfacing links are a relevance signal, never fold input
    # (rule 13), and reopening flows through here so reopened loops enqueue
    # naturally. After save(), so a failed write never leaves a phantom entry.
    if _TRANSCRIPT_LINK.match(occurrence):
        enqueue_fold_pending(loop.id, occurrence)
    return True


# --------------------------------------------------------------------------
# Catalog (§6.2) — regenerated from frontmatter, never hand-maintained
# --------------------------------------------------------------------------

def regenerate_catalog() -> Path:
    loops = sorted(load_loops(), key=lambda l: l.id)
    lines = [
        "---",
        "type: catalog",
        "generated: auto — do not hand-edit; regenerated at the end of every job",
        f"count: {len(loops)}",
        "---",
        "",
        "# Loop catalog",
        "",
        "One line per active loop. Agents read this FIRST and open full pages",
        "only on candidate hits (CLAUDE.md rule 11).",
        "",
        "| id | title | status | first_seen | count | last_seen |",
        "|---|---|---|---|---|---|",
    ]
    for l in loops:
        title = l.title.replace("|", "\\|")
        lines.append(
            f"| {l.id} | {title} | {l.status} | {l.first_seen or '—'} | "
            f"{l.recurrence_count} | {l.last_seen or '—'} |"
        )
    path = loops_dir() / CATALOG
    atomic_write(path, "\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# Decay (§6.2 / Job 3)
# --------------------------------------------------------------------------

def archive_loop(loop: Loop) -> Path:
    src = loop.path
    dest = archive_dir() / f"{loop.id}.md"
    loop.status = "archived"
    loop.path = dest
    loop.save()
    if src and src.exists() and src != dest:
        src.unlink()
    return dest


def run_decay(ref: _dt.date | None = None) -> list[Loop]:
    ref = ref or today()
    archived: list[Loop] = []
    for loop in load_loops():
        if loop.is_decayed(ref):
            archive_loop(loop)
            archived.append(loop)
    return archived


# --------------------------------------------------------------------------
# Selection (§6.9 Job 2)
# --------------------------------------------------------------------------

def conclusion_date(loop: Loop) -> _dt.date | None:
    """Conclusion pages are named YYYY-MM-DD--slug; the date is authoritative
    enough for the gate. Falls back to None (gate stays open) on odd names."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", loop.conclusion or "")
    return as_date(m.group(1)) if m else None


def reresearch_gate(loop: Loop, ref: _dt.date) -> tuple[bool, str]:
    """Conclusion-stability rule (CLAUDE.md rule 14).

    A concluded loop resurfacing is a RELEVANCE signal, not a re-research
    trigger: without this gate every resurfacing reopened the loop and the next
    weekly run re-researched it, which produced four superseding conclusions on
    one loop in two days (L0023) — each citing the previous one. Serve the
    existing conclusion unless something is genuinely new.

    Returns (eligible, reason_if_served).
    """
    if not loop.conclusion:
        return True, ""
    cdate = conclusion_date(loop)
    if cdate is None:
        return True, ""
    r = CFG.get("research", {})
    cooldown = int(r.get("min_days_between_reresearch", 21))
    age = (ref - cdate).days
    if age < cooldown:
        return False, (f"conclusion is {age}d old, inside the {cooldown}d "
                       f"re-research cooldown")
    if not any(d > cdate for d in loop.occurrence_dates()):
        # Nothing new has been said since the conclusion. Only external facts
        # rot on their own, so age alone re-qualifies web-routed loops only.
        stale = int(r.get("web_conclusion_stale_days", 90))
        if loop.route in ("web", "mixed") and age >= stale:
            return True, ""
        return False, "no occurrence newer than the existing conclusion"
    return True, ""


def select_for_research(n: int | None = None,
                        ref: _dt.date | None = None,
                        served: list[tuple[Loop, str]] | None = None) -> list[Loop]:
    ref = ref or today()
    n = n or int(CFG["research"]["weekly_loop_count"])
    minimum = int(CFG["matching"]["recurrence_min"])
    eligible = []
    for l in load_loops():
        if l.status != "open" or l.recurrence_count < minimum:
            continue
        ok, reason = reresearch_gate(l, ref)
        if ok:
            eligible.append(l)
        elif served is not None:
            served.append((l, reason))
    eligible.sort(key=lambda l: (l.recency_weighted_score(ref),
                                 l.last_seen or _dt.date.min, l.id),
                  reverse=True)
    return eligible[:n]


def recover_stranded(ref: _dt.date | None = None) -> list[Loop]:
    """§6.9 recovery rule: a loop left in `researching` by a usage-limit exit
    is invisible to selection forever, because selection reads only `open`.
    Reset it and let it be re-selected first."""
    recovered = []
    for loop in load_loops():
        if loop.status == "researching":
            loop.status = "open"
            loop.save()
            recovered.append(loop)
    return recovered


# --------------------------------------------------------------------------
# Merge (§6.6)
# --------------------------------------------------------------------------

def enqueue_fold_pending(loop_id: str, occurrence: str) -> bool:
    """Queue (loop_id, occurrence) for a later body-fold pass.

    Append-only ledger at meta/fold-pending.json, created lazily. The consumer
    collapses duplicates on read, so append-only is safe; the duplicate check
    here (on the identity fields only — entries also carry metadata) just
    keeps the file from growing on idempotent re-runs. `enqueued_at` feeds the
    fold-queue-current healthcheck's age leg; `attempts` is stamped later by
    fold_pending.record_attempt. Returns True if the entry was actually added
    (False on duplicate), so the thread-backfill drain can report honest
    counts.
    """
    path = p("meta") / "fold-pending.json"
    entries = read_json(path, default=[]) or []
    for e in entries:
        if (isinstance(e, dict) and e.get("loop_id") == loop_id
                and e.get("occurrence") == occurrence):
            return False
    entries.append({"loop_id": loop_id, "occurrence": occurrence,
                    "enqueued_at": _dt.datetime.now()
                    .isoformat(timespec="seconds")})
    atomic_write_json(path, entries)
    return True


def merge_loops(keep: Loop, retire: Loop) -> Loop:
    """Union of occurrences; recurrence_count = distinct conversations in that
    union (NOT max, NOT naive sum); earliest first_seen; redirect stub at the
    retired path so inbound links never break."""
    merged_in: list[str] = []
    for occ in retire.occurrences:
        if occ not in keep.occurrences:
            keep.occurrences.append(occ)
            # The keep page's body has never folded this occurrence in;
            # collected here, queued only after keep.save() succeeds below.
            merged_in.append(occ)
    # Same chronological invariant add_occurrence maintains: append order is
    # merge order, and a retire loop older than the keep loop would otherwise
    # leave the idea's history unreadable.
    keep.occurrences.sort(key=_occurrence_sort_key)
    keep.recurrence_count = keep.distinct_conversations()
    firsts = [d for d in (keep.first_seen, retire.first_seen) if d]
    lasts = [d for d in (keep.last_seen, retire.last_seen) if d]
    keep.first_seen = min(firsts) if firsts else None
    keep.last_seen = max(lasts) if lasts else None
    for t in retire.tags:
        if t not in keep.tags:
            keep.tags.append(t)
    # Surgical refresh, NOT keep.body = "": wiping the body made save() fall
    # back to default_body(), which regenerates only Statement + Occurrences
    # and silently deletes every other section — the same failure documented
    # on refresh_occurrences_section for add_occurrence.
    keep.body = refresh_occurrences_section(keep)
    keep.save()
    # AFTER the save, matching add_occurrence's discipline: a failed write
    # must never leave phantom queue entries for a merge that did not land.
    for occ in merged_in:
        enqueue_fold_pending(keep.id, occ)

    stub_fm = {
        "type": "loop-redirect",
        "id": retire.id,
        "status": "archived",
        "title": retire.title,
        "redirect_to": keep.id,
    }
    stub_body = (
        f"# {retire.title}\n\n"
        f"Merged into [[loops/{keep.id}]] on {today().isoformat()}.\n\n"
        "This stub exists so inbound links never break (§6.6).\n"
    )
    # The stub must sit at the RETIRED path, not only in the archive: the whole
    # point is that `[[loops/L0037]]` keeps resolving. Writing it to archive/
    # and unlinking loops/L0037.md broke exactly the links it claimed to
    # preserve (found 2026-08-02, after the first real merge). Archive keeps a
    # copy so the merge is visible in the decay record too.
    write_page(loops_dir() / f"{retire.id}.md", stub_fm, stub_body)
    write_page(archive_dir() / f"{retire.id}.md", stub_fm, stub_body)
    return keep


# --------------------------------------------------------------------------
# Lint (§6.2 DoD)
# --------------------------------------------------------------------------

# A trajectory line as apply_thread writes it: "- YYYY-MM-DD — text — [[occ]]".
_TRAJ_CITE = re.compile(r"^- \d{4}-\d{2}-\d{2} — .+? — \[\[([^\]]+)\]\]\s*$",
                        re.M)


def lint(vocabulary: set[str] | None = None) -> list[str]:
    problems: list[str] = []
    # Thread coverage (rule-15 repair discipline): every transcript occurrence
    # of a non-archived loop must be either FOLDED (cited by a trajectory line
    # in the Thread section) or QUEUED in fold-pending.json. This is the
    # strongest cheap invariant that catches the body-wipe class of defect
    # (a page rebuilt from default_body loses its trajectory while the queue
    # holds nothing) — note it reads one piece of vault-meta state alongside
    # the page, because page state alone cannot distinguish "wiped" from
    # "never folded". On a pre-thread vault this check fires honestly until
    # bin/thread-backfill.sh's first run enqueues the initial threads.
    pending: set[tuple[str, str]] = set()
    for e in read_json(p("meta") / "fold-pending.json", default=[]) or []:
        if isinstance(e, dict):
            pending.add((str(e.get("loop_id")), str(e.get("occurrence"))))
    # Quarantined entries (fold_pending.record_attempt, over
    # thread.fold_max_attempts) are a NAMED problem — "N failed folds" points
    # at the defective fold input, where the generic uncovered message would
    # misread it as a wiped thread.
    quarantined: dict[tuple[str, str], int] = {}
    for e in read_json(p("meta") / "fold-quarantine.json", default=[]) or []:
        if isinstance(e, dict):
            quarantined[(str(e.get("loop_id")), str(e.get("occurrence")))] = \
                int(e.get("attempts") or 0)
    if vocabulary is None:
        # Once the owner freezes a vocabulary, lint enforces it automatically.
        # Before that, tags are expected to be absent entirely.
        try:
            import propose_tags
            vocabulary = propose_tags.vocabulary()
        except Exception:  # noqa: BLE001
            vocabulary = None
    loops = load_loops(include_archived=True)
    seen_ids: dict[str, Path] = {}

    for loop in loops:
        where = loop.path.name if loop.path else loop.id
        if not _ID_RE.match(loop.id):
            problems.append(f"{where}: malformed id {loop.id!r}")
        if loop.id in seen_ids:
            problems.append(f"{where}: duplicate id {loop.id} "
                            f"(also {seen_ids[loop.id].name})")
        seen_ids[loop.id] = loop.path or Path(where)
        if loop.status not in STATUSES:
            problems.append(f"{where}: illegal status {loop.status!r}")
        if not loop.title.strip():
            problems.append(f"{where}: empty title")
        if loop.recurrence_count != loop.distinct_conversations():
            problems.append(
                f"{where}: recurrence_count={loop.recurrence_count} but "
                f"{loop.distinct_conversations()} distinct occurrences")
        # Occurrence ordering: every write path must leave the list sorted by
        # _occurrence_sort_key (add_occurrence and merge_loops both do).
        if loop.occurrences != sorted(loop.occurrences, key=_occurrence_sort_key):
            problems.append(f"{where}: occurrences not in chronological order")
        # Broken occurrence links
        for occ in loop.occurrences:
            target = _resolve_wikilink(occ)
            if target is None:
                problems.append(f"{where}: broken occurrence link {occ!r}")
        # Thread coverage — see the comment at the top of lint().
        if loop.status != "archived":
            folded = {_norm(m) for m in
                      _TRAJ_CITE.findall(thread_section(loop.body) or "")}
            for occ in loop.occurrences:
                if not _TRANSCRIPT_LINK.match(occ):
                    continue
                if _norm(occ) in folded:
                    continue
                if (loop.id, occ) in quarantined:
                    problems.append(
                        f"{where}: transcript occurrence {occ!r} quarantined "
                        f"after {quarantined[(loop.id, occ)]} failed folds — "
                        f"repair the fold input and re-enqueue "
                        f"(fold-quarantine.json)")
                    continue
                if (loop.id, occ) in pending:
                    continue
                problems.append(
                    f"{where}: transcript occurrence {occ!r} is neither "
                    f"folded into the Thread section nor queued in "
                    f"fold-pending — a body rebuild may have wiped the "
                    f"thread (for pre-thread loops, run bin/thread-backfill.sh)")
        # Conclusions must exist and be linked back
        if loop.conclusion:
            if _resolve_wikilink(loop.conclusion) is None:
                problems.append(f"{where}: conclusion link not found {loop.conclusion!r}")
        if loop.status == "paused" and not loop.conclusion:
            problems.append(f"{where}: paused without a conclusion")
        if vocabulary is not None:
            for tag in loop.tags:
                if tag not in vocabulary:
                    problems.append(f"{where}: out-of-vocabulary tag {tag!r}")

    # Orphan conclusions. A superseded conclusion is reachable through the
    # loop body's history list, not through the `conclusion` field, and must
    # not be reported as an orphan — re-researching a loop is normal, and the
    # older reasoning is an asset we deliberately keep linked.
    linked = {_norm(l.conclusion) for l in loops if l.conclusion}
    for loop in loops:
        for m in re.finditer(r"\[\[(conclusions/[^\]]+)\]\]", loop.body or ""):
            linked.add(_norm(m.group(1)))
    for path in p("conclusions").glob("*.md"):
        if _norm(f"conclusions/{path.stem}") not in linked:
            problems.append(f"conclusions/{path.name}: orphan (no loop links to it)")
    return problems


def _norm(ref: str) -> str:
    return ref.strip().strip("[]").lstrip("/").removesuffix(".md")


def _resolve_wikilink(ref: str) -> Path | None:
    """Resolve a wikilink to a file under the vault, refusing traversal.

    `ref` may originate from LLM output (extraction's `transcript` field,
    a fold occurrence) rather than deterministic code, so a `..` segment
    or an absolute path must never resolve outside `p("vault")` — that
    would turn a single model-invented path into an arbitrary-file read
    whose content then gets embedded in a later prompt (fold_pending.py).
    """
    rel = _norm(ref)
    if not rel or ".." in Path(rel).parts:
        return None
    base = p("vault").resolve()
    for cand in (base / f"{rel}.md", base / rel):
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_relative_to(base):
            return resolved
    # Bare name: search the vault.
    name = Path(rel).name
    for cand in base.rglob(f"{name}.md"):
        return cand
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["catalog", "lint", "decay", "select",
                                        "recover"])
    args = ap.parse_args()
    if args.command == "catalog":
        print(regenerate_catalog())
    elif args.command == "lint":
        probs = lint()
        for x in probs:
            print(f"LINT: {x}")
        print(f"{len(probs)} problem(s)")
        raise SystemExit(1 if probs else 0)
    elif args.command == "decay":
        for l in run_decay():
            print(f"archived {l.id} {l.title}")
    elif args.command == "select":
        served: list[tuple[Loop, str]] = []
        for l in select_for_research(served=served):
            print(f"{l.id} score={l.recency_weighted_score():.3f} "
                  f"count={l.recurrence_count} {l.title}")
        if served:
            # Lazy import: digest imports vault at module level.
            import digest as G
            for l, reason in served:
                G.stage("served", {"loop": l.id, "title": l.title,
                                   "conclusion": l.conclusion,
                                   "recurrence": l.recurrence_count,
                                   "reason": reason})
                log(f"serve {l.id}: {reason}", job="select")
    elif args.command == "recover":
        for l in recover_stranded():
            print(f"recovered {l.id}")
