"""Frequency × severity × pure-premium decomposition over the XBRL facts.

The ASC 944 short-duration disclosures already in insurer_xbrl_facts carry,
per accident year: cumulative incurred losses+ALAE (dataset='triangle',
field='incurred' — the LATEST evaluation is the carrier's current ultimate
estimate including IBNR re-estimates), reported claim counts
(dataset='claim_counts'), and earned premium by segment (dataset='premiums').
Combining them per accident year:

    severity      = incurred / reported claims          [product-line grain]
    frequency     = reported claims / earned premium    [segment grain]
    pure premium  = incurred / earned premium ( = frequency × severity )

Earned premium is the exposure PROXY — true exposure (car-years, house-years)
lives in statutory page 14 / Fast Track data, not GAAP — so "frequency" here is
claims per $M earned and its trend is net of rate changes. Segment rows
aggregate ONLY the product cells where counts AND incurred both exist, so
frequency × severity reconciles exactly to pure premium.

Trends are log-linear OLS over MATURE accident years (the latest AY is
excluded: at 12 months its counts and incurred are still developing) and
annualized. Detail rows persist to freq_sev_signals (+ silver mirror); trends
are cheap and recomputed on read via fit_trend()/trend_rows().

The distinctive cross-checks this feeds (see docs / next-ideas):
  (a) carrier-derived severity trend vs the FRED severity tape (Lead 3);
  (b) derived loss-cost trend vs the carrier's own SERFF rate ask.
"""
from __future__ import annotations

import logging
import math

import numpy as np

from digest import db

logger = logging.getLogger(__name__)

MIN_TREND_POINTS = 3         # log-linear OLS needs at least this many mature AYs


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _lob(segment: str | None, product: str | None, subsegment: str | None) -> str:
    return "_".join(p for p in (segment, product, subsegment) if p) or "all_lines"


def _latest_eval_by_ay(insurer: str, dataset: str, field: str | None = None) -> dict:
    """{(segment, product, subsegment, accident_year): value at MAX(period_end)}.

    Counts and triangle facts can carry several evaluations of the same accident
    year (one per 10-K column); the latest evaluation is the current estimate.
    """
    field_clause = "AND field=?" if field else ""
    params = (insurer, dataset) + ((field,) if field else ())
    out: dict[tuple, tuple[str, float]] = {}
    for r in _rows(
        f"""SELECT segment, product, subsegment, accident_year, period_end, value
            FROM insurer_xbrl_facts
            WHERE insurer=? AND dataset=? {field_clause}
              AND accident_year IS NOT NULL AND period_end IS NOT NULL""",
        params,
    ):
        key = (r["segment"], r["product"], r["subsegment"], r["accident_year"])
        prev = out.get(key)
        if prev is None or r["period_end"] > prev[0]:
            out[key] = (r["period_end"], r["value"])
    return {k: v for k, (_, v) in out.items()}


def _earned_premium_by_year(insurer: str) -> dict[tuple[str, int], float]:
    """{(segment_or_'all_lines', calendar_year): earned premium $M}.

    Prefers premiums_earned_net over the supplementary premium_revenue tagging;
    within a (segment, year) the LARGEST value wins — sibling facts on the same
    segment context are sub-cuts (channel, product) of the segment total, so the
    max is the total (summing would double-count). Calendar-year earned ≈
    accident-year earned for a steady book.
    """
    best: dict[tuple[str, int], tuple[int, float]] = {}
    for r in _rows(
        """SELECT segment, field, period_end, value FROM insurer_xbrl_facts
           WHERE insurer=? AND dataset='premiums'
             AND field IN ('premiums_earned_net', 'premium_revenue')
             AND period_type='duration' AND period_end IS NOT NULL""",
        (insurer,),
    ):
        year = int(r["period_end"][:4])
        key = (r["segment"] or "all_lines", year)
        rank = 1 if r["field"] == "premiums_earned_net" else 0
        prev = best.get(key)
        if prev is None or (rank, r["value"]) > prev:
            best[key] = (rank, r["value"])
    return {k: v for k, (_, v) in best.items()}


def derive_insurer(insurer: str) -> list[dict]:
    """freq_sev_signals detail rows for one insurer, both grains.

    Product grain ('product'): severity per accident year for every dimensional
    cell where counts AND incurred both exist. Segment grain ('segment'): those
    matched cells aggregated to the segment, joined to segment earned premium
    for frequency and pure premium.
    """
    tk = insurer.upper()
    counts = _latest_eval_by_ay(tk, "claim_counts")
    incurred = _latest_eval_by_ay(tk, "triangle", "incurred")
    if not counts or not incurred:
        return []
    as_of = _rows(
        "SELECT MAX(as_of) AS a FROM insurer_xbrl_facts WHERE insurer=?", (tk,)
    )[0]["a"]
    ep = _earned_premium_by_year(tk)

    detail: list[dict] = []
    seg_agg: dict[tuple[str, int], dict[str, float]] = {}
    for key, n_claims in counts.items():
        inc = incurred.get(key)
        if inc is None or n_claims is None or n_claims <= 0:
            continue
        segment, product, subsegment, ay = key
        detail.append({
            "insurer": tk, "grain": "product",
            "lob": _lob(segment, product, subsegment), "accident_year": ay,
            "reported_claims": n_claims, "incurred_musd": inc,
            "earned_premium_musd": None,
            "severity_usd": round(inc * 1_000_000.0 / n_claims, 2),
            "frequency_per_musd": None, "pure_premium_ratio": None,
            "as_of": as_of,
        })
        agg = seg_agg.setdefault((segment or "all_lines", ay),
                                 {"claims": 0.0, "incurred": 0.0})
        agg["claims"] += n_claims
        agg["incurred"] += inc

    for (segment, ay), agg in sorted(seg_agg.items()):
        ep_musd = ep.get((segment, ay))
        row = {
            "insurer": tk, "grain": "segment", "lob": segment, "accident_year": ay,
            "reported_claims": agg["claims"], "incurred_musd": round(agg["incurred"], 4),
            "earned_premium_musd": ep_musd,
            "severity_usd": round(agg["incurred"] * 1_000_000.0 / agg["claims"], 2),
            "frequency_per_musd": None, "pure_premium_ratio": None,
            "as_of": as_of,
        }
        if ep_musd:
            row["frequency_per_musd"] = round(agg["claims"] / ep_musd, 4)
            row["pure_premium_ratio"] = round(agg["incurred"] / ep_musd, 4)
        detail.append(row)
    return detail


def fit_trend(pairs: list[tuple[int, float]],
              min_points: int = MIN_TREND_POINTS) -> dict | None:
    """Annualized log-linear trend over (accident_year, value) pairs.

    OLS of ln(value) on accident year; returns {annual_trend, n, r2} where
    annual_trend = exp(slope) - 1 (e.g. 0.05 = +5%/yr). Non-positive values are
    dropped (log scale); None when fewer than min_points remain.
    """
    pts = [(ay, v) for ay, v in pairs if v is not None and v > 0]
    if len(pts) < min_points:
        return None
    x = np.array([p[0] for p in pts], dtype=float)
    y = np.log([p[1] for p in pts])
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {"annual_trend": round(math.exp(slope) - 1.0, 4),
            "n": len(pts), "r2": round(r2, 4)}


def trend_rows(detail: list[dict]) -> list[dict]:
    """Per (insurer, grain, lob) trend summary from detail rows.

    Trends fit over MATURE accident years only (AY strictly before the as-of
    year — the newest AY's counts and incurred are still developing at 12
    months). loss_cost_trend = (1+freq)(1+sev)-1, the rate-need proxy to hold
    against the carrier's own SERFF ask.

    A 10-K only carries three fiscal years of earned premium, so frequency /
    pure-premium trend fits start life short of MIN_TREND_POINTS; the *_yoy
    fields (latest mature AY vs the one before) are the honest fallback until
    more years accrue (or a historical-instance backfill extends the series).
    """

    def yoy(pairs: list[tuple[int, float | None]]) -> float | None:
        pts = sorted((ay, v) for ay, v in pairs if v is not None and v > 0)
        if len(pts) < 2 or pts[-1][0] - pts[-2][0] != 1:
            return None
        return round(pts[-1][1] / pts[-2][1] - 1.0, 4)

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in detail:
        groups.setdefault((r["insurer"], r["grain"], r["lob"]), []).append(r)

    out: list[dict] = []
    for (insurer, grain, lob), rows in sorted(groups.items()):
        as_of_year = max(int(r["as_of"][:4]) for r in rows if r["as_of"])
        mature = [r for r in rows if r["accident_year"] < as_of_year]
        sev_pairs = [(r["accident_year"], r["severity_usd"]) for r in mature]
        freq_pairs = [(r["accident_year"], r["frequency_per_musd"]) for r in mature]
        pp_pairs = [(r["accident_year"], r["pure_premium_ratio"]) for r in mature]
        sev = fit_trend(sev_pairs)
        freq = fit_trend(freq_pairs)
        pp = fit_trend(pp_pairs)
        freq_yoy, pp_yoy = yoy(freq_pairs), yoy(pp_pairs)
        if all(x is None for x in (sev, freq, pp, freq_yoy, pp_yoy)):
            continue
        latest = max(mature, key=lambda r: r["accident_year"], default=None)
        loss_cost = None
        if freq is not None and sev is not None:
            loss_cost = round((1 + freq["annual_trend"]) * (1 + sev["annual_trend"]) - 1, 4)
        out.append({
            "insurer": insurer, "grain": grain, "lob": lob,
            "ay_span": f"{min(r['accident_year'] for r in mature)}-"
                       f"{max(r['accident_year'] for r in mature)}" if mature else None,
            "severity_trend": sev["annual_trend"] if sev else None,
            "severity_n": sev["n"] if sev else 0,
            "severity_r2": sev["r2"] if sev else None,
            "frequency_trend": freq["annual_trend"] if freq else None,
            "frequency_yoy": freq_yoy,
            "pure_premium_trend": pp["annual_trend"] if pp else None,
            "pure_premium_yoy": pp_yoy,
            "loss_cost_trend": loss_cost if loss_cost is not None
                               else (pp["annual_trend"] if pp else None),
            "latest_mature_severity_usd": latest["severity_usd"] if latest else None,
            "latest_mature_ay": latest["accident_year"] if latest else None,
        })
    return out


def run_freq_sev(tickers: list[str] | None = None) -> dict[str, int]:
    """Derive + persist detail rows for every insurer with counts data."""
    if tickers:
        universe = [t.upper() for t in tickers]
    else:
        universe = [r["insurer"] for r in _rows(
            "SELECT DISTINCT insurer FROM insurer_xbrl_facts WHERE dataset='claim_counts' ORDER BY insurer"
        )]
    written = 0
    covered = 0
    for tk in universe:
        detail = derive_insurer(tk)
        if not detail:
            continue
        db.upsert_freq_sev(detail)
        written += len(detail)
        covered += 1
    logger.info("freq_sev: %d rows across %d insurers", written, covered)
    return {"insurers": covered, "rows": written}
