"""Wave 2 — PC two-axis regime detector.

Computes the current insurance-market regime as the product of two axes:

    market_cycle  ∈ {hard_market, transitioning_to_hard, stable,
                     transitioning_to_soft, soft_market}     (LLM-judged)
    cat_load      ∈ {low_season, active_season, post_major_event}  (mechanical)

The combined multiplier (`market_cycle_mult × cat_load_mult`) becomes the
`regime_mult` factor in the signal leaderboard formula and reshapes topic
ordering in the daily/weekly notes.

Cadence: 3 days. Triggered from the AM job when `latest_regime_signal().as_of`
is > 72h old. Hysteresis: a regime transition requires two consecutive
recomputes to agree before becoming the active state — so the minimum
real shift is 6 days, which keeps a single noisy reading from flipping
the multiplier.

Manual override: `config/regime_override.yaml`, if present, supersedes the
detector. Useful for known events (a confirmed Cat-4 landfall, an admitted
hard-market cycle turn from a major reinsurer's earnings call) without
waiting for the LLM to catch up.
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
    source: str = "detector"   # 'detector' | 'override' | 'hysteresis_pending'

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


def compute_cat_load(counts: dict[str, int] | None = None) -> tuple[str, dict[str, int]]:
    """Map NHC/USGS/NIFC counts → cat_load state. Returns (state, raw_counts).

    Rules (deliberately simple — easy to tune from the user's read of the daily):
      post_major_event:   ≥ 1 major EQ (M ≥ 6 US) OR ≥ 1 active wildfire OR ≥ 3 NHC advisories in window
      active_season:      ≥ 1 NHC advisory in last 14d
      low_season:         otherwise
    """
    if counts is None:
        counts = db.cat_load_counts()
    active_nhc      = counts.get("active_nhc", 0)
    recent_major_eq = counts.get("recent_major_eq", 0)
    recent_wildfire = counts.get("recent_wildfire", 0)

    if recent_major_eq >= 1 or recent_wildfire >= 1 or active_nhc >= 3:
        return "post_major_event", counts
    if active_nhc >= 1:
        return "active_season", counts
    return "low_season", counts


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

    Falls back to {"market_cycle": "stable", ...} if the LLM call fails or
    the window has too few items.
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
    }


# ── Override + hysteresis ─────────────────────────────────────────────


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


def _apply_hysteresis(
    proposed_market_cycle: str,
    proposed_cat_load: str,
) -> tuple[str, str, bool]:
    """Compare proposed regime to the last 2 stored signals.

    Returns (effective_market_cycle, effective_cat_load, transition_confirmed).
    A regime change holds only when the new state matches the most recent
    stored signal — i.e., two consecutive recomputes have agreed.
    """
    history = db.recent_regime_signals(n=2)
    if not history:
        return proposed_market_cycle, proposed_cat_load, True

    last = history[0]
    # If proposal already matches the last stored, no transition needed.
    if last["market_cycle"] == proposed_market_cycle and last["cat_load"] == proposed_cat_load:
        return proposed_market_cycle, proposed_cat_load, True

    # If we only have one historical reading, this proposal is the first
    # "disagreement" — defer transition (treat as pending).
    if len(history) < 2:
        return last["market_cycle"], last["cat_load"], False

    # We have two prior readings. If both prior match each other AND the
    # current proposal also matches them, transition is confirmed; else
    # the proposal differs from the established baseline and is pending.
    prev = history[1]
    if prev["market_cycle"] == last["market_cycle"] and prev["cat_load"] == last["cat_load"]:
        # Stable baseline; one disagreeing reading isn't enough.
        return last["market_cycle"], last["cat_load"], False

    # Baseline was already shifting; if the new reading matches the most
    # recent, accept the transition.
    return proposed_market_cycle, proposed_cat_load, True


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

    Args:
        force: skip the 72h staleness check and recompute anyway.
    """
    if not force and not is_stale():
        logger.info("regime: skipped — last signal < 72h old")
        return current_regime()

    proposed_cat_load, cat_counts = compute_cat_load()
    market_judgment                = compute_market_cycle()
    proposed_market_cycle          = market_judgment["market_cycle"]

    override = load_override()
    if override:
        if "market_cycle" in override:
            proposed_market_cycle = override["market_cycle"]
        if "cat_load" in override:
            proposed_cat_load = override["cat_load"]

    if override:
        effective_market, effective_cat = proposed_market_cycle, proposed_cat_load
        source = "override"
    else:
        effective_market, effective_cat, confirmed = _apply_hysteresis(
            proposed_market_cycle, proposed_cat_load,
        )
        source = "detector" if confirmed else "hysteresis_pending"

    mc_mult   = MARKET_CYCLE_MULT[effective_market]
    cat_mult  = CAT_LOAD_MULT[effective_cat]
    mult      = mc_mult * cat_mult
    as_of_iso = datetime.now(timezone.utc).isoformat()

    evidence = {
        "cat_load":           cat_counts,
        "market_judgment":    market_judgment,
        "proposed":           {"market_cycle": proposed_market_cycle, "cat_load": proposed_cat_load},
        "effective":          {"market_cycle": effective_market,      "cat_load": effective_cat},
        "override":           override,
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
        "regime: %s × %s → ×%.2f (source=%s, proposed=%s/%s)",
        effective_market, effective_cat, mult, source,
        proposed_market_cycle, proposed_cat_load,
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
