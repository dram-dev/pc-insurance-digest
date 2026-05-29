"""Reserving quant (Databricks Option 5) — chain-ladder IBNR + deterioration.

Turns loss triangles (cumulative paid/incurred by accident year × development
period, from naic_schedp / investor_supp) into the actuarial signal the
`reserving` topic deserves: chain-ladder ultimate + IBNR per insurer/LOB, and
how that IBNR moved vs. the prior estimate (adverse development = the warning
sign). Results → reserving_signals (+ silver mirror, gold.reserving_signals).

Free-Edition design: the deterministic volume-weighted chain-ladder is pure
numpy (no pandas). `chainladder-python` (casact/chainladder-python) is the
recommended install for the full toolkit — Mack/bootstrap stderr, tail fitting,
Bornhuetter-Ferguson — and is the documented upgrade; this module's estimates
match its basic `Development` + `Chainladder` for clean triangles.

The `reserve_deterioration_boost` is provided + tested but NOT yet wired into
the leaderboard formula — that's a deliberate one-step activation once real
triangle data flows (the locked scoring formula shouldn't change for a signal
that is 1.0 everywhere until then).
"""
from __future__ import annotations

import logging

import numpy as np

from digest import db
from digest.outcomes import match_insurer

logger = logging.getLogger(__name__)

RESERVE_BOOST_CAP = 1.3      # max multiplier from adverse reserve development


def build_matrix(rows: list) -> tuple[list[int], np.ndarray]:
    """Triangle cells → (accident_years, matrix[ay × dev]) with NaN for unfilled
    cells. `rows` each expose accident_year / dev_period / cumulative_value."""
    ays = sorted({r["accident_year"] for r in rows})
    devs = sorted({r["dev_period"] for r in rows})
    ay_ix = {a: i for i, a in enumerate(ays)}
    dev_ix = {d: j for j, d in enumerate(devs)}
    mat = np.full((len(ays), len(devs)), np.nan)
    for r in rows:
        mat[ay_ix[r["accident_year"]], dev_ix[r["dev_period"]]] = r["cumulative_value"]
    return ays, mat


def chain_ladder(mat: np.ndarray) -> dict | None:
    """Volume-weighted chain-ladder over a cumulative triangle matrix
    (rows=accident years, cols=development periods ascending; NaN = unobserved).

    Returns {dev_factors, cdf, ultimate_total, latest_total, ibnr} or None if
    the triangle is too sparse to develop.
    """
    if mat.ndim != 2 or mat.shape[1] < 2:
        return None
    n_dev = mat.shape[1]

    # Age-to-age factors f_j (dev j → j+1), volume-weighted over rows with both.
    factors = np.ones(n_dev - 1)
    for j in range(n_dev - 1):
        col, nxt = mat[:, j], mat[:, j + 1]
        mask = ~np.isnan(col) & ~np.isnan(nxt)
        denom = col[mask].sum()
        factors[j] = (nxt[mask].sum() / denom) if denom > 0 else 1.0

    # CDF from each dev to ultimate (last column has cdf 1).
    cdf = np.ones(n_dev)
    for j in range(n_dev - 1):
        cdf[j] = float(np.prod(factors[j:]))

    # Per accident year: develop the latest observed cell to ultimate.
    latest_total = ultimate_total = 0.0
    for i in range(mat.shape[0]):
        observed = np.where(~np.isnan(mat[i]))[0]
        if observed.size == 0:
            continue
        last_j = int(observed[-1])
        latest = float(mat[i, last_j])
        latest_total += latest
        ultimate_total += latest * cdf[last_j]

    return {
        "dev_factors": factors.tolist(),
        "cdf": cdf.tolist(),
        "latest_total": round(latest_total, 2),
        "ultimate_total": round(ultimate_total, 2),
        "ibnr": round(ultimate_total - latest_total, 2),
    }


def reserve_signal(insurer: str, lob: str, metric: str, as_of: str,
                   cl: dict, prior_ibnr: float | None) -> dict:
    """Assemble a reserving_signals row, classifying development vs. the prior IBNR."""
    ibnr = cl["ibnr"]
    deterioration = direction = None
    if prior_ibnr is not None and prior_ibnr != 0:
        deterioration = round((ibnr - prior_ibnr) / abs(prior_ibnr), 4)
        direction = ("adverse" if ibnr > prior_ibnr
                     else "favorable" if ibnr < prior_ibnr else "flat")
    return {
        "insurer": insurer, "lob": lob, "metric": metric, "as_of": as_of,
        "ultimate": cl["ultimate_total"], "latest": cl["latest_total"],
        "ibnr": ibnr, "prior_ibnr": prior_ibnr,
        "deterioration_pct": deterioration, "direction": direction,
    }


def run_reserving() -> dict[str, int]:
    """Compute chain-ladder estimates for every stored triangle. Returns counts."""
    keys = db.triangle_keys()
    computed = 0
    for k in keys:
        rows = db.load_triangle(k["insurer"], k["lob"], k["metric"], k["as_of"])
        if not rows:
            continue
        _, mat = build_matrix(rows)
        cl = chain_ladder(mat)
        if cl is None:
            continue
        prior = db.prior_reserving_ibnr(k["insurer"], k["lob"], k["metric"], k["as_of"])
        db.upsert_reserving_signal(
            reserve_signal(k["insurer"], k["lob"], k["metric"], k["as_of"], cl, prior)
        )
        computed += 1
    logger.info("reserving: computed %d estimates", computed)
    return {"triangles": len(keys), "computed": computed}


def reserve_deterioration_boost(text: str, severity_map: dict[str, float],
                                cap: float = RESERVE_BOOST_CAP) -> float:
    """Boost for an item naming an insurer with adverse reserve development.

    severity = the insurer's adverse IBNR deterioration fraction (e.g. 0.15 →
    +15%). boost = min(1 + severity, cap); 1.0 when no insurer match or no
    adverse signal. PURE — not yet wired into the leaderboard formula (activate
    by threading `severity_map` into signals.score_item, mirroring tplf_boost).
    """
    if not text or not severity_map:
        return 1.0
    ticker = match_insurer(text)
    if not ticker:
        return 1.0
    severity = severity_map.get(ticker, 0.0)
    return min(1.0 + max(severity, 0.0), cap) if severity > 0 else 1.0
