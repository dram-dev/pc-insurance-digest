"""Wave 2 — Signal leaderboard.

Scores every kept+summarized item with a per-item leaderboard formula:

    score = source_mult × regime_mult × topic_relevance × recency
          × llm_judgment × topic_priority_boost × burden_intensity_boost
          × insurer_priority_boost × inflation_keyword_boost
          × regulatory_action_boost

The output drives:
  * Top-5 callout in the daily note
  * Top-15 + per-source quality table in the weekly note
  * Eventual `_meta/Leaderboard.md` rolling 30d view

Components, in plain English:
  source_mult            Trust the channel (EDGAR > AM Best > trade press > Reddit > HN).
  regime_mult            Current market-cycle × cat-load multiplier from the regime detector.
  topic_relevance        Reserved — currently 1.0 for every topic. Tune later if topic
                         emphasis under specific regimes needs sharpening.
  recency                Exponential decay 2^(−age/h), per-topic half-life h (cat_event 2d,
                         regulatory_rate 14d, reserving 21d, default 7d), floor 0.1.
  llm_judgment           Materiality from summarize.py (0.5–1.5), default 1.0 if missing.
                         Once the isotonic calibrator is fitted (calibration.py), this
                         becomes the calibrated relativity P(corroborated)/base_rate.
  topic_priority_boost   Personal-lines auto + liability topics (social inflation,
                         commercial specialty, reserving, supply chain) > 1.0.
  burden_intensity_boost Regulatory Sonar lite — burden_intensity classification on
                         regulatory_rate items only.
  insurer_priority_boost Carrier weighting: max of an EDGAR ticker boost (PGR/ALL/BRK =
                         1.5, TRV = 1.3, …) and a carrier-NAME boost scanned over the
                         text (State Farm / Allstate = 1.5) so the largest personal-auto
                         carriers — including mutuals with no SEC filings, like State
                         Farm — outrank generic press on any source, not just EDGAR.
  inflation_keyword_boost  Title/summary keyword scan for the user's tracked inflation
                           drivers (auto parts, construction cost, labor cost/supply,
                           verdict/judgement, severity, loss cost). 1.2× on hit, else 1.0.

Persistence:
  * Each `digest signals` run inserts one row per item into `signal_scores`
    keyed by (item_id, computed_at). Older rows are retained so we can see
    drift over time.
  * `digest signals --display` reads the latest row per item.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from digest import db
from digest.config import settings
from digest.regime import current_regime, RegimeSignal

logger = logging.getLogger(__name__)


# ── Source multipliers (defaults; user can override via _meta/Scoring Weights.md) ────

SOURCE_MULT_DEFAULT = 0.7

SOURCE_MULT: dict[str, float] = {
    # Tier 1.3 — primary disclosures + government hazard advisories
    "edgar":   1.3,
    "nhc":     1.3,
    "clipped": 1.3,   # user self-curated, bypasses scoring tiers
    # Tier 1.2 — structured / regulatory primaries
    "usgs":             1.2,
    "fred":             1.2,
    "courtlistener":    1.2,   # federal docket primary source (MDL filings)
    "state_doi":        1.2,
    "serff":            1.2,
    "naic_schedp":      1.2,
    # Tier 1.1 — quarterly carrier disclosures
    "investor_supp":    1.1,
    # Tier 1.0 — trade press, curated reports, structured government datasets
    "rss":              1.0,
    "spc":              1.0,
    "nifc":             1.0,
    "collision":        1.0,
    "industry_research": 1.0,
    # Tier 0.9 — Substack longform
    "substack": 0.9,
    # Tier 0.7 — Reddit
    "reddit":  0.7,
    # Tier 0.6 — HN
    "hn":      0.6,
}


# ── Topic priority boost (defaults; user can override) ────────────────

TOPIC_PRIORITY_BOOST: dict[str, float] = {
    "personal_lines":       1.3,
    # Liability-trend topics — user preference: outrank cat_event by default
    "social_inflation":     1.4,
    "commercial_specialty": 1.4,
    "reserving":            1.4,
    # Inflationary cost-driver feeds (auto parts, construction, labor, medical/Rx)
    "supply_chain":         1.4,
    # Industry-financial-state topics — under-weighted before Score Higher review
    "underwriting_results": 1.2,   # combined ratio, AY commentary, industry profitability
    "distribution":         1.2,   # broker M&A (MMC/AON/WTW/BRO/AJG/RYAN/Patriot)
    "regulatory_rate":      1.2,   # state DOI / SERFF / NAIC actions (stacks with burden_boost)
}


# ── Burden intensity boost (Wave 2 lite — populated when Sonar ships) ──

BURDEN_INTENSITY_BOOST: dict[str, float] = {
    "high":   1.3,
    "medium": 1.1,
    "low":    1.0,
}


# ── Conviction tier (high/medium/low) — adapted from macro-ai-digest ──
#
# macro-ai-digest clamps its score to [0,1] and tiers on fixed 0.72/0.40
# cutoffs. PC's leaderboard score is an unbounded product of ~11 multipliers
# centered on a neutral baseline of ~1.0 (average source, neutral regime, fresh,
# materiality 1.0, no boosts ≈ 1.0). So PC tiers anchor to that baseline: an
# item has to clear it by stacking real signal (strong source + materiality +
# boosts) to read "high". Thresholds are user-tunable via the `signal_tiers`
# section of _meta/Scoring Weights.md — recalibrate once live score
# distributions are visible on the Mac mini.

SIGNAL_TIER_DEFAULTS: dict[str, float] = {"high": 1.6, "medium": 0.9}
TIER_EMOJI: dict[str, str] = {"high": "🔴", "medium": "🟡", "low": "🔵"}
TIER_LABEL: dict[str, str] = {"high": "High", "medium": "Medium", "low": "Low"}


# ── Insurer-priority boost (carrier-level weighting on EDGAR items) ───
#
# User priority: largest personal-auto carriers (PGR, ALL, GEICO/BRK) must
# outrank generic trade-press coverage. Applied only when source=='edgar'
# and metadata.ticker matches. GEICO is a Berkshire subsidiary, so BRK is
# the proxy ticker.

PRIORITY_INSURERS_BOOST: dict[str, float] = {
    "PGR": 1.5,   # personal auto leader
    "ALL": 1.5,   # personal lines #2
    "BRK": 1.5,   # GEICO parent
    "TRV": 1.3,   # commercial + bond / specialty bellwether
    "CB":  1.3,
    "HIG": 1.2,
    "AIG": 1.2,
}


# ── Carrier-NAME priority boost (carrier weighting beyond EDGAR) ──────
#
# The ticker boost above only fires on source=='edgar' items, so it misses
# (a) trade-press / rate-filing coverage of public carriers and (b) mutuals
# entirely. State Farm — the #1 US personal-auto + homeowners writer — is a
# mutual with NO SEC filings, so it could never be weighted by ticker. This
# name map fires when the title/summary names a priority carrier, on ANY
# source, and is combined with the ticker boost as a max (no double-count).
# Keys are matched on word boundaries (case-insensitive); user-overridable
# via the `insurer_names` section of Scoring Weights.md.

PRIORITY_INSURER_NAMES: dict[str, float] = {
    "state farm": 1.5,   # #1 personal auto + home; mutual → never had an EDGAR path
    "allstate":   1.5,   # extends ALL's ticker boost to its non-filing coverage
}


# ── Inflation keyword boost (user-tracked cost drivers) ───────────────
#
# Cross-topic boost for items naming the loss-cost inflation drivers the
# user cares about. Compiled once at module import — scans title +
# summary + why_it_matters. 1.2× on any hit, 1.0× otherwise. Multiple
# hits don't stack (one keyword is enough signal).
#
# COST DRIVERS ONLY. Litigation phrases (nuclear verdict, verdicts/
# settlements, tort reform, social inflation, litigation financing) live in
# _TPLF_KEYWORDS — they used to appear in BOTH lists, so one phrase fired
# the inflation boost (1.2×) AND the TPLF boost (1.3×) on top of the
# social_inflation topic boost (1.4×): a 2.18× stack from a single signal
# counted three times. The two regex families are now disjoint, and the
# keyword stack cap below is the belt-and-suspenders guard.

_INFLATION_KEYWORDS = (
    r"\bauto[\s-]?parts?\b",
    r"\bconstruction (?:cost|labor|material)",
    r"\blabor (?:cost|supply|shortage|inflation|rate)",
    r"\bwage[s]? (?:inflation|growth|pressure)",
    r"\bmedical (?:cost|inflation|trend)",
    r"\bloss cost",
    r"\bseverity (?:trend|inflation|increase)",
    r"\bpure premium",
    r"\bclaim severity",
    r"\bbody shop",
    r"\brepair cost",
    r"\bused[\s-]car",
)

_INFLATION_RE = re.compile("|".join(_INFLATION_KEYWORDS), re.IGNORECASE)


# ── Regulatory / state-action keyword boost ───────────────────────────
#
# Items naming a top-5-state regulatory action, an insurer of last resort,
# or a SERFF rate filing get 1.2× even when the assigned topic isn't
# `regulatory_rate` (e.g., the CA FAIR Plan rate hike is classified
# `personal_lines` per the fire-content routing rule but is structurally
# a regulatory event). Stacks with `burden_intensity_boost` when both
# apply.

_REGULATORY_KEYWORDS = (
    r"\bFAIR Plan\b",                          # CA insurer of last resort
    r"\bCitizens (?:Property|Insurance)\b",    # FL / LA insurer of last resort
    r"\binsurer of last resort\b",
    r"\b(?:rate|premium) (?:filing|hike|increase|approval|reduction)\b",
    r"\b(?:DOI|Department of Insurance) (?:approves|denies|orders|files)",
    r"\bSERFF\b",
    r"\bNAIC (?:adopts|approves|votes|releases|publishes)\b",
    r"\b(?:CDI|California Department of Insurance)\b",
    r"\b(?:FLOIR|Florida Office of Insurance Regulation)\b",
    r"\b(?:TDI|Texas Department of Insurance)\b",
    r"\b(?:LDI|Louisiana Department of Insurance)\b",
    r"\bNYDFS\b",
    r"\btort reform (?:bill|act|law|legislation)\b",
    r"\b(?:Senate|House) (?:bill|insurance committee)\b",
    r"\bmarket conduct (?:action|examination|review)\b",
    r"\bproposed rate (?:change|increase|filing)\b",
)

_REGULATORY_RE = re.compile("|".join(_REGULATORY_KEYWORDS), re.IGNORECASE)


# ── User-editable weights (Obsidian _meta/Scoring Weights.md) ─────────
#
# The dict constants above are baked-in defaults. The user can override
# any value by editing the YAML frontmatter of `Scoring Weights.md` in
# the Obsidian vault's `_meta/` folder. The file is re-read at scoring
# time when its mtime changes — no Python edits needed for tuning.
#
# Missing file → use defaults. Malformed YAML → use defaults, warning
# logged. Unknown keys in overrides are ignored. Non-numeric values
# are ignored with a warning. The pipeline never breaks on a bad edit.

# Per-topic recency half-lives (days) for the exponential decay. A live cat
# advisory is stale in days; a reserving development or rate filing stays
# informative for weeks. Wave-2 follow-on: calibrate these from the decay of
# corroboration rate vs item age once the outcome store matures.
RECENCY_HALF_LIVES_DEFAULT: dict[str, float] = {
    "default":         7.0,
    "cat_event":       2.0,
    "regulatory_rate": 14.0,
    "reserving":       21.0,
}

# Cap on the PRODUCT of the three keyword boosts (inflation × regulatory ×
# tplf). The regex families are disjoint, but distinct phrases in one item can
# still stack multiplicatively; above the cap all three are scaled back
# proportionally (cube root) so the persisted factors still multiply to the
# score exactly.
KEYWORD_STACK_CAP_DEFAULT = 1.6

_DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "sources":          dict(SOURCE_MULT),
    "topics":           dict(TOPIC_PRIORITY_BOOST),
    "insurer_priority": dict(PRIORITY_INSURERS_BOOST),
    "insurer_names":    dict(PRIORITY_INSURER_NAMES),
    "keyword_boosts":   {"inflation": 1.2, "regulatory": 1.2, "tplf": 1.3,
                         "stack_cap": KEYWORD_STACK_CAP_DEFAULT},
    "burden_intensity": dict(BURDEN_INTENSITY_BOOST),
    # high/medium are the FIXED fallback cutoffs; high_quantile/medium_quantile
    # + min_n drive the quantile calibration (trailing-90d score distribution).
    "signal_tiers":     {**SIGNAL_TIER_DEFAULTS,
                         "high_quantile": 0.90, "medium_quantile": 0.60,
                         "min_n": 80},
    "recency_half_lives": dict(RECENCY_HALF_LIVES_DEFAULT),
    # PR3 activation flags — both REPORT-ONLY at 0 (the default). credibility
    # swaps in Bühlmann-implied source multipliers; loglinear applies the
    # learned exponents once its two-consecutive-pass gate is also met.
    "credibility": {"apply": 0.0, "gamma": 0.5, "horizon_days": 30.0},
    "loglinear":   {"apply": 0.0},
}

# Cache shape: (path, mtime, weights). Re-read only when the file's
# mtime changes; the helpers can call _load_scoring_weights() freely.
_WEIGHTS_CACHE: tuple[Path | None, float, dict[str, dict[str, float]]] | None = None


def _scoring_weights_path() -> Path | None:
    if not settings.obsidian_vault_path:
        return None
    return (
        Path(settings.obsidian_vault_path)
        / settings.obsidian_digest_dir
        / "_meta"
        / "Scoring Weights.md"
    )


def _read_overrides(path: Path | None) -> dict[str, Any]:
    """Read YAML frontmatter from the Obsidian scoring file. Empty dict
    on missing file or unparseable YAML."""
    if not path or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("scoring: read %s failed: %s", path, exc)
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    try:
        parsed = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "scoring: YAML parse failed in %s: %s — falling back to defaults",
            path, exc,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_float_dict(raw: Any) -> dict[str, float]:
    """Filter an override section to {str: float}; warn on bad entries."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            logger.warning("scoring: ignoring non-numeric override %s=%r", k, v)
    return out


def _load_scoring_weights() -> dict[str, dict[str, float]]:
    """Defaults merged with user overrides from Obsidian. Cached on mtime.

    The merge is per-section shallow — user-supplied keys override the
    matching default key; defaults for unmentioned keys are preserved.
    Sections the user doesn't mention fall through to defaults entirely.
    """
    global _WEIGHTS_CACHE
    path = _scoring_weights_path()
    mtime = 0.0
    if path and path.exists():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
    cached = _WEIGHTS_CACHE
    if cached and cached[0] == path and cached[1] == mtime:
        return cached[2]
    overrides = _read_overrides(path)
    merged: dict[str, dict[str, float]] = {}
    for section, defaults in _DEFAULT_WEIGHTS.items():
        merged[section] = {**defaults, **_coerce_float_dict(overrides.get(section))}
    _WEIGHTS_CACHE = (path, mtime, merged)
    if overrides:
        logger.info(
            "scoring: weights loaded from %s (mtime=%.0f, sections=%s)",
            path, mtime, sorted(overrides.keys()),
        )
    return merged


def tier_thresholds() -> tuple[float, float]:
    """(high, medium) FIXED score cutoffs — user-tunable via Scoring Weights.md.

    These are the fallback (and the ad-hoc display path). The scoring run
    itself uses `quantile_tier_thresholds`, which replaces them with empirical
    percentiles of the trailing score distribution once enough history exists;
    the tier each run stamps on the row is the authoritative one.
    """
    section = _load_scoring_weights().get("signal_tiers", SIGNAL_TIER_DEFAULTS)
    return (
        section.get("high",   SIGNAL_TIER_DEFAULTS["high"]),
        section.get("medium", SIGNAL_TIER_DEFAULTS["medium"]),
    )


def _percentile_value(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an ascending list (numpy-free)."""
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def quantile_tier_thresholds(
    weights: dict[str, dict[str, float]] | None = None, days: int = 90,
) -> tuple[float, float, str]:
    """(high, medium, basis) tier cutoffs, self-calibrating.

    With ≥min_n latest-scores in the trailing `days`, the cutoffs are the
    empirical P90/P60 (quantiles user-tunable via `signal_tiers.high_quantile`
    / `.medium_quantile`) — so 'high conviction' always means 'top decile of
    what the pipeline has actually been producing', no matter how weights,
    regime multipliers, or the source mix drift. Below min_n — or when the
    distribution is too degenerate to separate the two cutoffs — the fixed
    signal_tiers values apply. basis ∈ {'quantile', 'fixed'}.
    """
    if weights is None:
        weights = _load_scoring_weights()
    st = weights["signal_tiers"]
    fixed = (
        st.get("high",   SIGNAL_TIER_DEFAULTS["high"]),
        st.get("medium", SIGNAL_TIER_DEFAULTS["medium"]),
    )
    min_n = int(st.get("min_n", 80))
    scores = db.latest_scores_since(days=days)
    if len(scores) < min_n:
        return (*fixed, "fixed")
    hq = min(max(float(st.get("high_quantile",   0.90)), 0.0), 1.0)
    mq = min(max(float(st.get("medium_quantile", 0.60)), 0.0), 1.0)
    high   = _percentile_value(scores, hq)
    medium = _percentile_value(scores, mq)
    if not high > medium:        # mass ties → quantiles can't separate the tiers
        return (*fixed, "fixed")
    return round(high, 4), round(medium, 4), "quantile"


def tier_for_score(
    score: float | None,
    high: float | None = None,
    medium: float | None = None,
) -> str | None:
    """Map a leaderboard score to a conviction tier. None score → None tier.

    Thresholds default to the user-tuned `signal_tiers` weights when omitted.
    `score_item` passes the batch thresholds in so the tier it persists matches
    the score it was computed alongside.
    """
    if score is None:
        return None
    if high is None or medium is None:
        wh, wm = tier_thresholds()
        high = wh if high is None else high
        medium = wm if medium is None else medium
    if score >= high:
        return "high"
    if score >= medium:
        return "medium"
    return "low"


def tier_badge(score: float | None) -> str:
    """'🔴 High' / '🟡 Medium' / '🔵 Low' for a score; '' when score is None."""
    tier = tier_for_score(score)
    return f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]}" if tier else ""


def tier_badge_for_row(row: Any) -> str:
    """Badge for a leaderboard row. Prefers the PERSISTED tier — stamped with
    the (possibly quantile-calibrated) cutoffs in force when the row was
    scored — falling back to the fixed-threshold score mapping for rows
    persisted before tiers existed."""
    tier = row["tier"] if "tier" in row.keys() and row["tier"] else None
    if tier in TIER_EMOJI:
        return f"{TIER_EMOJI[tier]} {TIER_LABEL[tier]}"
    score = row["score"] if "score" in row.keys() else None
    return tier_badge(float(score)) if score is not None else ""


# ── TPLF / litigation-financing first-class boost (Wave 3 Phase 2) ────
#
# Fires when EITHER the LLM-classified sub_tags list contains
# 'litigation_tplf' OR the title/summary regex hits one of the explicit
# TPLF / mass-tort phrasings. Stacks on top of topic_priority_boost for
# social_inflation (1.4×) — a TPLF item lands at 1.4 × 1.3 = 1.82× topic
# weight, which is the user's intent given the Liability Intelligence
# cluster scope in Wave 3.

LITIGATION_TPLF_BOOST = 1.3

_TPLF_KEYWORDS = (
    r"\bthird[- ]party (?:litigation )?(?:financ\w+|fund\w+)\b",
    r"\bTPLF\b",
    r"\blitigation (?:funder|financier|finance|funding)\b",
    r"\battorney (?:advance|funding)\b",
    r"\bjudgment monetization\b",
    r"\bmass tort (?:funder|MDL)\b",
    r"\bnuclear verdict\b",
    r"\baggregate (?:settlement|verdict)\b",
    r"\bMDL panel\b",
    # Moved here from _INFLATION_KEYWORDS (de-dup): litigation-environment
    # phrases boost once, via this family only.
    r"\b(?:verdict|judgement|judgment|settlement)s?\b",
    r"\btort reform\b",
    r"\bsocial inflation\b",
)

_TPLF_RE = re.compile("|".join(_TPLF_KEYWORDS), re.IGNORECASE)


def _text_blob(row: Any) -> str:
    """Concatenate title + summary + why_it_matters for keyword scanning."""
    parts: list[str] = []
    for key in ("title", "summary", "why_it_matters"):
        if key in row.keys() and row[key]:
            parts.append(str(row[key]))
    return " ".join(parts)


def _litigation_tplf_boost(
    row: Any, blob: str, boost_value: float = 1.3, pressure: float | None = None
) -> float:
    """Returns `boost_value` when the LLM tagged the item with litigation_tplf
    sub_tag OR the title/summary names a TPLF / MDL signal; else 1.0.

    Lead 4: when the litigation-pressure index (`pressure`, 0-100) is elevated,
    the boost is scaled up (capped). `pressure` is None until the index is
    computed, so the boost is behavior-preserving by default. Pass the
    pre-computed `blob` so score_item doesn't rebuild it per helper.
    """
    fires = False
    sub_tags_json = row["sub_tags"] if "sub_tags" in row.keys() else None
    if sub_tags_json:
        try:
            fires = "litigation_tplf" in json.loads(sub_tags_json)
        except (TypeError, ValueError):
            fires = False
    if not fires:
        fires = bool(blob and _TPLF_RE.search(blob))
    if not fires:
        return 1.0
    from digest.litigation import tplf_pressure_boost
    return tplf_pressure_boost(boost_value, pressure)


def _regulatory_action_boost(blob: str, boost_value: float = 1.2) -> float:
    """Returns `boost_value` if the row blob names a state DOI action,
    insurer of last resort, or SERFF rate filing.
    """
    return boost_value if blob and _REGULATORY_RE.search(blob) else 1.0


# Lead 3 — Severity Tape: when the blended loss-cost tape is in an elevated
# regime, an inflation-keyword hit is worth more. Uplift + cap stay modest so
# the heuristic's character is preserved.
_SEVERITY_HOT_Z = 2.0
_SEVERITY_UPLIFT = 0.1
_SEVERITY_BOOST_CAP = 1.4


def _inflation_keyword_boost(
    blob: str, boost_value: float = 1.2, severity_z: float | None = None
) -> float:
    """`boost_value` if the row blob names an inflation driver, else 1.0.

    Lead 3: when `severity_z` (the blended FRED loss-cost tape) is hot (≥2σ), the
    keyword hit is magnitude-scaled up by `_SEVERITY_UPLIFT` (capped). `severity_z`
    is None until the tape has run, so the boost is behavior-preserving by default.
    """
    if not (blob and _INFLATION_RE.search(blob)):
        return 1.0
    if severity_z is not None and severity_z >= _SEVERITY_HOT_Z:
        return min(boost_value + _SEVERITY_UPLIFT, _SEVERITY_BOOST_CAP)
    return boost_value


def _insurer_priority_boost(
    source: str,
    metadata_json: Any,
    insurer_map: dict[str, float] | None = None,
    blob: str = "",
    name_map: dict[str, float] | None = None,
) -> float:
    """Carrier-priority boost: the max of a per-ticker boost (EDGAR items) and a
    carrier-name boost (any source, scanned over `blob`).

    The ticker path weights filings from public carriers; the name path extends
    that to trade-press / rate coverage AND to carriers with no EDGAR ticker at
    all (State Farm, a mutual). Combined as a max so an EDGAR item that also
    names its carrier isn't double-counted. `insurer_map`/`name_map` default to
    the module-level tables so older 3-arg callers (tests, scripts) are
    unchanged; production passes the user-overridable maps from
    _load_scoring_weights().
    """
    ticker_boost = 1.0
    if source == "edgar" and metadata_json:
        try:
            meta = json.loads(metadata_json)
        except (TypeError, ValueError):
            meta = {}
        ticker = (meta.get("ticker") or "").upper().strip()
        # Normalize BRK.A / BRK.B / BRK-B → BRK for matching
        if ticker.startswith("BRK"):
            ticker = "BRK"
        table = insurer_map if insurer_map is not None else PRIORITY_INSURERS_BOOST
        ticker_boost = table.get(ticker, 1.0)

    name_boost = 1.0
    names = name_map if name_map is not None else PRIORITY_INSURER_NAMES
    if blob and names:
        for name, boost in names.items():
            if boost > name_boost and re.search(
                rf"\b{re.escape(name)}\b", blob, re.IGNORECASE
            ):
                name_boost = boost

    return max(ticker_boost, name_boost)


def _reserve_deterioration_boost(blob: str, reserve_map: dict[str, float]) -> float:
    """Boost for an item naming an insurer with adverse reserve development.

    Delegates to digest.reserving (lazy import keeps signals decoupled from the
    reserving/outcomes modules at load time). 1.0 when reserve_map is empty —
    i.e. until `digest reserving` has produced data — so the formula is
    behaviour-preserving today.
    """
    if not reserve_map:
        return 1.0
    from digest.reserving import reserve_deterioration_boost
    return reserve_deterioration_boost(blob, reserve_map)


# ── Recency ────────────────────────────────────────────────────────────

RECENCY_FLOOR = 0.1   # exponential tail floor (old linear ramp floored at 0.3)


def _recency(
    published_iso: str | None,
    ingested_iso: str | None,
    topic: str | None = None,
    half_lives: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> float:
    """True exponential decay 2^(−age/h) with a PER-TOPIC half-life h.

    The old implementation was a linear ramp (misnamed half-life) with one
    global rate — but a cat advisory and a reserving development have wildly
    different information half-lives. h comes from the `recency_half_lives`
    weights section (cat_event 2d, regulatory_rate 14d, reserving 21d,
    default 7d). Uses published_at if present, otherwise ingested_at; floored
    at RECENCY_FLOOR; missing/unparseable timestamps → 0.6 (unchanged).

    `as_of` replaces "now" as the age reference — the historical backfill
    scores items as-of their filing date, so recency must decay from then,
    not from the wall clock. None (the live path) keeps now().
    """
    hl = half_lives if half_lives is not None else RECENCY_HALF_LIVES_DEFAULT
    h = float(hl.get((topic or "").lower()) or hl.get("default", 7.0))
    raw = published_iso or ingested_iso
    if not raw:
        return 0.6
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.6
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ref = as_of if as_of is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (ref - ts).total_seconds() / 86400.0)
    decay = max(RECENCY_FLOOR, 2.0 ** (-age_days / max(h, 0.1)))
    return round(decay, 3)


# ── Topic relevance (placeholder; tune in Wave 2.x) ───────────────────


def _topic_relevance(topic: str | None, regime: RegimeSignal) -> float:
    """Currently 1.0 for every topic — keeps the formula intact so we can
    enable topic-by-regime tuning later without a schema change.

    Where it'll go (commented for later wiring):
      regime.cat_load == "post_major_event":
          cat_event           → 1.3
          personal_lines      → 1.1   # consumer-impact angle
      regime.market_cycle in {"hard_market", "transitioning_to_hard"}:
          reinsurance_cycle   → 1.15
          underwriting_results→ 1.1
    """
    return 1.0


# ── Scoring ───────────────────────────────────────────────────────────


@dataclass
class Score:
    item_id: int
    score: float
    tier: str
    source_mult: float
    regime_mult: float
    topic_relevance: float
    recency: float
    llm_judgment: float
    topic_boost: float
    burden_boost: float
    insurer_boost: float
    inflation_boost: float
    regulatory_boost: float
    tplf_boost: float
    reserve_boost: float = 1.0
    learned_score: float | None = None

    def as_row(self, computed_at: str) -> dict[str, Any]:
        return {
            "item_id":          self.item_id,
            "computed_at":      computed_at,
            "score":            self.score,
            "tier":             self.tier,
            "source_mult":      self.source_mult,
            "regime_mult":      self.regime_mult,
            "topic_relevance":  self.topic_relevance,
            "recency":          self.recency,
            "llm_judgment":     self.llm_judgment,
            "topic_boost":      self.topic_boost,
            "burden_boost":     self.burden_boost,
            "insurer_boost":    self.insurer_boost,
            "inflation_boost":  self.inflation_boost,
            "regulatory_boost": self.regulatory_boost,
            "tplf_boost":       self.tplf_boost,
            "reserve_boost":    self.reserve_boost,
            "learned_score":    self.learned_score,
        }


def score_item(
    row: Any,
    regime: RegimeSignal,
    weights: dict[str, dict[str, float]] | None = None,
    reserve_map: dict[str, float] | None = None,
    severity_z: float | None = None,
    litigation_pressure: float | None = None,
    calibrator: Any | None = None,
    tier_cutoffs: tuple[float, float] | None = None,
    exponents: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> Score:
    """Compute the leaderboard score for one item row.

    Weights resolution order: explicit `weights` arg → cached load from
    Obsidian _meta/Scoring Weights.md → module-level defaults. Production
    `run_signals` passes a single resolved dict so a per-batch reload
    happens at most once; ad-hoc callers can omit and pay the cached lookup.

    `calibrator` (isotonic materiality → P(corroborated), see calibration.py)
    replaces the raw materiality clamp when fitted; None → raw clamp (the
    pre-calibration behavior). `tier_cutoffs` lets run_signals stamp tiers
    from the batch's quantile-calibrated thresholds; None → fixed weights.
    `as_of` makes recency decay from a historical timestamp instead of now
    (the backfill path); the caller must also pass the regime in force then.
    """
    if weights is None:
        weights = _load_scoring_weights()

    source     = (row["source"] or "").lower()
    topic      = (row["topic"]  or "").lower()

    sources    = weights["sources"]
    src_mult   = sources.get(source, sources.get("default", SOURCE_MULT_DEFAULT))
    rg_mult    = regime.multiplier
    topic_rel  = _topic_relevance(topic, regime)
    rec        = _recency(
        row["published_at"] if "published_at" in row.keys() else None,
        row["ingested_at"]  if "ingested_at"  in row.keys() else None,
        topic=topic,
        half_lives=weights.get("recency_half_lives"),
        as_of=as_of,
    )

    materiality = row["materiality_score"] if "materiality_score" in row.keys() else None
    if calibrator is not None and materiality is not None:
        llm_j = calibrator.judgment(materiality)
    else:
        try:
            llm_j = float(materiality) if materiality is not None else 1.0
        except (TypeError, ValueError):
            llm_j = 1.0
        llm_j = max(0.5, min(1.5, llm_j))

    topic_boost  = weights["topics"].get(topic, 1.0)

    burden_intensity = (
        row["burden_intensity"] if "burden_intensity" in row.keys() else None
    )
    burden_boost = weights["burden_intensity"].get(
        (burden_intensity or "").lower(), 1.0
    )

    kw = weights["keyword_boosts"]
    metadata_json = row["metadata_json"] if "metadata_json" in row.keys() else None
    blob = _text_blob(row)   # build once, reuse across the three keyword helpers
    insurer_boost    = _insurer_priority_boost(
        source, metadata_json, weights["insurer_priority"], blob, weights["insurer_names"])
    inflation_boost  = _inflation_keyword_boost(blob,      kw.get("inflation",  1.2), severity_z)
    regulatory_boost = _regulatory_action_boost(blob,      kw.get("regulatory", 1.2))
    tplf_boost       = _litigation_tplf_boost(row, blob,   kw.get("tplf",       1.3), litigation_pressure)

    # Keyword stack cap: the three keyword families are disjoint, but distinct
    # phrases in one item can still stack multiplicatively. Above the cap all
    # three are scaled back proportionally (cube root), so the persisted
    # factors still multiply to the score exactly.
    cap = float(kw.get("stack_cap", KEYWORD_STACK_CAP_DEFAULT))
    stack = inflation_boost * regulatory_boost * tplf_boost
    if cap > 0 and stack > cap:
        shrink = (cap / stack) ** (1.0 / 3.0)
        inflation_boost  *= shrink
        regulatory_boost *= shrink
        tplf_boost       *= shrink

    # Option 5: adverse reserve development on a named insurer. Neutral (1.0)
    # until reserving_signals has data (reserve_map empty → no-op).
    reserve_boost    = _reserve_deterioration_boost(blob, reserve_map or {})

    # The score is Π fᵢ^wᵢ — wᵢ ≡ 1 (the default) is the plain product. The
    # log-linear gate (loglinear.py) supplies learned exponents only when it
    # has passed twice consecutively AND the user flipped `loglinear.apply`;
    # factor columns persist RAW either way, so exponents stay auditable.
    factors = {
        "source_mult": src_mult, "regime_mult": rg_mult,
        "topic_relevance": topic_rel, "recency": rec, "llm_judgment": llm_j,
        "topic_boost": topic_boost, "burden_boost": burden_boost,
        "insurer_boost": insurer_boost, "inflation_boost": inflation_boost,
        "regulatory_boost": regulatory_boost, "tplf_boost": tplf_boost,
        "reserve_boost": reserve_boost,
    }
    if exponents:
        score = 1.0
        for name, val in factors.items():
            score *= max(val, 1e-6) ** float(exponents.get(name, 1.0))
    else:
        score = 1.0
        for val in factors.values():
            score *= val
    if tier_cutoffs is not None:
        tier = tier_for_score(score, tier_cutoffs[0], tier_cutoffs[1])
    else:
        st = weights["signal_tiers"]
        tier = tier_for_score(score, st.get("high"), st.get("medium"))
    return Score(
        item_id=int(row["id"]),
        score=round(score, 4),
        tier=tier,
        source_mult=round(src_mult, 3),
        regime_mult=round(rg_mult, 3),
        topic_relevance=round(topic_rel, 3),
        recency=rec,
        llm_judgment=round(llm_j, 3),
        topic_boost=round(topic_boost, 3),
        burden_boost=round(burden_boost, 3),
        insurer_boost=round(insurer_boost, 3),
        inflation_boost=round(inflation_boost, 3),
        regulatory_boost=round(regulatory_boost, 3),
        tplf_boost=round(tplf_boost, 3),
        reserve_boost=round(reserve_boost, 3),
    )


# ── Public API ────────────────────────────────────────────────────────


def run_signals() -> dict[str, int]:
    """Score every kept+summarized item against the current regime; persist."""
    regime = current_regime()
    rows   = db.items_for_signals()
    if not rows:
        logger.info("signals: nothing to score")
        return {"scored": 0}

    # Resolve weights once per batch so the YAML mtime stat happens at most
    # once even when scoring hundreds of items. Edits to Scoring Weights.md
    # land on the next run, not mid-batch.
    weights = _load_scoring_weights()
    # PR3 — credibility apply (user opt-in, default report-only): swap the
    # hand-set source multipliers for the Bühlmann-implied ones. A copy, never
    # a mutation — the weights cache must keep the hand map.
    if float(weights.get("credibility", {}).get("apply", 0.0)) >= 1.0:
        from digest.credibility import adjusted_source_multipliers
        adjusted = adjusted_source_multipliers(weights)
        if adjusted:
            weights = {**weights, "sources": adjusted}
            logger.info("signals: credibility-adjusted source multipliers in force")
    # PR3 — log-linear exponents: None unless gate passed twice AND user opted in.
    from digest.loglinear import active_weights as _loglinear_active
    exponents = _loglinear_active(weights.get("loglinear"))
    if exponents:
        logger.info("signals: log-linear exponents in force (gate passed ×2 + apply flag)")
    reserve_map = db.reserving_severity_map()      # Option 5 (empty until data)
    from digest.severity_tape import severity_regime
    severity_z = severity_regime()                 # Lead 3 (None until tape runs)
    from digest.litigation import pressure_signal
    litigation_pressure = pressure_signal()        # Lead 4 (None until index runs)
    from digest.calibration import latest_materiality_calibrator
    calibrator = latest_materiality_calibrator()   # None until fitted (raw clamp)
    # Tier cutoffs for this batch: trailing-90d quantiles once history exists,
    # the fixed signal_tiers values before that. Resolved once and stamped on
    # every row so the persisted tier matches the thresholds actually in force.
    tier_high, tier_medium, tier_basis = quantile_tier_thresholds(weights)
    computed_at = datetime.now(timezone.utc).isoformat()

    # Option 4: if a learned model exists, attach its score alongside the
    # heuristic (ranking stays on the heuristic). NULL when no model trained yet.
    model = None
    row_to_features = None
    meta = db.latest_learned_model()
    if meta:
        from digest.learn import LogisticModel, row_to_features as _rtf
        model = LogisticModel.from_json(meta["model_json"])
        row_to_features = _rtf

    scored: list[dict[str, Any]] = []
    for row in rows:
        s = score_item(row, regime, weights=weights, reserve_map=reserve_map,
                       severity_z=severity_z, litigation_pressure=litigation_pressure,
                       calibrator=calibrator, tier_cutoffs=(tier_high, tier_medium),
                       exponents=exponents)
        d = s.as_row(computed_at)
        if model is not None:
            feat = dict(d)
            feat["materiality_score"] = (
                row["materiality_score"] if "materiality_score" in row.keys() else None
            )
            d["learned_score"] = round(float(model.predict_proba([row_to_features(feat)])[0]), 4)
        scored.append(d)

    inserted = db.upsert_signal_scores(scored)
    logger.info(
        "signals: scored %d items at regime ×%.2f (inserted=%d, learned=%s, "
        "calibrated=%s, tiers=%s high=%.3f medium=%.3f)",
        len(scored), regime.multiplier, inserted, "on" if model else "off",
        "on" if calibrator else "off", tier_basis, tier_high, tier_medium,
    )
    return {"scored": len(scored), "tier_basis": tier_basis,
            "tier_high": tier_high, "tier_medium": tier_medium}


def top_n(n: int = 5, since_iso: str | None = None) -> list[Any]:
    """Convenience: top-N items by latest score, since `since_iso`."""
    return db.top_signal_scores(limit=n, since_iso=since_iso)
