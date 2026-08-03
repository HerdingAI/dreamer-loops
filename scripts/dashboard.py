#!/usr/bin/env python3
"""Regenerate the vault-shape dashboard from live vault state.

A digest answers "what happened this week, and what should you read?". This
answers a different question — "what shape is the vault in right now?" — and it
has to answer it between runs, not only after one. So it is a pure read-only
projection: it opens the same pages `vault.py` and `grade_conclusions.py` do,
computes counts, and writes one self-contained HTML file. No LLM calls, no
network, no writes anywhere except its own output.

That purity is what lets it run on a timer without a lock. Every vault write
goes through `os.replace()` (dreamer_common.atomic_write), so a reader either
sees the old page or the new one, never a partial one — the same guarantee
dreamer-mcp relies on. Reading mid-run is therefore safe, and a dashboard that
is occasionally one page stale is worth far more than one that is only correct
immediately after a job.

Usage:
    python3 scripts/dashboard.py                  # -> vault/dashboard.html
    python3 scripts/dashboard.py --out FILE
    python3 scripts/dashboard.py --json           # stats only, no HTML
    python3 scripts/dashboard.py --serve [PORT]   # live: regenerates per request
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dreamer_common as C  # noqa: E402
import vault as V  # noqa: E402

try:
    from grade_conclusions import grade as _grade
except Exception:  # pragma: no cover - rubric is optional, never fatal
    _grade = None


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

_SERVED_RE = __import__("re").compile(
    r"^- `(?P<loop>L\d+)`\s+\*\*(?P<title>.+?)\*\*\s+—\s+"
    r"resurfaced \(recurrence (?P<recur>\d+)\)"
)


def _served_from_digest() -> tuple[list[dict], str]:
    """Recover the served list from the newest digest's own section.

    Parses rather than re-derives: the digest is the record of what the run
    actually decided, and re-deriving would risk showing a serve that never
    happened. A parse failure degrades to an empty panel, never to a guess.
    """
    d_dir = C.p("digests")
    if not d_dir.exists():
        return [], ""
    digests = sorted(d_dir.glob("[0-9]*.md"))
    if not digests:
        return [], ""
    newest = digests[-1]
    try:
        text = newest.read_text(encoding="utf-8")
    except Exception:
        return [], ""

    out: list[dict] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = "served" in line.lower()
            continue
        if not in_section:
            continue
        m = _SERVED_RE.match(line.strip())
        if m:
            out.append({
                "loop": m.group("loop"),
                "title": m.group("title"),
                "recurrence": int(m.group("recur")),
                "reason": "existing conclusion served — no research run",
            })
    return out, f"digest {newest.stem}"


def collect() -> dict:
    """Gather every number the page shows. Pure reads."""
    cfg = C.CFG
    today = C.today()

    loops = V.load_loops()
    archived = V.load_loops(include_archived=True)
    n_archived = max(0, len(archived) - len(loops))

    statuses = collections.Counter(l.status for l in loops)
    routes = collections.Counter(l.route or "unrouted" for l in loops)
    tags = collections.Counter(t for l in loops for t in l.tags)

    recur = [l.recurrence_count for l in loops] or [0]
    firsts = [l.first_seen for l in loops if l.first_seen]
    lasts = [l.last_seen for l in loops if l.last_seen]

    # Loops carrying a conclusion vs. loops still unanswered.
    concluded = [l for l in loops if l.conclusion]

    # Recurrence histogram — the relevance distribution. A vault where
    # everything sits at 1 has not yet earned any research; a long tail is
    # where the system is actually paying for itself.
    hist = collections.Counter(min(l.recurrence_count, 6) for l in loops)

    # Conclusion pages + rubric scores.
    conc_dir = C.p("conclusions")
    conc_files = sorted(conc_dir.glob("*.md")) if conc_dir.exists() else []
    confidences: collections.Counter = collections.Counter()
    scores: list[float] = []
    fails: collections.Counter = collections.Counter()
    supersedes: dict[str, str | None] = {}
    for f in conc_files:
        try:
            fm, _ = C.read_page(f)
        except Exception:
            continue
        confidences[str(fm.get("confidence") or "unset")] += 1
        supersedes[f.stem] = fm.get("superseded_by")
        if _grade is not None:
            try:
                row = _grade(f)
                scores.append(float(row["score"]))
                for bad in row.get("failed") or []:
                    fails[bad] += 1
            except Exception:
                pass

    # Deepest supersession chain. Rule 13 exists because this number silently
    # grew to 4 in two days; showing it makes a regression visible on sight.
    def depth(stem: str, seen: frozenset = frozenset()) -> int:
        if stem in seen:
            return len(seen)  # cycle guard — never loop forever on bad data
        nxt = supersedes.get(stem)
        if not nxt:
            return 1
        return 1 + depth(str(nxt).split("/")[-1], seen | {stem})

    max_chain = max((depth(s) for s in supersedes), default=0)

    # Run state: costs, deferrals, events.
    state = C.read_json(C.p("meta") / "run-state.json", {}) or {}
    costs = state.get("costs") or []
    deferrals = state.get("deferrals") or []
    events = state.get("events") or []
    by_job: dict[str, dict] = {}
    for c in costs:
        j = by_job.setdefault(str(c.get("job") or "?"),
                              {"runs": 0, "cost": 0.0, "deferrals": 0})
        j["runs"] += 1
        j["cost"] += float(c.get("cost") or 0)
    for d in deferrals:
        j = by_job.setdefault(str(d.get("job") or "?"),
                              {"runs": 0, "cost": 0.0, "deferrals": 0})
        j["deferrals"] += 1

    # Cost outliers. Under a subscription these are notional units, not money,
    # so the useful signal is not a total but a value that does not belong to
    # the same distribution as its neighbours.
    vals = sorted(float(c.get("cost") or 0) for c in costs)
    median = vals[len(vals) // 2] if vals else 0.0
    outliers = [c for c in costs if float(c.get("cost") or 0) > max(20.0, median * 25)]

    # Served loops — rule 14's novelty gate doing its job.
    #
    # pending.json only holds these between the run that stages them and the
    # digest that consumes them, so reading it alone makes the single most
    # interesting signal vanish the moment a digest is built. Fall back to the
    # newest digest so the panel survives the handoff.
    pending = C.read_json(C.p("digests") / "pending.json", {}) or {}
    served = list(pending.get("served") or [])
    served_from = "the run in flight"
    if not served:
        served, served_from = _served_from_digest()

    # Freshness: newest transcript on disk vs. today.
    src = C.p("sources")
    newest = None
    if src.exists():
        dates = []
        for f in src.rglob("*.md"):
            d = C.as_date(f.stem.split("--")[0])
            if d:
                dates.append(d)
        newest = max(dates) if dates else None

    go_live = C.go_live_date()
    decay_weeks = ((cfg.get("decay") or {}).get("decay_weeks"))

    return {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "loops": {
            "total": len(loops),
            "archived": n_archived,
            "statuses": dict(statuses),
            "routes": dict(routes),
            "concluded": len(concluded),
            "recurrence": {
                "min": min(recur), "max": max(recur),
                "avg": round(sum(recur) / len(recur), 2),
                "histogram": {str(k): v for k, v in sorted(hist.items())},
            },
            "first_seen": min(firsts).isoformat() if firsts else None,
            "last_seen": max(lasts).isoformat() if lasts else None,
        },
        "tags": tags.most_common(14),
        "conclusions": {
            "total": len(conc_files),
            "confidences": dict(confidences),
            "scores": scores,
            "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
            "low_scoring": sum(1 for s in scores if s < 0.7),
            "fails": fails.most_common(8),
            "max_chain": max_chain,
        },
        "runs": {
            "total": len(costs),
            "by_job": by_job,
            "deferrals": len(deferrals),
            "outliers": outliers,
            "recent_events": events[-6:],
        },
        "pending": pending,
        "served": served,
        "served_from": served_from,
        "freshness": {
            "newest_transcript": newest.isoformat() if newest else None,
            "stale_days": (today - newest).days if newest else None,
            "go_live": go_live.isoformat() if go_live else None,
            "decay_weeks": decay_weeks,
        },
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _bars(counts: dict, total: int, limit: int = 8) -> str:
    if not counts:
        return '<p class="empty">Nothing recorded yet.</p>'
    rows = []
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    biggest = max(v for _, v in top) or 1
    for name, n in top:
        pct = 100.0 * n / biggest
        share = f"{100.0 * n / total:.0f}%" if total else ""
        rows.append(
            f'<div class="bar-row"><span class="name">{_e(name)}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="count">{n}<em>{share}</em></span></div>'
        )
    return '<div class="bars">' + "".join(rows) + "</div>"


def _pill(kind: str, text: str) -> str:
    return f'<span class="pill {kind}">{_e(text)}</span>'


def render(d: dict) -> str:
    L, CN, R, F = d["loops"], d["conclusions"], d["runs"], d["freshness"]

    # --- headline status cells -------------------------------------------
    cells = []

    stale = F["stale_days"]
    if stale is None:
        cells.append(("Transcript inbox", "warn", "no transcripts found",
                      "Nothing on disk under sources/transcripts."))
    else:
        kind = "good" if stale <= 3 else ("warn" if stale <= 10 else "crit")
        cells.append(("Newest transcript", kind,
                      "today" if stale == 0 else f"{stale}d ago",
                      f"Latest conversation ingested: {F['newest_transcript']}."))

    if F["go_live"]:
        cells.append(("Decay clock", "good", f"live since {F['go_live']}",
                      f"Loops archive after {F['decay_weeks']} weeks without recurrence."))
    else:
        cells.append(("Decay clock", "warn", "inert",
                      "GO_LIVE_DATE is null, so nothing archives yet — backfilled "
                      "loops keep their full window (rule 3)."))

    chain = CN["max_chain"]
    ckind = "good" if chain <= 2 else ("warn" if chain <= 3 else "crit")
    cells.append(("Deepest conclusion chain", ckind, f"{chain} deep",
                  "How many times one loop's conclusion has been superseded. "
                  "Above 2 is worth a look at the re-research cooldown."))

    dcount = R["deferrals"]
    cells.append(("Deferred runs", "good" if dcount == 0 else "warn",
                  "none" if dcount == 0 else f"{dcount} logged",
                  "A deferral is an honest stop, not a failure — work resumes "
                  "on the next scheduled run."))

    status_html = "".join(
        f'<div class="status-cell"><div class="label">{_e(lbl)}</div>{_pill(k, txt)}'
        f'<div class="detail">{_e(det)}</div></div>'
        for lbl, k, txt, det in cells
    )

    # --- recurrence histogram --------------------------------------------
    hist = L["recurrence"]["histogram"]
    hist_rows = []
    hbig = max(hist.values()) if hist else 1
    for k in sorted(hist, key=lambda x: int(x)):
        label = "6+" if int(k) >= 6 else k
        n = hist[k]
        hist_rows.append(
            f'<div class="bar-row"><span class="name">seen {_e(label)}×</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{100.0*n/hbig:.1f}%"></div></div>'
            f'<span class="count">{n}</span></div>'
        )
    hist_html = '<div class="bars">' + "".join(hist_rows) + "</div>"

    # --- tags -------------------------------------------------------------
    tags_html = "".join(f'<span class="tag">{_e(t)}<em>{n}</em></span>'
                        for t, n in d["tags"]) or '<p class="empty">No approved tags in use yet.</p>'

    # --- conclusion quality ----------------------------------------------
    scores = CN["scores"]
    if scores:
        buckets: collections.Counter = collections.Counter()
        for s in scores:
            buckets[round(s, 1)] += 1
        qrows = []
        qbig = max(buckets.values())
        for b in sorted(buckets, reverse=True):
            n = buckets[b]
            cls = "bar-fill" + ("" if b >= 0.7 else " low")
            qrows.append(
                f'<div class="bar-row"><span class="name">{b:.1f}</span>'
                f'<div class="bar-track"><div class="{cls}" style="width:{100.0*n/qbig:.1f}%"></div></div>'
                f'<span class="count">{n}</span></div>'
            )
        quality_html = '<div class="bars">' + "".join(qrows) + "</div>"
    else:
        quality_html = '<p class="empty">No conclusions graded yet.</p>'

    fails_html = "".join(
        f'<tr><td>{_e(name.replace("_", " "))}</td><td class="num-cell">{n}</td></tr>'
        for name, n in CN["fails"]
    ) or '<tr><td colspan="2" class="empty">No rubric failures.</td></tr>'

    # --- runs -------------------------------------------------------------
    job_rows = "".join(
        f'<tr><td>{_e(job)}</td><td class="num-cell">{v["runs"]}</td>'
        f'<td class="num-cell">{v["cost"]:.1f}</td>'
        f'<td class="num-cell">{v["deferrals"] or "—"}</td></tr>'
        for job, v in sorted(R["by_job"].items(), key=lambda kv: -kv[1]["runs"])
    ) or '<tr><td colspan="4" class="empty">No runs recorded.</td></tr>'

    outlier_html = ""
    if R["outliers"]:
        items = ", ".join(
            f'{float(c.get("cost") or 0):.2f} ({_e(c.get("job"))}, {_e(str(c.get("at"))[:16])})'
            for c in R["outliers"][:4]
        )
        outlier_html = (
            f'<div class="note warn-note"><strong>Cost outlier.</strong> '
            f'{items} sits far outside the range of every other recorded run. '
            f'Worth checking whether that batch really did proportionally more '
            f'work, or whether something mis-recorded its cost.</div>'
        )

    events_html = "".join(
        f'<tr><td class="mono">{_e(str(e.get("at"))[5:16])}</td>'
        f'<td>{_e(e.get("job"))}</td><td>{_e(e.get("kind"))}</td>'
        f'<td>{_e(e.get("detail"))}</td></tr>'
        for e in reversed(R["recent_events"])
    ) or '<tr><td colspan="4" class="empty">No run events pending.</td></tr>'

    # --- served (novelty gate) -------------------------------------------
    served = d.get("served") or []
    served_html = "".join(
        f'<tr><td class="mono">{_e(s.get("loop"))}</td><td>{_e(s.get("title"))}</td>'
        f'<td class="num-cell">{_e(s.get("recurrence"))}</td>'
        f'<td class="dim">{_e(s.get("reason"))}</td></tr>'
        for s in served
    ) or ('<tr><td colspan="4" class="empty">No serves on record — no concluded '
          'loop has resurfaced yet.</td></tr>')

    unrouted = L["routes"].get("unrouted", 0)

    return f"""<title>Dreamer — Vault Shape</title>
<style>
:root{{
  --bg:#f3f0e8; --bg-raised:#fff; --ink:#201d1a; --ink-dim:#6b6459;
  --accent:#b5651d; --accent-soft:#eeddc6; --line:#ddd5c5;
  --good:#4c7a5e; --good-bg:#e3ecdf; --warn:#a3760f; --warn-bg:#f5e8ca;
  --crit:#a3372f; --crit-bg:#f5dcd6;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Consolas,monospace;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --sans:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}}
@media (prefers-color-scheme:dark){{
  :root{{
    --bg:#14171f; --bg-raised:#1b1f2b; --ink:#e9e6dd; --ink-dim:#98a0b2;
    --accent:#dd9a52; --accent-soft:#3a2e1e; --line:#2b3040;
    --good:#7cb797; --good-bg:#1c2c22; --warn:#dcb15a; --warn-bg:#2e2711;
    --crit:#e08079; --crit-bg:#331d1c;
  }}
}}
:root[data-theme="dark"]{{
  --bg:#14171f; --bg-raised:#1b1f2b; --ink:#e9e6dd; --ink-dim:#98a0b2;
  --accent:#dd9a52; --accent-soft:#3a2e1e; --line:#2b3040;
  --good:#7cb797; --good-bg:#1c2c22; --warn:#dcb15a; --warn-bg:#2e2711;
  --crit:#e08079; --crit-bg:#331d1c;
}}
:root[data-theme="light"]{{
  --bg:#f3f0e8; --bg-raised:#fff; --ink:#201d1a; --ink-dim:#6b6459;
  --accent:#b5651d; --accent-soft:#eeddc6; --line:#ddd5c5;
  --good:#4c7a5e; --good-bg:#e3ecdf; --warn:#a3760f; --warn-bg:#f5e8ca;
  --crit:#a3372f; --crit-bg:#f5dcd6;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  line-height:1.5;padding:0 0 4rem}}
.wrap{{max-width:1060px;margin:0 auto;padding:2.5rem 1.5rem 0}}
header.top{{display:flex;justify-content:space-between;align-items:flex-end;gap:1.5rem;
  flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:1.4rem;margin-bottom:1.8rem}}
h1{{font-family:var(--serif);font-weight:600;font-size:2rem;margin:0 0 .3rem;
  letter-spacing:-.01em;text-wrap:balance}}
.eyebrow{{font-family:var(--mono);font-size:.7rem;letter-spacing:.13em;text-transform:uppercase;
  color:var(--accent);margin:0 0 .5rem}}
.subtitle{{color:var(--ink-dim);font-size:.93rem;margin:0;max-width:46ch}}
.asof{{font-family:var(--mono);font-size:.75rem;color:var(--ink-dim);text-align:right;line-height:1.7}}
.asof strong{{color:var(--ink);font-weight:600;display:block;font-size:.84rem}}
.status-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden;margin-bottom:2.4rem}}
.status-cell{{background:var(--bg-raised);padding:.95rem 1.1rem}}
.status-cell .label{{font-family:var(--mono);font-size:.66rem;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-dim);margin-bottom:.45rem}}
.pill{{display:inline-flex;align-items:center;gap:.4rem;font-size:.81rem;font-weight:600;
  padding:.15rem .6rem;border-radius:999px}}
.pill::before{{content:"";width:.48rem;height:.48rem;border-radius:50%}}
.pill.good{{background:var(--good-bg);color:var(--good)}} .pill.good::before{{background:var(--good)}}
.pill.warn{{background:var(--warn-bg);color:var(--warn)}} .pill.warn::before{{background:var(--warn)}}
.pill.crit{{background:var(--crit-bg);color:var(--crit)}} .pill.crit::before{{background:var(--crit)}}
.status-cell .detail{{font-size:.79rem;color:var(--ink-dim);margin-top:.5rem}}
section{{margin-bottom:2.5rem}}
h2{{font-family:var(--serif);font-size:1.2rem;font-weight:600;margin:0 0 .2rem;
  padding-bottom:.55rem;border-bottom:1px solid var(--line)}}
.section-note{{color:var(--ink-dim);font-size:.84rem;margin:.55rem 0 1.15rem;max-width:62ch}}
.cols{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:2rem}}
.card-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:.9rem}}
.card{{background:var(--bg-raised);border:1px solid var(--line);border-radius:6px;padding:1rem 1.1rem}}
.card .num{{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:1.75rem;
  font-weight:600;line-height:1.1}}
.card .num small{{font-size:.9rem;color:var(--ink-dim);font-weight:500;margin-left:.15rem}}
.card .cap{{font-size:.78rem;color:var(--ink-dim);margin-top:.35rem}}
.bars{{display:flex;flex-direction:column;gap:.55rem}}
.bar-row{{display:grid;grid-template-columns:minmax(90px,140px) 1fr 4.5rem;align-items:center;gap:.7rem}}
.bar-row .name{{font-size:.84rem}}
.bar-track{{background:var(--line);border-radius:3px;height:.52rem;overflow:hidden}}
.bar-fill{{height:100%;background:var(--accent);border-radius:3px}}
.bar-fill.low{{background:var(--crit)}}
.bar-row .count{{font-family:var(--mono);font-size:.78rem;text-align:right;
  font-variant-numeric:tabular-nums}}
.bar-row .count em{{font-style:normal;color:var(--ink-dim);font-size:.7rem;margin-left:.35rem}}
.tbl-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--bg-raised)}}
table{{width:100%;border-collapse:collapse;font-size:.84rem}}
th{{text-align:left;font-family:var(--mono);font-size:.66rem;letter-spacing:.06em;
  text-transform:uppercase;color:var(--ink-dim);font-weight:600;padding:.5rem .65rem;
  border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:.5rem .65rem;border-bottom:1px solid var(--line);vertical-align:top}}
tr:last-child td{{border-bottom:none}}
.num-cell{{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}}
.mono{{font-family:var(--mono);font-size:.78rem;white-space:nowrap}}
.dim{{color:var(--ink-dim)}}
.tag{{display:inline-flex;align-items:baseline;gap:.35rem;font-family:var(--mono);font-size:.73rem;
  background:var(--accent-soft);color:var(--accent);padding:.15rem .55rem;border-radius:4px;
  margin:0 .3rem .4rem 0}}
.tag em{{font-style:normal;opacity:.65}}
.note{{background:var(--bg-raised);border:1px solid var(--line);border-radius:0 6px 6px 0;
  padding:.85rem 1.05rem;font-size:.84rem;color:var(--ink-dim);margin-top:1rem}}
.warn-note{{border-left:3px solid var(--warn)}}
.note strong{{color:var(--ink)}}
.empty{{color:var(--ink-dim);font-size:.83rem;font-style:italic;padding:.5rem .65rem}}
footer{{max-width:1060px;margin:0 auto;padding:1.2rem 1.5rem 0;color:var(--ink-dim);
  font-size:.76rem;border-top:1px solid var(--line)}}
footer code{{font-family:var(--mono);background:var(--accent-soft);color:var(--accent);
  padding:.05rem .3rem;border-radius:3px}}
#refresh{{font-family:var(--mono);font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;
  color:var(--ink-dim);background:var(--bg-raised);border:1px solid var(--line);
  border-radius:4px;padding:.25rem .6rem;margin-top:.5rem;cursor:pointer}}
#refresh:hover{{color:var(--accent);border-color:var(--accent)}}
#refresh:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
#age.stale{{color:var(--warn)}}
#age.very-stale{{color:var(--crit)}}
</style>

<div class="wrap">
<header class="top">
  <div>
    <p class="eyebrow">Dreamer · vault shape</p>
    <h1>{L['total']} loops, {CN['total']} conclusions</h1>
    <p class="subtitle">A live projection of what the vault holds and how the
      nightly jobs are behaving. Regenerated from the pages themselves — refresh
      to re-read.</p>
  </div>
  <div class="asof">
    <strong id="age" data-generated="{_e(d['generated_at'])}">{_e(d['generated_at'].replace('T', ' '))}</strong>
    logical date {_e(d['today'])}
    <button id="refresh" type="button">Refresh</button>
  </div>
</header>

<div class="status-strip">{status_html}</div>

<section>
  <h2>Loop population</h2>
  <p class="section-note">Tracking starts at the first occurrence, so most loops
    sit at <code>open</code> long before they ever qualify for research. That
    backlog is expected, not a queue to drain.</p>
  <div class="card-grid">
    <div class="card"><div class="num">{L['total']}</div><div class="cap">live loops</div></div>
    <div class="card"><div class="num">{L['statuses'].get('open', 0)}</div><div class="cap">open</div></div>
    <div class="card"><div class="num">{L['statuses'].get('researching', 0)}</div><div class="cap">researching</div></div>
    <div class="card"><div class="num">{L['statuses'].get('paused', 0)}</div><div class="cap">paused · concluded</div></div>
    <div class="card"><div class="num">{L['statuses'].get('decision-only', 0)}</div><div class="cap">decision-only</div></div>
    <div class="card"><div class="num">{L['archived']}</div><div class="cap">archived</div></div>
  </div>
  <div class="cols" style="margin-top:1.6rem">
    <div>
      <p class="section-note" style="margin-top:0"><strong style="color:var(--ink)">Recurrence
        — the relevance axis.</strong> Average {L['recurrence']['avg']}, top loop seen
        {L['recurrence']['max']}×. Loops at 1 are tracked but not yet earning research.</p>
      {hist_html}
    </div>
    <div>
      <p class="section-note" style="margin-top:0"><strong style="color:var(--ink)">Route.</strong>
        Assigned only at research time — {unrouted} loops are unrouted simply because
        they have not been selected yet.</p>
      {_bars(L['routes'], L['total'])}
    </div>
  </div>
</section>

<section>
  <h2>Themes</h2>
  <p class="section-note">Approved-vocabulary tags by loop count — what the vault is
    actually about, as opposed to what it was built for.</p>
  {tags_html}
</section>

<section>
  <h2>Conclusion quality</h2>
  <p class="section-note">Structural rubric scores. A low score means "probably not
    worth your ten minutes", never "wrong" — it measures whether a page decided
    anything and cited it, not whether the answer is true.</p>
  <div class="cols">
    <div>
      {quality_html}
      <p class="section-note" style="margin-top:1rem">Average
        {_e(CN['avg_score'] if CN['avg_score'] is not None else '—')} ·
        {CN['low_scoring']} below the 0.7 line ·
        deepest supersession chain {CN['max_chain']}.</p>
    </div>
    <div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Most common rubric failure</th><th class="num-cell">Pages</th></tr></thead>
          <tbody>{fails_html}</tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<section>
  <h2>Served, not re-researched</h2>
  <p class="section-note">Concluded loops that resurfaced and were answered from the
    existing page — zero research calls, the novelty gate doing its job.
    {("Source: " + _e(d.get("served_from"))) if served else ""}</p>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Loop</th><th>Title</th><th class="num-cell">Recur.</th><th>Why served</th></tr></thead>
      <tbody>{served_html}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>Job history</h2>
  <p class="section-note">{R['total']} recorded invocations. Cost is in notional
    units — under a subscription it tracks API-equivalent pricing, not money spent.</p>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Job</th><th class="num-cell">Runs</th><th class="num-cell">Cost</th><th class="num-cell">Deferred</th></tr></thead>
      <tbody>{job_rows}</tbody>
    </table>
  </div>
  {outlier_html}
</section>

<section>
  <h2>Recent run events</h2>
  <p class="section-note">Staged for the next digest — deferrals, recoveries,
    resurfacings, and quality warnings the jobs wanted you to see.</p>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>When</th><th>Job</th><th>Kind</th><th>Detail</th></tr></thead>
      <tbody>{events_html}</tbody>
    </table>
  </div>
</section>
</div>

<footer>
  Read-only projection of the vault — it writes nothing and calls no model.
  Refresh on demand with <code>bin/dashboard.sh</code>, or
  <code>bin/dashboard.sh --serve</code> to make the button above regenerate live.
  Otherwise it rebuilds on every job commit, plus a weekly backstop.
</footer>

<script>
// Age readout. With a weekly backstop cadence, "when was this true?" matters as
// much as the numbers — a stale page that looks current is the failure mode.
(function () {{
  var el = document.getElementById('age');
  if (!el) return;
  var gen = new Date(el.getAttribute('data-generated'));
  if (isNaN(gen)) return;
  var exact = el.textContent;

  function tick() {{
    var mins = Math.floor((Date.now() - gen.getTime()) / 60000);
    var rel;
    if (mins < 1) rel = 'just now';
    else if (mins < 60) rel = mins + 'm ago';
    else if (mins < 1440) rel = Math.floor(mins / 60) + 'h ago';
    else rel = Math.floor(mins / 1440) + 'd ago';
    el.textContent = rel === 'just now' ? exact : exact + ' · ' + rel;
    el.classList.toggle('stale', mins >= 1440);
    el.classList.toggle('very-stale', mins >= 10080);
  }}
  tick();
  setInterval(tick, 30000);

  // Under --serve every request regenerates, so a reload is a true refresh.
  // Opened as a file:// page it re-reads whatever is on disk, which still picks
  // up a job- or cron-written rebuild. Both are honest; neither can run a shell.
  var btn = document.getElementById('refresh');
  if (btn) btn.addEventListener('click', function () {{
    btn.textContent = 'Refreshing…';
    location.reload();
  }});
}})();
</script>
"""


def serve(port: int) -> int:
    """Serve the dashboard, regenerating on every request.

    The point of this mode is to make the page's Refresh button mean something.
    A file:// page can only re-read whatever is on disk; here a reload re-reads
    the vault itself, which is what you want while watching a run progress.

    Bound to loopback deliberately. The page reports on private reasoning
    threads, and there is no auth in front of it.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            if self.path not in ("/", "/index.html", "/dashboard.html"):
                self.send_error(404)
                return
            try:
                body = render(collect()).encode("utf-8")
            except Exception as exc:  # a broken chart must not kill the server
                body = (f"<h1>Dashboard failed to build</h1><pre>{html.escape(str(exc))}"
                        f"</pre>").encode("utf-8")
                self.send_response(500)
            else:
                self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Always revalidate, or the browser serves the very staleness this
            # mode exists to eliminate.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep the terminal readable
            pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard live at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None,
                    help="output HTML path (default: vault/dashboard.html)")
    ap.add_argument("--json", action="store_true",
                    help="print collected stats as JSON and write no HTML")
    ap.add_argument("--serve", nargs="?", type=int, const=8787, default=None,
                    metavar="PORT",
                    help="serve on localhost, regenerating per request (default port 8787)")
    args = ap.parse_args()

    if args.serve is not None:
        return serve(args.serve)

    data = collect()

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    out = args.out or (C.p("vault") / "dashboard.html")
    C.atomic_write(out, render(data))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
