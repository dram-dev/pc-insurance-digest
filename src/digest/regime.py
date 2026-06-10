"""Wave 2 — PC two-axis regime detector (PR4: Markov-switching market cycle).

Computes the current insurance-market regime as the product of two axes:

    market_cycle  ∈ {hard_market, transitioning_to_hard, stable,
                     transitioning_to_soft, soft_market}     (latent, filtered)
    cat_load      ∈ {low_season, active_season, post_major_event}  (mechanical)

The combined multiplier (`market_cycle_mult × cat_load_mult`) becomes the
`regime_mult` factor in the signal leaderboard formula and reshapes topic
ordering in the daily/weekly notes.

Cadence: 3 days. Triggered from the AM job when `latest_regime_signal().as_of`
is > 72h old.

MARKET CYCLE IS A HIDDEN STATE (PR4). The underwriting cycle is a textbook
regime-switching process, so the detector runs a discrete forward filter over
the five ordered states:

  * a STICKY transition prior (self-transition ≈ 0.90, adjacent ≈ 0.045) makes
    persistence structural — the old two-consecutive-recomputes hysteresis rule
    falls out as a property of the posterior instead of an if-statement;
  * the LLM classification is a NOISY EMISSION (confusion kernel: 0.70 on the
    diagonal, 0.125 one state off), never the state itself — one contrarian
    reading shifts the posterior, it cannot flip the mode;
  * the priced reinsurance hint (Lead 1), when present, is a second emission;
  * `market_cycle_mult` is the POSTERIOR-EXPECTED multiplier Σ πₛ·multₛ —
    continuous, so the leaderboard sees a smooth glide between regimes rather
    than a ±10-point cliff. The reported state is the posterior mode; the full
    posterior is persisted in evidence_json.

Manual override: `config/regime_override.yaml`, if present, supersedes the
detector (posterior collapses to the override state). Useful for known events
(a confirmed Cat-4 landfall, an admitted hard-market cycle turn from a major
reinsurer's earnings call) without waiting for the filter to catch up.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

# ── Multiplier tables (locked in CLAUDE.md) ───────────────────────────

MARKET_CYCLE_MULT: dict[str, float] = {
    "hard_market":           1.20,
    "transitioning_to_hard": 1.10,
    "stable":                1.00,
    "transitioning_to_soft": 0.95,
    "soft_market":           0.85,
}

CAT_LOAD_MULT: dict[str, float] = {
    "low_season":       1.00,
    "active_season":    1.10,
    "post_major_event": 1.20,
}

MARKET_CYCLES = tuple(MARKET_CYCLE_MULT.keys())
CAT_LOADS     = tuple(CAT_LOAD_MULT.keys())

DEFAULT_MARKET_CYCLE = "stable"
DEFAULT_CAT_LOAD     = "low_season"
STALE_HOURS          = 72   # recompute if last signal is older than this

OVERRIDE_PATH = Path(__file__).resolve().parents[2] / "config" / "regime_override.yaml"


# ── Markov-switching market cycle (PR4) ───────────────────────────────
#
# Both kernels are indexed by |i−j| over the ordered state axis (hardest →
# softest) and row-normalized at the edges, so an end state's lost mass goes
# back to itself proportionally rather than leaking.

# Sticky transition prior: the cycle turns over quarters, not 72h recomputes.
_TRANSITION_KERNEL = (0.90, 0.045, 0.005, 0.002, 0.001)
# LLM-as-noisy-sensor confusion kernel: right ~70% of the time, one state off
# ~12.5% per side. Also used for the priced reinsurance hint emission.
_EMISSION_KERNEL = (0.70, 0.125, 0.025, 0.008, 0.004)


def _kernel_matrix(kernel: tuple[float, ...]) -> list[list[float]]:
    """n×n row-normalized matrix from a |i−j| kernel over MARKET_CYCLES."""
    n = len(MARKET_CYCLES)
    rows: list[list[float]] = []
    for i in range(n):
        row = [kernel[abs(i - j)] for j in range(n)]
        total = sum(row)
        rows.append([v / total for v in row])
    return rows


TRANSITION = _kernel_matrix(_TRANSITION_KERNEL)
EMISSION   = _kernel_matrix(_EMISSION_KERNEL)
_STATE_IX  = {s: i for i, s in enumerate(MARKET_CYCLES)}


def market_cycle_filter(
    prior: list[float], observations: list[str],
) -> list[float]:
    """One forward-filter step: predict through the sticky transition prior,
    then update on each observation through the emission kernel.

    `observations` are state labels (the LLM call, the priced hint) — an empty
    list is a pure predict step, so with no evidence the posterior just
    diffuses slowly toward the prior's neighbors. Degenerate posteriors
    (zero mass) fall back to uniform rather than NaN.
    """
    n = len(MARKET_CYCLES)
    pi = [sum(prior[i] * TRANSITION[i][j] for i in range(n)) for j in range(n)]
    for obs in observations:
        k = _STATE_IX.get(obs)
        if k is None:
            continue
        pi = [pi[s] * EMISSION[s][k] for s in range(n)]
        total = sum(pi)
        pi = [p / total for p in pi] if total > 0 else [1.0 / n] * n
    total = sum(pi)
    return [p / total for p in pi] if total > 0 else [1.0 / n] * n


def _prior_posterior(row: Any) -> list[float]:
    """The previous recompute's posterior, from its evidence_json. Falls back
    to one-hot on the previously reported state (pre-PR4 rows), then to one-hot
    `stable` when no history exists."""
    n = len(MARKET_CYCLES)
    if row is not None:
        raw = row["evidence_json"] if "evidence_json" in row.keys() else None
        if raw:
            try:
                stored = (json.loads(raw) or {}).get("posterior") or {}
            except (TypeError, json.JSONDecodeError):
                stored = {}
            pi = [float(stored.get(s, 0.0)) for s in MARKET_CYCLES]
            if sum(pi) > 0:
                total = sum(pi)
                return [p / total for p in pi]
        last_state = row["market_cycle"]
        if last_state in _STATE_IX:
            return [1.0 if s == last_state else 0.0 for s in MARKET_CYCLES]
    return [1.0 if s == DEFAULT_MARKET_CYCLE else 0.0 for s in MARKET_CYCLES]


def posterior_multiplier(posterior: list[float]) -> float:
    """Posterior-expected market-cycle multiplier Σ πₛ·multₛ (continuous)."""
    return sum(p * MARKET_CYCLE_MULT[s] for p, s in zip(posterior, MARKET_CYCLES))


# ── Dataclass ─────────────────────────────────────────────────────────


@dataclass
class RegimeSignal:
    as_of: str
    market_cycle: str
    cat_load: str
    market_cycle_mult: float
    cat_load_mult: float
    multiplier: float
    evidence: dict[str, Any] = field(default_factory=dict)
    source: str = "detector"   # 'detector' | 'override' ('hysteresis_pending' pre-PR4)

    @classmethod
    def from_row(cls, row: Any) -> "RegimeSignal":
        evidence: dict[str, Any] = {}
        raw = row["evidence_json"] if row is not None else None
        if raw:
            try:
                evidence = json.loads(raw) or {}
            except (TypeError, json.JSONDecodeError):
                evidence = {}
        return cls(
            as_of=row["as_of"],
            market_cycle=row["market_cycle"],
            cat_load=row["cat_load"],
            market_cycle_mult=float(row["market_cycle_mult"]),
            cat_load_mult=float(row["cat_load_mult"]),
            multiplier=float(row["multiplier"]),
            evidence=evidence,
            source=row["source"],
        )

    def summary_line(self) -> str:
        return (
            f"{self.market_cycle} × {self.cat_load} = ×{self.multiplier:.2f} "
            f"(market {self.market_cycle_mult:.2f}, cat {self.cat_load_mult:.2f})"
        )


# ── CAT-load (mechanical) ─────────────────────────────────────────────


def compute_cat_load(
    counts: dict[str, int] | None = None,
    nowcast: dict[str, float] | None = None,
) -> tuple[str, dict[str, int]]:
    """Map NHC/USGS/NIFC counts → cat_load state. Returns (state, raw_counts).

    Rules (deliberately simple — easy to tune from the user's read of the daily):
      post_major_event:   ≥ 1 major EQ (M ≥ 6 US) OR ≥ 1 active wildfire OR ≥ 3 NHC advisories in window
      active_season:      ≥ 1 NHC advisory in last 14d
      low_season:         otherwise

    Lead 2 (CAT-Load Nowcast): an anomalous surge in federal disaster
    declarations can *escalate* the state (never lower it), catching perils the
    three item sources miss (riverine flood, severe convective, ice). The nudge
    is a no-op until `digest cat-nowcast` has run, so the axis is behavior-
    preserving by default.
    """
    if counts is None:
        counts = db.cat_load_counts()
    active_nhc      = counts.get("active_nhc", 0)
    recent_major_eq = counts.get("recent_major_eq", 0)
    recent_wildfire = counts.get("recent_wildfire", 0)

    if recent_major_eq >= 1 or recent_wildfire >= 1 or active_nhc >= 3:
        state = "post_major_event"
    elif active_nhc >= 1:
        state = "active_season"
    else:
        state = "low_season"

    if nowcast is None:
        from digest.cat_nowcast import nowcast_signal
        nowcast = nowcast_signal()
    from digest.cat_nowcast import escalate_cat_load
    escalated = escalate_cat_load(state, nowcast)
    out_counts = {**counts}
    if "declaration_z" in nowcast:
        out_counts["declaration_z"] = nowcast["declaration_z"]
    return escalated, out_counts


# ── Market-cycle (LLM-judged) ─────────────────────────────────────────


MARKET_CYCLE_SYSTEM_PROMPT = """You are a P&C insurance market-cycle classifier.

Given a window of recent underwriting-results and reinsurance-cycle items
from a US P&C digest, classify the current market-cycle position into
exactly one of these five buckets:

  hard_market           — broad rate increases, capacity constrained, combined
                          ratios trending below 95, reinsurer pushback strong
  transitioning_to_hard — early signs of firming: select-line increases,
                          slight capacity withdrawal, deteriorating loss costs
  stable                — neither side dominates; flat-to-modest rate changes
  transitioning_to_soft — early signs of softening: select-line decreases,
                          new capacity, improving loss costs
  soft_market           — broad rate decreases, abundant capacity, combined
                          ratios trending above 100, reinsurers chasing risk

Output strict JSON, no prose, no fences:
{
  "market_cycle":       "<one of the five buckets>",
  "combined_ratio_dir": "improving" | "stable" | "deteriorating",
  "capacity_tone":      "abundant" | "balanced" | "constrained",
  "evidence":           "<≤ 60-word citation of which items drove the call>"
}

If the window contains fewer than 5 substantive items, default to "stable"
and set evidence to "insufficient evidence". Do not invent confidence you
don't have."""


def _build_market_cycle_user_prompt(rows: list[Any]) -> str:
    lines = [f"Trailing window: {len(rows)} items.", ""]
    for row in rows:
        published = (row["published_at"] or row["ingested_at"] or "")[:10]
        title     = (row["title"] or "")[:120]
        summary   = (row["summary"] or "")[:300]
        why       = (row["why_it_matters"] or "")[:200]
        topic     = row["topic"] or ""
        lines.append(f"[{published}] [{topic}] {title}")
        if summary:
            lines.append(f"  {summary}")
        if why:
            lines.append(f"  Why: {why}")
        lines.append("")
    lines.append("JSON only:")
    return "\n".join(lines)


def compute_market_cycle(window_days: int = 60) -> dict[str, Any]:
    """Run Qwen3.5 on the trailing window. Returns dict with cycle + evidence.

    Falls back to {"market_cycle": "stable", ...} if the LLM call fails or the
    window has too few items — with `observed: False`, so the filter treats the
    fallback as NO observation (a pure predict step) rather than a real
    "stable" reading pulling the posterior toward the middle.
    """
    rows = db.items_for_market_cycle(window_days=window_days)
    if len(rows) < 5:
        logger.info("regime: market_cycle defaulting to stable (only %d items in window)", len(rows))
        return {
            "market_cycle":       "stable",
            "combined_ratio_dir": "stable",
            "capacity_tone":      "balanced",
            "evidence":           f"insufficient evidence ({len(rows)} items)",
            "n_items":            len(rows),
            "observed":           False,
        }

    # Lazy import to keep regime usable in test/dev environments without MLX.
    from digest.summarize import BACKENDS, BackendError, _backend_config
    from digest_core.summarize.runner import extract_json

    backend_fn = BACKENDS.get(settings.summarizer_backend)
    if backend_fn is None:
        logger.warning(
            "regime: unknown SUMMARIZER_BACKEND %r — defaulting market_cycle=stable",
            settings.summarizer_backend,
        )
        return {
            "market_cycle":       "stable",
            "combined_ratio_dir": "stable",
            "capacity_tone":      "balanced",
            "evidence":           "no summarizer backend configured",
            "n_items":            len(rows),
            "observed":           False,
        }

    user_prompt = _build_market_cycle_user_prompt(rows)
    try:
        raw = backend_fn(MARKET_CYCLE_SYSTEM_PROMPT, user_prompt, _backend_config())
    except BackendError as exc:
        logger.warning("regime: backend failed (%s); defaulting market_cycle=stable", exc)
        return {
            "market_cycle":       "stable",
            "combined_ratio_dir": "stable",
            "capacity_tone":      "balanced",
            "evidence":           f"backend error: {exc!s}"[:200],
            "n_items":            len(rows),
            "observed":           False,
        }

    parsed = extract_json(raw) or {}
    cycle = str(parsed.get("market_cycle", "stable")).lower().strip()
    if cycle not in MARKET_CYCLE_MULT:
        cycle = "stable"
    return {
        "market_cycle":       cycle,
        "combined_ratio_dir": str(parsed.get("combined_ratio_dir", "stable")).lower().strip(),
        "capacity_tone":      str(parsed.get("capacity_tone", "balanced")).lower().strip(),
        "evidence":           str(parsed.get("evidence", ""))[:400],
        "n_items":            len(rows),
        "observed":           True,
    }


# ── Override ──────────────────────────────────────────────────────────


def load_override() -> dict[str, str] | None:
    """Read `config/regime_override.yaml` if present.

    Expected shape:
        market_cycle: hard_market   # optional
        cat_load:     post_major_event  # optional

    Returns a normalized dict or None. Missing keys leave detector value in place.
    """
    if not OVERRIDE_PATH.exists():
        return None
    try:
        data = yaml.safe_load(OVERRIDE_PATH.read_text()) or {}
    except yaml.YAMLError as exc:
        logger.warning("regime: override yaml parse failed: %s", exc)
        return None
    out: dict[str, str] = {}
    mc = str(data.get("market_cycle", "")).lower().strip()
    cl = str(data.get("cat_load", "")).lower().strip()
    if mc in MARKET_CYCLE_MULT:
        out["market_cycle"] = mc
    if cl in CAT_LOAD_MULT:
        out["cat_load"] = cl
    return out or None


# ── Public API ────────────────────────────────────────────────────────


def is_stale(hours: int = STALE_HOURS) -> bool:
    """True if no regime_signals row exists or the latest is older than `hours`."""
    row = db.latest_regime_signal()
    if row is None:
        return True
    try:
        as_of = datetime.fromisoformat(row["as_of"])
    except ValueError:
        return True
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - as_of).total_seconds() / 3600.0
    return age_hours > hours


def current_regime() -> RegimeSignal:
    """Return the active RegimeSignal, falling back to neutral if none stored."""
    row = db.latest_regime_signal()
    if row is None:
        return RegimeSignal(
            as_of=datetime.now(timezone.utc).isoformat(),
            market_cycle=DEFAULT_MARKET_CYCLE,
            cat_load=DEFAULT_CAT_LOAD,
            market_cycle_mult=MARKET_CYCLE_MULT[DEFAULT_MARKET_CYCLE],
            cat_load_mult=CAT_LOAD_MULT[DEFAULT_CAT_LOAD],
            multiplier=MARKET_CYCLE_MULT[DEFAULT_MARKET_CYCLE] * CAT_LOAD_MULT[DEFAULT_CAT_LOAD],
            evidence={"note": "no regime computed yet"},
            source="default",
        )
    return RegimeSignal.from_row(row)


def compute_regime(force: bool = False) -> RegimeSignal:
    """Compute (or short-circuit) the current regime and persist it.

    Market cycle: one forward-filter step — predict through the sticky
    transition prior, update on the LLM emission (when it actually observed)
    and the priced reinsurance hint (when present). The reported state is the
    posterior mode; `market_cycle_mult` is the posterior-expected multiplier
    (continuous). Cat load: mechanical thresholds + nowcast escalation, as-is.

    Args:
        force: skip the 72h staleness check and recompute anyway.
    """
    if not force and not is_stale():
        logger.info("regime: skipped — last signal < 72h old")
        return current_regime()

    effective_cat, cat_counts = compute_cat_load()
    market_judgment           = compute_market_cycle()

    observations: list[str] = []
    if market_judgment.get("observed"):
        observations.append(market_judgment["market_cycle"])
    from digest.reinsurance import market_cycle_hint
    hint = market_cycle_hint()                 # Lead 1 — None until pricing data
    if hint in _STATE_IX:
        observations.append(hint)

    override = load_override()
    if override and "cat_load" in override:
        effective_cat = override["cat_load"]
    if override and "market_cycle" in override:
        # Override collapses the posterior — the next detector run starts there.
        posterior = [1.0 if s == override["market_cycle"] else 0.0
                     for s in MARKET_CYCLES]
    else:
        prior = _prior_posterior(db.latest_regime_signal())
        posterior = market_cycle_filter(prior, observations)

    source = "override" if override else "detector"
    effective_market = MARKET_CYCLES[max(range(len(posterior)), key=posterior.__getitem__)]
    mc_mult   = round(posterior_multiplier(posterior), 4)
    cat_mult  = CAT_LOAD_MULT[effective_cat]
    mult      = round(mc_mult * cat_mult, 4)
    as_of_iso = datetime.now(timezone.utc).isoformat()

    evidence = {
        "cat_load":        cat_counts,
        "market_judgment": market_judgment,
        "observations":    observations,
        "posterior":       {s: round(p, 4) for s, p in zip(MARKET_CYCLES, posterior)},
        "effective":       {"market_cycle": effective_market, "cat_load": effective_cat},
        "override":        override,
    }
    db.upsert_regime_signal(
        as_of=as_of_iso,
        market_cycle=effective_market,
        cat_load=effective_cat,
        market_cycle_mult=mc_mult,
        cat_load_mult=cat_mult,
        multiplier=mult,
        evidence_json=json.dumps(evidence),
        source=source,
    )
    logger.info(
        "regime: %s × %s → ×%.3f (source=%s, obs=%s, posterior mode p=%.2f)",
        effective_market, effective_cat, mult, source,
        observations or "none", max(posterior),
    )
    return RegimeSignal(
        as_of=as_of_iso,
        market_cycle=effective_market,
        cat_load=effective_cat,
        market_cycle_mult=mc_mult,
        cat_load_mult=cat_mult,
        multiplier=mult,
        evidence=evidence,
        source=source,
    )
