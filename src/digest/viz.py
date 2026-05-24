"""Time-series SVG visualizations for P&C claims trend metrics.

Generates a self-contained HTML file for Obsidian's built-in HTML viewer.
Synthetic quarterly index series (2018Q1 = 100) scaffold each chart; a DB
overlay adds amber intensity bands for quarters with high-materiality triage
hits in social_inflation / reserving / commercial_specialty.

Output: {OBSIDIAN_VAULT}/81 P&C Digest/Viz/Personal Auto.html
CLI:    uv run digest viz [--open]
"""
from __future__ import annotations

import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from digest.config import settings

# ── Quarter labels ──────────────────────────────────────────────────────────

QUARTERS: list[str] = [f"{y}Q{q}" for y in range(2018, 2026) for q in range(1, 5)]
_N = 32

# ── SVG chart geometry ──────────────────────────────────────────────────────

_W, _H   = 860, 300
_ML, _MR = 65, 18
_MT, _MB = 32, 58
_PW      = _W - _ML - _MR   # 777
_PH      = _H - _MT - _MB   # 210


# ── Synthetic index data ─────────────────────────────────────────────────────
# 2018Q1 = 100. Based on CAS/ISO/NAIC published trend studies and carrier
# investor supplements. Replace with Schedule P actuals in Wave 3.

_BI_F = [
    100.0,99.1,98.3,97.5,  96.8,95.7,94.8,94.0,
     93.2,74.1,71.8,78.4,  82.6,85.9,84.7,83.5,
     82.3,81.4,80.6,79.8,  78.9,78.1,77.4,76.7,
     76.0,75.4,74.8,74.2,  73.7,73.2,72.7,72.2,
]
_BI_S = [
    100.0,102.1,104.3,106.5,  108.8,111.2,113.6,116.1,
    118.7,117.9,118.4,121.6,  127.4,134.2,140.8,147.1,
    154.6,161.9,167.3,172.4,  176.8,180.3,183.1,185.6,
    187.4,189.1,190.8,192.1,  193.4,194.6,195.7,196.8,
]

_PD_F = [
    100.0,99.3,98.7,98.0,  97.4,96.8,96.2,95.6,
     95.0,67.4,65.2,72.8,  79.6,84.1,87.3,89.0,
     89.8,90.4,90.9,91.2,  91.0,90.7,90.3,89.9,
     89.4,89.0,88.5,88.1,  87.7,87.3,86.9,86.5,
]
_PD_S = [
    100.0,101.4,102.8,104.3,  105.8,107.3,108.8,110.4,
    112.0,111.3,112.8,115.7,  120.3,129.8,138.4,146.2,
    152.9,156.3,158.8,160.4,  161.3,162.0,162.4,162.9,
    163.5,164.2,164.9,165.4,  165.9,166.3,166.7,167.1,
]

_PIP_F = [
    100.0,98.8,97.6,96.5,  95.4,94.3,93.3,92.3,
     91.4,76.2,73.5,78.9,  82.4,84.1,83.6,83.1,
     82.5,82.0,81.5,81.0,  80.4,79.9,79.3,78.8,
     78.2,77.7,77.1,76.6,  76.1,75.6,75.1,74.6,
]
_PIP_S = [
    100.0,101.5,103.0,104.6,  106.2,107.8,109.4,111.1,
    112.8,111.4,112.1,115.0,  119.4,124.8,129.7,134.1,
    138.3,141.6,144.2,146.3,  147.9,149.2,150.4,151.5,
    152.6,153.5,154.4,155.2,  156.0,156.8,157.5,158.2,
]

_COMP_F = [
    100.0,103.2,98.6,105.4,  101.8,108.3,97.4,103.1,
    104.2, 95.6,98.3,101.7,  107.4,110.2,105.6,109.8,
    112.3,108.7,115.1,111.4,  104.8,112.3,106.9,110.2,
    108.7,113.5,107.4,111.9,  110.3,114.6,108.8,112.3,
]
_COMP_S = [
    100.0,102.3,104.7,107.2,  109.7,112.3,114.9,117.6,
    120.4,119.6,121.8,126.3,  133.7,148.9,159.4,166.8,
    171.4,174.2,175.8,176.9,  175.3,173.8,172.4,171.6,
    172.1,172.8,173.5,174.2,  174.9,175.6,176.3,177.0,
]

_COLL_F = [
    100.0,99.4,98.8,98.2,  97.6,97.1,96.5,96.0,
     95.5,62.3,59.8,68.4,  75.6,80.8,84.2,86.3,
     87.4,88.2,88.7,89.0,  88.6,88.2,87.8,87.4,
     86.9,86.5,86.1,85.7,  85.3,84.9,84.5,84.1,
]
_COLL_S = [
    100.0,102.0,104.0,106.1,  108.2,110.4,112.6,114.9,
    117.2,115.8,117.4,121.4,  128.3,142.6,154.8,163.9,
    170.4,175.8,179.2,181.6,  182.8,183.7,184.4,185.0,
    185.8,186.6,187.4,188.2,  189.0,189.8,190.6,191.4,
]


def _pp(f: list[float], s: list[float]) -> list[float]:
    return [round(a * b / 100, 1) for a, b in zip(f, s)]


# (slug, display title, [(label, values, color, dash), ...], analyst note)
_COVERAGES: list[tuple[str, str, list[tuple[str, list[float], str, str]], str]] = [
    (
        "bi", "Bodily Injury (BI)",
        [
            ("Frequency",    _BI_F,           "#4A90D9", ""),
            ("Severity",     _BI_S,           "#E05C5C", ""),
            ("Pure Premium", _pp(_BI_F,_BI_S),"#9B59B6", "5 3"),
        ],
        "Frequency driven by ADAS/telematics adoption and VMT decline. "
        "Severity reflects social inflation acceleration and court backlogs post-COVID. "
        "Pure premium ending 2025 ~42% above 2018 baseline despite frequency decline.",
    ),
    (
        "pd", "Property Damage (PD)",
        [
            ("Frequency",    _PD_F,           "#4A90D9", ""),
            ("Severity",     _PD_S,           "#E05C5C", ""),
            ("Pure Premium", _pp(_PD_F,_PD_S),"#9B59B6", "5 3"),
        ],
        "Frequency partially recovered post-COVID but structurally below pre-pandemic. "
        "Severity spiked 2021–2022 on OEM parts shortage and supply chain disruption; "
        "moderating but still elevated ~65% above 2018 baseline.",
    ),
    (
        "pip", "PIP / Medical Payments",
        [
            ("Frequency",    _PIP_F,            "#4A90D9", ""),
            ("Severity",     _PIP_S,            "#E05C5C", ""),
            ("Pure Premium", _pp(_PIP_F,_PIP_S),"#9B59B6", "5 3"),
        ],
        "Frequency declining in no-fault reform states (MI, FL, NY). "
        "Severity tracks medical CPI plus increased attorney representation rates. "
        "Watch for post-reform frequency stabilization in MI (2019 reform) and FL (2023 reform).",
    ),
    (
        "comp", "Comprehensive",
        [
            ("Frequency",    _COMP_F,             "#4A90D9", ""),
            ("Severity",     _COMP_S,             "#E05C5C", ""),
            ("Pure Premium", _pp(_COMP_F,_COMP_S),"#9B59B6", "5 3"),
        ],
        "Frequency is weather-driven (hail, wildfire, flood) — high quarterly volatility is "
        "structural, not noise. Severity elevated by total loss frequency and used-vehicle "
        "value normalization; CAT-load regime currently post_major_event.",
    ),
    (
        "collision", "Collision",
        [
            ("Frequency",    _COLL_F,             "#4A90D9", ""),
            ("Severity",     _COLL_S,             "#E05C5C", ""),
            ("Pure Premium", _pp(_COLL_F,_COLL_S),"#9B59B6", "5 3"),
        ],
        "Frequency still ~16% below pre-COVID baseline — WFH and urban driving pattern shifts. "
        "Severity sharply higher on total-loss ratio (EV battery replacement), OEM parts, "
        "and body shop labor. Pure premium approaching 160 — largest driver of personal auto "
        "rate need in hard market.",
    ),
]


# ── SVG helpers ─────────────────────────────────────────────────────────────


def _sx(i: int) -> float:
    return _ML + (i / (_N - 1)) * _PW


def _sy(v: float, vmin: float, vmax: float) -> float:
    return _MT + _PH * (1.0 - (v - vmin) / (vmax - vmin))


def _nice_ticks(vmin: float, vmax: float, n: int = 6) -> list[float]:
    step = max(5.0, round((vmax - vmin) / (n - 1) / 5) * 5)
    first = math.ceil(vmin / step) * step
    ticks, v = [], first
    while v <= vmax + 0.5:
        ticks.append(round(v, 1))
        v += step
    return ticks


def _path_d(vals: list[float], vmin: float, vmax: float) -> str:
    pts = [(_sx(i), _sy(v, vmin, vmax)) for i, v in enumerate(vals)]
    return "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def _chart_svg(
    slug: str,
    series: list[tuple[str, list[float], str, str]],
    db_bands: dict[str, float],
) -> str:
    all_vals = [v for _, vals, _, _ in series for v in vals]
    raw_min, raw_max = min(all_vals), max(all_vals)
    pad = (raw_max - raw_min) * 0.07
    ticks = _nice_ticks(raw_min - pad, raw_max + pad)
    vmin, vmax = min(ticks) - 0.1, max(ticks) + 0.1

    p: list[str] = []
    p.append(
        f'<svg id="chart-{slug}" class="chart-svg" viewBox="0 0 {_W} {_H}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:{_W}px;display:block;">'
    )

    # DB intensity bands
    q_w = _PW / (_N - 1)
    for qi, q in enumerate(QUARTERS):
        intensity = db_bands.get(q, 0.0)
        if intensity > 0.2:
            op = min(0.30, 0.05 + 0.28 * intensity)
            x = _sx(qi) - q_w / 2
            p.append(
                f'<rect x="{x:.1f}" y="{_MT}" width="{q_w:.1f}" height="{_PH}" '
                f'fill="#F5A623" fill-opacity="{op:.3f}"/>'
            )

    # Horizontal grid lines
    for tick in ticks:
        y = _sy(tick, vmin, vmax)
        p.append(
            f'<line x1="{_ML}" y1="{y:.1f}" x2="{_ML+_PW}" y2="{y:.1f}" '
            f'stroke="currentColor" stroke-opacity="0.09" stroke-width="1"/>'
        )

    # Baseline at 100
    if vmin < 100 < vmax:
        y100 = _sy(100.0, vmin, vmax)
        p.append(
            f'<line x1="{_ML}" y1="{y100:.1f}" x2="{_ML+_PW}" y2="{y100:.1f}" '
            f'stroke="#888" stroke-opacity="0.45" stroke-width="1" stroke-dasharray="4 3"/>'
        )
        p.append(
            f'<text x="{_ML-5}" y="{y100+4:.1f}" text-anchor="end" '
            f'font-size="9" fill="#888" fill-opacity="0.75">100</text>'
        )

    # Data lines
    for label, vals, color, dash in series:
        d = _path_d(vals, vmin, vmax)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        p.append(
            f'<path d="{d}" fill="none" stroke="{color}" '
            f'stroke-width="2"{dash_attr} stroke-linejoin="round"/>'
        )

    # Dots at annual Q1 (index 0,4,8...28) and final Q4 (index 31)
    dot_indices = list(range(0, _N, 4)) + [_N - 1]
    for label, vals, color, _ in series:
        for qi in dot_indices:
            v = vals[qi]
            cx, cy = _sx(qi), _sy(v, vmin, vmax)
            p.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" '
                f'fill="{color}" stroke="var(--card-bg,#1e1e2e)" stroke-width="1.2" '
                f'fill-opacity="0.88" style="cursor:default">'
                f'<title>{QUARTERS[qi]} — {label}: {v:.1f}</title>'
                f'</circle>'
            )

    # X-axis
    y_ax = _MT + _PH
    p.append(
        f'<line x1="{_ML}" y1="{y_ax}" x2="{_ML+_PW}" y2="{y_ax}" '
        f'stroke="currentColor" stroke-opacity="0.35" stroke-width="1"/>'
    )
    for yi, year in enumerate(range(2018, 2026)):
        x = _sx(yi * 4)
        p.append(
            f'<line x1="{x:.1f}" y1="{y_ax}" x2="{x:.1f}" y2="{y_ax+4}" '
            f'stroke="currentColor" stroke-opacity="0.35" stroke-width="1"/>'
        )
        p.append(
            f'<text x="{x:.1f}" y="{y_ax+15}" text-anchor="middle" '
            f'font-size="11" fill="currentColor" fill-opacity="0.65">{year}</text>'
        )

    # Y-axis
    p.append(
        f'<line x1="{_ML}" y1="{_MT}" x2="{_ML}" y2="{_MT+_PH}" '
        f'stroke="currentColor" stroke-opacity="0.35" stroke-width="1"/>'
    )
    for tick in ticks:
        y = _sy(tick, vmin, vmax)
        p.append(
            f'<text x="{_ML-6}" y="{y+4:.1f}" text-anchor="end" '
            f'font-size="10" fill="currentColor" fill-opacity="0.65">{int(tick)}</text>'
        )

    # Y-axis rotated label
    mid_y = _MT + _PH / 2
    p.append(
        f'<text x="{_ML-46}" y="{mid_y:.1f}" text-anchor="middle" '
        f'font-size="10" fill="currentColor" fill-opacity="0.5" '
        f'transform="rotate(-90,{_ML-46:.1f},{mid_y:.1f})">'
        f'Index (2018Q1 = 100)</text>'
    )

    # Legend (top-right)
    lx = _ML + _PW - 8
    for i, (label, _, color, dash) in enumerate(series):
        ly = _MT + 12 + i * 16
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        p.append(
            f'<line x1="{lx-28}" y1="{ly}" x2="{lx-6}" y2="{ly}" '
            f'stroke="{color}" stroke-width="2"{dash_attr}/>'
        )
        p.append(
            f'<text x="{lx-32}" y="{ly+4}" text-anchor="end" '
            f'font-size="10" fill="{color}">{label}</text>'
        )

    # DB band legend if any bands shown
    if any(db_bands.get(q, 0) > 0.2 for q in QUARTERS):
        p.append(
            f'<rect x="{_ML+5}" y="{_MT+5}" width="10" height="10" '
            f'fill="#F5A623" fill-opacity="0.3" rx="2"/>'
        )
        p.append(
            f'<text x="{_ML+19}" y="{_MT+14}" font-size="9" '
            f'fill="currentColor" fill-opacity="0.55">'
            f'Elevated liability signal (DB)</text>'
        )

    p.append("</svg>")
    return "\n".join(p)


# ── DB intensity query ──────────────────────────────────────────────────────


def _query_db_intensity() -> dict[str, float]:
    """Map quarter strings to [0,1] signal intensity from the digest DB."""
    sql = """
        SELECT
            strftime('%Y', published_at) || 'Q' ||
            (((CAST(strftime('%m', published_at) AS INTEGER) - 1) / 3) + 1)
            AS quarter,
            AVG(COALESCE(materiality_score, triage_score, 0.5)) AS intensity
        FROM items
        WHERE topic IN ('social_inflation','reserving','commercial_specialty')
          AND triage_decision = 'keep'
          AND published_at IS NOT NULL
          AND strftime('%Y', published_at) BETWEEN '2018' AND '2025'
        GROUP BY quarter
    """
    try:
        with sqlite3.connect(settings.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
        if not rows:
            return {}
        raw = {r["quarter"]: float(r["intensity"]) for r in rows}
        vmax = max(raw.values()) or 1.0
        return {q: round(v / vmax, 3) for q, v in raw.items()}
    except Exception:
        return {}


# ── CSS ─────────────────────────────────────────────────────────────────────

_CSS = """\
:root{--bg:#1e1e2e;--card:#2a2a3e;--text:#cdd6f4;--sub:#a6adc8;
  --accent:#cba6f7;--border:#45475a;--notebg:#313244}
@media(prefers-color-scheme:light){:root{--bg:#f8f8f8;--card:#fff;
  --text:#1e1e2e;--sub:#6c7086;--accent:#7c3aed;--border:#e2e8f0;--notebg:#f1f5f9}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  font-size:14px;line-height:1.5;padding:24px;max-width:960px;margin:0 auto}
h1{font-size:21px;font-weight:600;margin-bottom:4px}
.regime{display:inline-block;background:var(--accent);color:var(--bg);
  padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-left:8px;
  vertical-align:middle}
.meta{color:var(--sub);font-size:12px;margin-bottom:20px}
nav{margin-bottom:20px;display:flex;gap:10px;flex-wrap:wrap}
nav a{color:var(--accent);text-decoration:none;font-size:13px;
  padding:3px 10px;border:1px solid var(--border);border-radius:4px}
nav a:hover{background:var(--card)}
.cov{background:var(--card);border:1px solid var(--border);border-radius:8px;
  padding:16px 18px;margin-bottom:18px}
.cov h2{font-size:15px;font-weight:600;margin-bottom:6px}
.note{font-size:12px;color:var(--sub);margin-bottom:12px;background:var(--notebg);
  padding:6px 10px;border-radius:4px;border-left:3px solid var(--accent)}
.foot-note{font-size:11px;color:var(--sub);margin-top:6px;font-style:italic}
footer{margin-top:28px;padding-top:10px;border-top:1px solid var(--border);
  font-size:11px;color:var(--sub)}
"""


# ── HTML assembly ───────────────────────────────────────────────────────────


def generate_personal_auto_html(db_bands: dict[str, float] | None = None) -> str:
    if db_bands is None:
        db_bands = _query_db_intensity()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        from digest.regime import current_regime
        r = current_regime()
        regime_txt = r.summary_line() if r else "regime unknown"
    except Exception:
        regime_txt = "regime unknown"

    nav = " ".join(
        f'<a href="#{slug}">{title}</a>'
        for slug, title, _, _ in _COVERAGES
    )
    has_bands = any(db_bands.get(q, 0) > 0.2 for q in QUARTERS)
    foot_note = (
        "⊕ Amber bands = quarters with elevated liability signal intensity (digest DB materiality scores)."
        if has_bands else
        "Synthetic index data only — no DB signal overlay yet for this coverage period."
    )

    sections: list[str] = []
    for slug, title, series, note in _COVERAGES:
        chart = _chart_svg(slug, series, db_bands)
        sections.append(f"""
<div class="cov" id="{slug}">
  <h2>{title}</h2>
  <div class="note">{note}</div>
  {chart}
  <p class="foot-note">{foot_note}</p>
</div>""")

    n_bands = len([q for q in QUARTERS if db_bands.get(q, 0) > 0.2])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Personal Auto — Claims Trend Index</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Personal Auto — Claims Trend Index<span class="regime">{regime_txt}</span></h1>
<p class="meta">Generated {now} &nbsp;·&nbsp; Synthetic scaffold 2018Q1–2025Q4 (index = 100)
&nbsp;·&nbsp; Hover dots for quarter values &nbsp;·&nbsp; {n_bands} quarters with DB signal overlay</p>
<nav>{nav}</nav>
{"".join(sections)}
<footer>
  Synthetic series derived from CAS trend studies, ISO/Verisk industry averages, and published
  carrier investor supplements. DB intensity bands sourced from digest materiality scores
  (social_inflation · reserving · commercial_specialty). Replace synthetic series with NAIC
  Schedule P actuals when Wave 3 actuarial pipeline ships.
</footer>
</body>
</html>"""


# ── Write entry point ───────────────────────────────────────────────────────


def write_viz_pages(open_after: bool = False) -> list[Path]:
    """Generate HTML viz pages into the Obsidian vault. Returns paths written."""
    from digest.obsidian import Paths

    paths = Paths.resolve()
    viz_dir = paths.digest_root / "Viz"
    viz_dir.mkdir(parents=True, exist_ok=True)

    db_bands = _query_db_intensity()
    out = viz_dir / "Personal Auto.html"
    out.write_text(generate_personal_auto_html(db_bands), encoding="utf-8")

    if open_after and sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)

    return [out]
