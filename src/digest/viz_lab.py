"""Viz Lab — side-by-side eval harness for candidate Markdown data-viz techniques.

Renders every candidate visualization against the SAME live SQLite slice into one
note — `_meta/Viz Lab (YYYY-MM-DD).md` — each under its own H2 with a scorecard
stub. Lets the user eyeball which techniques render cleanly on desktop + iOS
before any get promoted into the daily-note render path (Phase B) or a dashboard
(Phase C).

Additive only: reads SQLite, writes one Markdown file. No pipeline / daily-note
change. Every renderer degrades gracefully — when its EKG lead has no data yet
(the ingestors are sparse until they mature) it emits a "no data" note so the
*technique* still renders structurally.

CLI: `uv run digest viz --lab`
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

from digest import db
from digest.config import settings
from digest_core.catalog import sparkline

# ── Unicode helpers (zero-dependency, mobile-safe) ───────────────────────────

_BRAILLE_BASE = 0x2800
# Dot bit values, per column, bottom→top. Left col dots 7,3,2,1; right col 8,6,5,4.
_LEFT_DOTS  = (0x40, 0x04, 0x02, 0x01)
_RIGHT_DOTS = (0x80, 0x20, 0x10, 0x08)


def _level(v: float, vmin: float, vmax: float) -> int:
    """Map a value to a 0–4 fill height (0 = empty column)."""
    if vmax <= vmin:
        return 2
    frac = (v - vmin) / (vmax - vmin)
    return max(0, min(4, round(frac * 4)))


def braille_line(values: list[float]) -> str:
    """Render a numeric series as a braille micro-line-chart (2 points per char)."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    vmin, vmax = min(vals), max(vals)
    out: list[str] = []
    for i in range(0, len(vals), 2):
        left = _level(vals[i], vmin, vmax)
        right = _level(vals[i + 1], vmin, vmax) if i + 1 < len(vals) else 0
        bits = 0
        for lvl in range(left):          # fill from bottom up
            bits |= _LEFT_DOTS[lvl]
        for lvl in range(right):
            bits |= _RIGHT_DOTS[lvl]
        out.append(chr(_BRAILLE_BASE + bits))
    return "".join(out)


def gauge_bar(frac: float, width: int = 12) -> str:
    """A `███░░` fill bar for a 0–1 fraction."""
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)


def _dir_arrow(net: int | float | None) -> str:
    if net is None:
        return "→"
    return "↑" if net > 0 else "↓" if net < 0 else "→"


def _no_data(cmd: str) -> str:
    return f"_No data yet — run `{cmd}` once its ingestor has matured._"


# ── Regime axis mapping (shared by quadrant + gauges) ────────────────────────

_CYCLE_X = {
    "soft_market": 0.10, "transitioning_to_soft": 0.30, "stable": 0.50,
    "transitioning_to_hard": 0.70, "hard_market": 0.90,
}
_CATLOAD_Y = {"low_season": 0.15, "active_season": 0.50, "post_major_event": 0.85}


def regime_xy(market_cycle: str, cat_load: str) -> tuple[float, float]:
    """Map a (market_cycle, cat_load) regime to quadrant [x, y] in 0–1."""
    return _CYCLE_X.get(market_cycle, 0.5), _CATLOAD_Y.get(cat_load, 0.15)


# ── Option 6 — Regime Quadrant Map (Mermaid quadrantChart, zero-plugin) ──────

def render_regime_quadrant() -> str:
    rows = db.recent_regime_signals(n=6)
    if not rows:
        return _no_data("digest regime --force")
    lines = [
        "```mermaid",
        "quadrantChart",
        "  title Market Regime — cycle × cat-load (trail = last recomputes)",
        "  x-axis Soft --> Hard",
        "  y-axis Low-Season --> Post-Major-Event",
        "  quadrant-1 Hard + High-CAT",
        "  quadrant-2 Soft + High-CAT",
        "  quadrant-3 Soft + Calm",
        "  quadrant-4 Hard + Calm",
    ]
    # rows are newest-first. Collapse identical positions so a stable regime shows
    # ONE point (not 3 labels stacked on the same dot) — the trail plots only the
    # distinct positions the market has actually occupied, labelled Now, T-1, T-2…
    seen: set[tuple[float, float]] = set()
    plotted = 0
    for r in rows:
        x, y = regime_xy(r["market_cycle"], r["cat_load"])
        key = (round(x, 2), round(y, 2))
        if key in seen:
            continue
        seen.add(key)
        label = "Now" if plotted == 0 else f"T-{plotted}"
        lines.append(f'  "{label}": [{x:.2f}, {y:.2f}]')
        plotted += 1
    lines.append("```")
    cur = rows[0]
    lines.append("")
    lines.append(f"> **Now:** {cur['market_cycle']} × {cat_load_label(cur['cat_load'])} "
                 f"→ ×{float(cur['multiplier']):.2f}")
    return "\n".join(lines)


def cat_load_label(slug: str) -> str:
    return {"low_season": "low season", "active_season": "active season",
            "post_major_event": "post-major-event"}.get(slug, slug)


# ── Option 8 — Severity tape: braille vs Mermaid xychart-beta ────────────────

def _severity_history(limit: int = 12) -> list[sqlite3.Row]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT observation_date, value, zscore_12m FROM severity_index
               WHERE index_name='blended_severity'
               ORDER BY observation_date DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return list(reversed(rows))   # oldest → newest for charting


def _severity_drivers() -> list[tuple[str, float]]:
    """Latest-month m/m z-score per cost-driver category (parts/labor/…), hottest first."""
    with db.get_conn() as conn:
        latest = conn.execute(
            "SELECT MAX(observation_date) FROM severity_index WHERE index_name LIKE 'fred_%'"
        ).fetchone()[0]
        if not latest:
            return []
        rows = conn.execute(
            "SELECT category, value FROM severity_index "
            "WHERE index_name LIKE 'fred_%' AND observation_date=?",
            (latest,),
        ).fetchall()
    agg: dict[str, float] = {}
    for r in rows:                       # several series can share a category → keep the hottest
        cat, z = (r["category"] or "other"), float(r["value"] or 0.0)
        if cat not in agg or abs(z) > abs(agg[cat]):
            agg[cat] = z
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)


def _z_chip(z: float) -> str:
    return "🔴" if z >= 0.5 else "🟠" if z >= 0.2 else "🔵" if z <= -0.2 else "⚪"


def render_severity_drivers() -> str:
    drivers = _severity_drivers()
    if not drivers:
        return _no_data("digest severity-tape")
    lines = ["**Loss-cost severity — driver breakdown** (latest-month m/m z-scores):", ""]
    for cat, z in drivers:
        bar = "█" * min(10, round(abs(z) * 6))
        lines.append(f"- {_z_chip(z)} `{cat:<12}` {z:+.2f}σ  {bar}")
    blended = db.latest_severity_index("blended_severity")
    if blended is not None:
        bz = float(blended["value"])
        lines += ["", f"**Blended:** {bz:+.2f}σ  `{gauge_bar((bz + 3) / 6)}`"]
    hist = _severity_history()           # add a trend braille once >1 month accumulates
    if len(hist) > 1:
        lines.append(f"Trend `{braille_line([float(r['value']) for r in hist])}` ({len(hist)} mo)")
    return "\n".join(lines)


# ── Option 4b — Litigation pulse (block sparkline) ───────────────────────────

def render_litigation_pulse() -> str:
    vel = db.courtlistener_docket_velocity(30)
    row = db.latest_litigation_pressure()
    if vel == 0 and row is None:                 # nothing live yet — hide from the header
        return _no_data("digest litigation")
    lines = [f"- **Federal P&C docket velocity:** {vel:.2f}/day (last 30d) — live sub-signal"]
    # Composite index history sparkline, when it has actually moved.
    with db.get_conn() as conn:
        hist = conn.execute(
            """SELECT pressure_index FROM litigation_pressure
               WHERE state='US' AND sector='all' ORDER BY as_of DESC LIMIT 12""",
        ).fetchall()
    pis = [float(r["pressure_index"] or 0) for r in reversed(hist)]
    pi = pis[-1] if pis else 0.0
    if any(v > 0 for v in pis):
        spark = sparkline([int(round(v * 100)) for v in pis], width=len(pis))
        lines.append(f"- **Composite litigation pressure:** `{spark}` latest **{pi:.2f}** "
                     f"{_dir_arrow(pi - pis[0])}")
    elif row is not None:
        lines.append("- **Composite litigation pressure:** 0.00 — pending verdict-count / "
                     "median-award / TPLF inputs (docket velocity is the only live input today)")
    else:
        lines.append("- **Composite litigation pressure:** _not computed — run `digest litigation`_")
    return "\n".join(lines)


# ── Option 4c — Regulatory burden bar chart (Unicode █ bars) ─────────────────

def render_burden_bars() -> str:
    rows = db.burden_by_state(window_days=90)
    if not rows:
        return _no_data("digest burden")
    top = rows[:8]
    wmax = max(int(r["weighted_burden"] or 0) for r in top) or 1
    light = {3: "🟥", 2: "🟧", 1: "🟨"}   # by avg weight bucket
    lines = ["| State | Pressure | Net dir | Items |", "|---|---|---|---|"]
    for r in top:
        wb = int(r["weighted_burden"] or 0)
        bar = "█" * max(1, round(wb / wmax * 12))
        avg = wb / max(1, int(r["n"] or 1))
        chip = light.get(round(avg), "🟩")
        lines.append(f"| {r['state']} {chip} | `{bar}` {wb} | "
                     f"{_dir_arrow(r['net_direction'])} | {r['n']} |")
    return "\n".join(lines)


# ── Option 7 — Reserve-adequacy heat-grid + Sankey ───────────────────────────

_LOB_ABBREV = [
    ("commercial_lines", "Comm"), ("personal_lines", "PL"), ("vehicles", "Veh"),
    ("agency", "Agcy"), ("direct", "Dir"), ("physical_damage", "PD"),
    ("liability", "Liab"), ("property", "Prop"),
]


def _abbrev_lob(lob: str) -> str:
    s = lob
    for long, short in _LOB_ABBREV:
        s = s.replace(long, short)
    return s.replace("_", " ").strip()


def _reserve_cell(direction: str, pct: float | None) -> str:
    p = abs(pct or 0.0)
    if direction == "adverse":
        return "🟥" if p >= 0.10 else "🟧" if p >= 0.03 else "🟨"
    if direction == "favorable":
        return "🟩"
    return "⬜"


def render_reserve_heatgrid() -> str:
    rows = db.latest_reserving_signals(limit=200)
    if not rows:
        return _no_data("digest reserving")
    insurers = sorted({r["insurer"] for r in rows})
    lobs = sorted({r["lob"] for r in rows})
    cell: dict[tuple[str, str], str] = {}
    for r in rows:
        # keep the most severe cell per (insurer, lob)
        c = _reserve_cell(r["direction"], r["deterioration_pct"])
        cell[(r["insurer"], r["lob"])] = c
    header = "| insurer \\ LOB | " + " | ".join(_abbrev_lob(l) for l in lobs) + " |"
    sep = "|" + "---|" * (len(lobs) + 1)
    lines = [header, sep]
    for ins in insurers:
        cells = " | ".join(cell.get((ins, lob), "·") for lob in lobs)
        lines.append(f"| **{ins}** | {cells} |")
    lines.append("")
    lines.append("_🟥 adverse ≥10% · 🟧 ≥3% · 🟨 mild · 🟩 favorable · ⬜ neutral_")
    return "\n".join(lines)


def render_reserve_sankey() -> str:
    rows = db.latest_reserving_signals(limit=200)
    if not rows:
        return _no_data("digest reserving")
    # Flow magnitude = |deterioration_pct| scaled to integer weight per LOB→bucket.
    flows: dict[tuple[str, str], float] = {}
    for r in rows:
        pct = abs(r["deterioration_pct"] or 0.0)
        if pct <= 0:
            continue
        bucket = "Adverse" if r["direction"] == "adverse" else "Favorable"
        flows[(r["lob"], bucket)] = flows.get((r["lob"], bucket), 0.0) + pct
    if not flows:
        return "_Reserving rows present but no non-zero development to flow._"
    lines = ["```mermaid", "sankey-beta", ""]
    for (lob, bucket), w in sorted(flows.items()):
        lines.append(f"Reserves,{lob},{w * 100:.0f}")
        lines.append(f"{lob},{bucket},{w * 100:.0f}")
    lines.append("```")
    return "\n".join(lines)


# ── Option 10 — Catastrophe-season heatmap calendar (DataviewJS plugin) ───────

def _hazard_counts_by_day(year: int) -> list[tuple[str, int]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT substr(COALESCE(published_at, ingested_at),1,10) AS day,
                      COUNT(*) AS n
               FROM items
               WHERE source IN ('nhc','usgs','spc','nifc')
                 AND substr(COALESCE(published_at, ingested_at),1,4)=?
               GROUP BY day ORDER BY day""",
            (str(year),),
        ).fetchall()
    return [(r["day"], int(r["n"])) for r in rows if r["day"]]


def render_cat_heatmap() -> str:
    year = datetime.now(timezone.utc).year
    counts = _hazard_counts_by_day(year)
    if not counts:
        return _no_data("digest ingest nhc/usgs/spc/nifc")
    entries = ",\n    ".join(
        f'{{ date: "{day}", intensity: {n} }}' for day, n in counts
    )
    return "\n".join([
        "```dataviewjs",
        "// Needs the Heatmap Calendar community plugin (exposes renderHeatmapCalendar).",
        f"const entries = [\n    {entries}\n];",
        "renderHeatmapCalendar(this.container, {",
        f"  year: {year},",
        '  colors: { orange: ["#ffe0b2","#ffb74d","#ff9800","#f57c00","#e65100"] },',
        '  entries: entries.map(e => ({ date: e.date, intensity: e.intensity, content: "" })),',
        "});",
        "```",
    ])


# ── Option 4a — Unicode vital gauges (zero-plugin) ───────────────────────────

def render_unicode_gauges() -> str:
    lines: list[str] = []
    reg = db.latest_regime_signal()
    if reg is not None:
        mult = float(reg["multiplier"])
        # regime multiplier spans ~0.72 (0.85×0.85) to ~1.44 (1.20×1.20)
        frac = (mult - 0.72) / (1.44 - 0.72)
        lines.append(f"- **Regime mult** ×{mult:.2f}  `{gauge_bar(frac)}`")
    sev = db.latest_severity_index("blended_severity")
    if sev is not None and sev["zscore_12m"] is not None:
        z = float(sev["zscore_12m"])
        lines.append(f"- **Severity z** {z:+.2f}σ  `{gauge_bar((z + 3) / 6)}`")
    cat = db.latest_cat_nowcast("open_disaster_declarations")
    if cat is not None and cat["zscore_12m"] is not None:
        z = float(cat["zscore_12m"])
        lines.append(f"- **CAT-nowcast z** {z:+.2f}σ  `{gauge_bar((z + 3) / 6)}`")
    return "\n".join(lines) if lines else _no_data("digest regime / severity-tape / cat-nowcast")


# ── Assembly ─────────────────────────────────────────────────────────────────

# (H2 title, plugin requirement, renderer)
_SECTIONS = [
    ("6 · Regime Quadrant Map", "Mermaid (built-in)", render_regime_quadrant),
    ("4a · Vital Gauges (Unicode)", "none", render_unicode_gauges),
    ("8 · Loss-cost Severity — driver breakdown", "none", render_severity_drivers),
    ("4b · Litigation Pulse (docket velocity)", "none", render_litigation_pulse),
    ("4c · Regulatory Burden Bars", "none", render_burden_bars),
    ("7a · Reserve-Adequacy Heat-grid", "none", render_reserve_heatgrid),
    ("7b · Reserve Sankey", "Mermaid sankey-beta", render_reserve_sankey),
    ("10 · Catastrophe-Season Heatmap", "Heatmap Calendar + Dataview", render_cat_heatmap),
]

_SCORECARD = """\
| Technique | Renders desktop | Renders iOS | Theme-adaptive | Live data | Glanceable | Keep? |
|---|---|---|---|---|---|---|
""" + "\n".join(
    f"| {title} | | | | | | |" for title, _, _ in _SECTIONS
)


def build_viz_lab_md() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "---",
        "title: Viz Lab",
        f"generated: {now}",
        "type: eval",
        "tags: [pc-digest, viz, eval, _meta]",
        "---",
        "",
        "# Viz Lab — candidate Markdown visualizations",
        "",
        f"> Generated {now}. Each technique below renders against the **same live "
        "SQLite slice**. Open this note on **desktop and iOS** and fill the scorecard "
        "to decide which graduate to the daily-note EKG header (Phase B) and the "
        "Signal Desk dashboard (Phase C). Sparse EKG leads show a 'no data' note — "
        "that's expected until their ingestors mature.",
        "",
        "## Scorecard",
        "",
        _SCORECARD,
        "",
        "---",
        "",
    ]
    for title, plugin, render in _SECTIONS:
        lines.append(f"## {title}")
        lines.append(f"*Needs: {plugin}*")
        lines.append("")
        try:
            body = render()
        except Exception as exc:  # noqa: BLE001 — a lab note should never hard-fail
            body = f"_Renderer error: {exc}_"
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_viz_lab(open_after: bool = False):
    """Write the Viz Lab note into `_meta/`. Returns the path written."""
    from digest.obsidian import Paths

    paths = Paths.resolve()
    meta_dir = paths.digest_root / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = meta_dir / f"Viz Lab ({today}).md"
    out.write_text(build_viz_lab_md(), encoding="utf-8")
    if open_after and sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)
    return out
