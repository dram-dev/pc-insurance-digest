"""Isotonic calibration of LLM materiality → P(corroborated).

The summarizer's materiality score (0.5–1.5) enters the leaderboard raw, but
LLM-emitted numerics are clumpy and uncalibrated — mass piles up at 0.7/0.8/0.9
and the spacing between values carries no probability meaning. Once the outcome
backtest has enough labels, this module fits a monotone (isotonic) map from
materiality to the observed corroboration rate via pool-adjacent-violators, and
`signals.score_item` swaps the raw clamp for a calibrated RELATIVITY:

    llm_judgment = clamp( P(corroborated | materiality) / base_rate, 0.5, 1.5 )

— the GLM-relativity form: an average item lands exactly at 1.0, an item twice
as likely to corroborate caps at 1.5. Monotonicity is enforced by construction,
so the LLM's ordering is preserved; only the spacing is re-learned.

Behavior-preserving by default: until `train_materiality_calibrator` has fitted
and persisted a curve (gated on ≥100 labeled items with ≥10 in each class —
the learn.py small-n discipline), `latest_materiality_calibrator()` returns
None and the raw clamp stays in force.

Free-Edition design: pure numpy PAVA (no sklearn), curve persisted as a JSON
step function in the `calibrators` table. sklearn's IsotonicRegression is the
documented upgrade path; for a 1-D curve the PAVA fit is exact, not approximate.
"""
from __future__ import annotations

import bisect
import json
import logging

import numpy as np

from digest import db

logger = logging.getLogger(__name__)

MIN_LABELED = 100   # labeled items with a materiality score before fitting
MIN_CLASS = 10      # each class (corroborated / not) must have at least this many
# Unlike the advisory learned model, a fitted calibrator goes straight into
# live scoring (llm_judgment), so — like the log-linear gate — it must not be
# unlocked by historical-backfill labels alone: their EDGAR-heavy mix has a
# different corroboration base rate than the live feed the calibrator will be
# applied to. Backfill labels still enlarge the fit once live labels unlock it.
MIN_LIVE_LABELED = 50
JUDGMENT_MIN, JUDGMENT_MAX = 0.5, 1.5   # the llm_judgment clamp signals.py uses


def pava(x, y, w=None) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators: least-squares monotone NON-DECREASING step fit
    of y on x. Ties in x are pre-pooled. Returns (block_x, block_y) — each
    block covers [block_x[i], block_x[i+1]) and predicts block_y[i].
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    w = np.ones_like(y) if w is None else np.asarray(w, dtype=float)
    order = np.argsort(x, kind="mergesort")
    x, y, w = x[order], y[order], w[order]

    # Pre-pool exact ties so the step function is single-valued per x.
    ux, inv = np.unique(x, return_inverse=True)
    uw = np.zeros(len(ux))
    uy = np.zeros(len(ux))
    np.add.at(uw, inv, w)
    np.add.at(uy, inv, w * y)
    uy /= uw

    # Each block: [start_x, mean, weight]; merge while monotonicity is violated.
    blocks: list[list[float]] = []
    for xi, yi, wi in zip(ux, uy, uw):
        blocks.append([float(xi), float(yi), float(wi)])
        while len(blocks) > 1 and blocks[-2][1] > blocks[-1][1]:
            x0, y0, w0 = blocks[-2]
            x1, y1, w1 = blocks[-1]
            blocks[-2:] = [[x0, (y0 * w0 + y1 * w1) / (w0 + w1), w0 + w1]]
    return [b[0] for b in blocks], [b[1] for b in blocks]


class IsotonicCalibrator:
    """A fitted monotone step map materiality → P(corroborated), plus the cohort
    base rate the judgment relativity divides by."""

    def __init__(self, block_x: list[float], block_y: list[float], base_rate: float):
        self.block_x = list(block_x)
        self.block_y = list(block_y)
        self.base_rate = float(base_rate)

    def predict(self, x: float) -> float:
        """Calibrated P(corroborated) at materiality `x` (clamped to the curve's
        ends — extrapolation beyond observed materiality is flat, not linear)."""
        i = bisect.bisect_right(self.block_x, float(x)) - 1
        return self.block_y[max(0, i)]

    def judgment(self, materiality) -> float:
        """The llm_judgment factor: calibrated relativity P/base_rate, clamped to
        the same [0.5, 1.5] band as the raw path. None/garbage materiality → 1.0
        (neutral), matching the uncalibrated behavior."""
        try:
            m = float(materiality)
        except (TypeError, ValueError):
            return 1.0
        if self.base_rate <= 0:
            return 1.0
        rel = self.predict(m) / self.base_rate
        return max(JUDGMENT_MIN, min(JUDGMENT_MAX, rel))

    def to_json(self) -> str:
        return json.dumps({
            "block_x": self.block_x, "block_y": self.block_y,
            "base_rate": self.base_rate,
        })

    @classmethod
    def from_json(cls, s: str) -> "IsotonicCalibrator":
        d = json.loads(s)
        return cls(d["block_x"], d["block_y"], d["base_rate"])


def train_materiality_calibrator(horizon_days: int = 30) -> dict:
    """Fit + persist the materiality calibrator from the labeled backtest set.
    Returns a summary dict; calibrator_id is None (with a note) under the
    small-n gates — same discipline as learn.train."""
    from digest.learn import is_backfill_row

    rows = db.learning_dataset(horizon_days)
    pairs = [
        (float(r["materiality_score"]), int(r["corroborated"]))
        for r in rows if r["materiality_score"] is not None
    ]
    n = len(pairs)
    n_pos = sum(y for _, y in pairs)
    n_neg = n - n_pos
    if n < MIN_LABELED or n_pos < MIN_CLASS or n_neg < MIN_CLASS:
        return {"calibrator_id": None, "n_samples": n,
                "note": (f"need ≥{MIN_LABELED} labeled items with ≥{MIN_CLASS} per class "
                         f"(have {n}: {n_pos}+/{n_neg}−) — raw materiality clamp stays")}
    n_live = sum(
        1 for r in rows
        if r["materiality_score"] is not None and not is_backfill_row(r)
    )
    if n_live < MIN_LIVE_LABELED:
        return {"calibrator_id": None, "n_samples": n, "n_live": n_live,
                "note": (f"live-mix gate: need ≥{MIN_LIVE_LABELED} live (non-backfill) "
                         f"labels (have {n_live}) — raw materiality clamp stays")}

    xs = [m for m, _ in pairs]
    ys = [y for _, y in pairs]
    block_x, block_y = pava(xs, ys)
    base_rate = float(np.mean(ys))
    cal = IsotonicCalibrator(block_x, block_y, base_rate)
    cal_id = db.save_calibrator({
        "name": "materiality", "horizon_days": horizon_days, "n_samples": n,
        "base_rate": base_rate, "curve_json": cal.to_json(),
    })
    logger.info(
        "calibration: materiality curve fitted (id=%d, n=%d, base_rate=%.3f, %d blocks)",
        cal_id, n, base_rate, len(block_x),
    )
    return {"calibrator_id": cal_id, "n_samples": n, "n_live": n_live,
            "base_rate": round(base_rate, 4), "blocks": len(block_x)}


def latest_materiality_calibrator() -> IsotonicCalibrator | None:
    """The most recently fitted calibrator, or None (raw clamp stays in force)."""
    row = db.latest_calibrator("materiality")
    return IsotonicCalibrator.from_json(row["curve_json"]) if row else None
