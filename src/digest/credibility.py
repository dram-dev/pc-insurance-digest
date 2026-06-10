"""Bühlmann–Straub source credibility — experience-rate the trust tiers.

The leaderboard's source multipliers (EDGAR 1.3 … HN 0.6) are hand-set priors.
Once the outcome backtest has labels, each source has an OBSERVED corroboration
rate — but a thin source's raw rate is noise. This module shrinks each source's
rate toward the book-wide mean with classical Bühlmann–Straub credibility:

    r̄    = Σ xₛ / Σ nₛ                       (grand mean)
    EPV  = Σ nₛ rₛ(1−rₛ) / Σ (nₛ−1)          (expected process variance, Bernoulli)
    VHM  = [Σ nₛ(rₛ−r̄)² − (S−1)·EPV] / (N − Σ nₛ²/N)   (variance of hypothetical means)
    k    = EPV / VHM        Zₛ = nₛ / (nₛ + k)
    r̂ₛ   = Zₛ·rₛ + (1−Zₛ)·r̄                 (credibility-weighted rate)

and maps the credibility rate to an IMPLIED multiplier as a dampened relativity
on the hand-set value:  m̂ₛ = mₛ · clamp((r̂ₛ/r̄)^γ),  γ = 0.5, ratio clamped to
[0.75, 1.25] so experience can move a tier by at most ±25%.

REPORT-ONLY by default: the table surfaces in the weekly note ("Source
Credibility") and `digest credibility`; the hand-set multipliers keep driving
scoring. Setting `credibility: {apply: 1}` in Scoring Weights.md lets
run_signals swap in the implied multipliers once the table has earned trust —
the same one-step activation pattern as every other dormant signal here.

Degenerate cases are explicit, not silent: VHM ≤ 0 (between-source spread is
within sampling noise) → Z = 0 everywhere and every implied multiplier equals
the hand-set one; fewer than 2 sources with outcomes → same.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from digest import db

logger = logging.getLogger(__name__)

GAMMA_DEFAULT = 0.5            # dampening exponent on the rate relativity
RATIO_CLAMP = (0.75, 1.25)     # experience moves a hand-set multiplier ≤ ±25%
HORIZON_DEFAULT = 30           # outcome horizon the rates are read at


@dataclass
class SourceCredibility:
    source: str
    n: int
    raw_rate: float
    z: float
    cred_rate: float
    hand_mult: float
    implied_mult: float


def buhlmann_straub(stats: dict[str, tuple[int, int]]) -> dict:
    """Variance components + per-source credibility from {source: (n, positives)}.

    Returns {"r_bar", "epv", "vhm", "k", "z": {source: Z}, "cred": {source: r̂}}.
    k is math.inf (Z=0 everywhere) when the between-source variance estimate is
    non-positive — the honest reading that observed spread is sampling noise.
    """
    stats = {s: (n, x) for s, (n, x) in stats.items() if n > 0}
    if not stats:
        return {"r_bar": 0.0, "epv": 0.0, "vhm": 0.0, "k": math.inf, "z": {}, "cred": {}}

    big_n = sum(n for n, _ in stats.values())
    n_sources = len(stats)
    r = {s: x / n for s, (n, x) in stats.items()}
    r_bar = sum(x for _, x in stats.values()) / big_n

    epv_denom = sum(n - 1 for n, _ in stats.values())
    epv = (
        sum(n * r[s] * (1 - r[s]) for s, (n, _) in stats.items()) / epv_denom
        if epv_denom > 0 else 0.0
    )
    vhm_denom = big_n - sum(n * n for n, _ in stats.values()) / big_n
    vhm = (
        (sum(n * (r[s] - r_bar) ** 2 for s, (n, _) in stats.items())
         - (n_sources - 1) * epv) / vhm_denom
        if (n_sources >= 2 and vhm_denom > 0) else 0.0
    )

    if vhm <= 0:
        k = math.inf
        z = {s: 0.0 for s in stats}
    else:
        k = epv / vhm
        z = {s: n / (n + k) for s, (n, _) in stats.items()}

    cred = {s: z[s] * r[s] + (1 - z[s]) * r_bar for s in stats}
    return {"r_bar": r_bar, "epv": epv, "vhm": vhm, "k": k, "z": z, "cred": cred}


def implied_multiplier(
    hand_mult: float, cred_rate: float, r_bar: float, gamma: float = GAMMA_DEFAULT,
) -> float:
    """Hand-set multiplier × dampened, clamped credibility relativity."""
    if r_bar <= 0 or cred_rate < 0:
        return hand_mult
    ratio = (cred_rate / r_bar) ** gamma
    lo, hi = RATIO_CLAMP
    return hand_mult * max(lo, min(hi, ratio))


def credibility_table(
    horizon_days: int = HORIZON_DEFAULT,
    weights: dict | None = None,
) -> list[SourceCredibility]:
    """The per-source credibility table, descending by n. Empty until the
    outcome backtest has rows (so the weekly section just doesn't render)."""
    from digest.signals import SOURCE_MULT_DEFAULT, _load_scoring_weights

    if weights is None:
        weights = _load_scoring_weights()
    sources_map = weights.get("sources", {})
    gamma = float(weights.get("credibility", {}).get("gamma", GAMMA_DEFAULT))

    rows = db.source_outcome_stats(horizon_days)
    stats = {r["source"]: (int(r["n"]), int(r["positives"])) for r in rows}
    bs = buhlmann_straub(stats)
    out: list[SourceCredibility] = []
    for source, (n, x) in sorted(stats.items(), key=lambda kv: -kv[1][0]):
        hand = float(sources_map.get(source, sources_map.get("default", SOURCE_MULT_DEFAULT)))
        cred = bs["cred"][source]
        out.append(SourceCredibility(
            source=source, n=n,
            raw_rate=round(x / n, 4),
            z=round(bs["z"][source], 4),
            cred_rate=round(cred, 4),
            hand_mult=round(hand, 3),
            implied_mult=round(implied_multiplier(hand, cred, bs["r_bar"], gamma), 3),
        ))
    return out


def adjusted_source_multipliers(weights: dict) -> dict[str, float]:
    """{source: implied multiplier} for run_signals when `credibility.apply` is
    set — a COPY of the hand map with experience-rated values swapped in for
    sources that have outcomes. Empty dict when there's nothing to adjust
    (caller keeps the hand map)."""
    cfg = weights.get("credibility", {})
    horizon = int(cfg.get("horizon_days", HORIZON_DEFAULT))
    table = credibility_table(horizon_days=horizon, weights=weights)
    if not table:
        return {}
    adjusted = dict(weights.get("sources", {}))
    for row in table:
        adjusted[row.source] = row.implied_mult
    return adjusted
