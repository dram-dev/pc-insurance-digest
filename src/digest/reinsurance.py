"""Reinsurance Pulse (EKG Lead 1) — priced ROL/ILS → regime.market_cycle.

The regime's market_cycle axis is otherwise LLM-judged from trade-press *narrative*
(`regime.compute_market_cycle`). This lead adds the cleanest market-*priced* read
of the reinsurance cycle — rate-on-line and ILS/cat-bond spreads — so the
classifier reflects what capital is actually charging, not just tone:

    GuyCarp ROL index · Artemis/Lane ILS spreads   (scrape / published series)
      → run_reinsurance()  → reduce_series() (latest, slope, 12m z-score, trend)
      → db.upsert_reinsurance_pricing()  (local mirror of pc_bronze.reinsurance_pricing)
      → pricing_signal() / market_cycle_hint()
      → regime.compute_market_cycle()  (firm-only nudge)

**Source status.** GuyCarp/Artemis/Lane publish these as commentary / PDFs /
HTML tables, not clean APIs — so the fetchers ship as a config-driven scaffold
(`config/reinsurance_sources.yaml`, every source `enabled: false`) pending live
selector validation on the Mac mini, exactly like `serff.py` / `state_doi.py`.
The *reducer* and the *regime hook* are complete and tested; `run_reinsurance()`
is a clean no-op until a source is enabled, so the market_cycle axis is
behavior-preserving by default.

Databricks-native upgrade: `ai_forecast()` projects the next renewal's direction
off the ROL/spread series; this numpy slope + z-score (the `fred.py` pattern) is
the Free-Edition default.
"""
from __future__ import annotations

import logging
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from digest import db

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "reinsurance_sources.yaml"
_REQUEST_TIMEOUT = 20
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_ANOMALY_Z = 1.5
# How firm the priced signal must be before it nudges market_cycle.
_FIRMING_Z = 1.0
_HARD_Z = 2.0


def reduce_series(observations: list[tuple[str, float]]) -> dict | None:
    """Reduce a dated ROL/spread series → latest value + 12m z-score + trend.

    `observations` is [(iso_date, value)] oldest-first. Returns None if there are
    too few points. `trend` is the sign of the latest vs the trailing mean:
    'firming' (priced up), 'softening' (priced down), or 'flat'.
    """
    pts = [(d, float(v)) for d, v in observations if v is not None]
    if len(pts) < 4:
        return None
    pts.sort(key=lambda p: p[0])
    values = [v for _, v in pts]
    latest_date, latest = pts[-1]
    baseline = values[:-1]
    mean = statistics.fmean(baseline)
    stdev = statistics.pstdev(baseline)
    z = (latest - mean) / stdev if stdev > 0 else 0.0
    if latest > mean * 1.005:
        trend = "firming"
    elif latest < mean * 0.995:
        trend = "softening"
    else:
        trend = "flat"
    return {
        "observation_date": latest_date, "value": round(latest, 4),
        "zscore_12m": round(z, 3), "trend": trend,
        "is_anomaly": int(abs(z) >= _ANOMALY_Z),
    }


def _load_config() -> list[dict]:
    if not _CONFIG_PATH.exists():
        return []
    cfg = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    return cfg.get("indices", []) or []


def _parse_highcharts_year_series(html: str) -> list[tuple[str, float]]:
    """Pull a (year, value) series out of an Artemis ROL Highcharts config.

    The ROL index pages render the chart inline as a Highcharts block:
    `categories: ['1990', ..., '2026*']` (years, a trailing `*` marks a
    preliminary print) paired with a `series` `data: [100, 115, ...]` numeric
    array. We take the categories array that's a run of 4-digit years and the
    first numeric `data:` array of the SAME length, so other charts on the page
    (different lengths) don't get picked up. Returns [] if nothing matches.
    """
    years: list[str] | None = None
    for m in re.finditer(r"categories:\s*\[([^\]]*)\]", html):
        toks = re.findall(r"'(\d{4})\*?'", m.group(1))
        if len(toks) >= 4:
            years = toks
            break
    if not years:
        return []
    for m in re.finditer(r"data:\s*\[([0-9.,\s]+)\]", html):
        vals = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.group(1))]
        if len(vals) == len(years):
            return list(zip(years, vals))
    return []


def _fetch_artemis_rol(entry: dict) -> list[tuple[str, float]]:
    """Artemis-hosted property-cat Rate-on-Line index (republishes the Guy
    Carpenter series) → [(YYYY-01-01, index_value)] oldest-first.

    Static fetch — the series lives in the page's inline Highcharts config, so no
    headless render is needed. Any failure logs and returns [] (a clean no-op)."""
    url = entry["url"]
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reinsurance: %s fetch failed: %s", entry.get("index_name"), exc)
        return []
    series = _parse_highcharts_year_series(r.text)
    if not series:
        logger.warning(
            "reinsurance: %s — no ROL series parsed from %s (page structure may "
            "have changed)", entry.get("index_name"), url,
        )
    return [(f"{year}-01-01", value) for year, value in series]


# Per-source parsers, keyed on entry['source']. Add a new source by writing its
# fetcher and registering it here — `run_reinsurance()` and the reducer/regime
# hook stay unchanged.
_FETCHERS = {
    "artemis_rol": _fetch_artemis_rol,
}


def _fetch_series(entry: dict) -> list[tuple[str, float]]:
    """Fetch an index's observation series, dispatched on entry['source'].

    Sources without a registered fetcher (guycarp commentary, lane PDFs, the
    artemis deal-directory spread aggregation) return [] — a clean no-op — until
    their parser is implemented + validated."""
    fetcher = _FETCHERS.get(entry.get("source", ""))
    if fetcher is None:
        logger.info("reinsurance: %s scraper not yet implemented — skipping",
                    entry.get("index_name"))
        return []
    return fetcher(entry)


def run_reinsurance() -> dict[str, int]:
    """Reduce each enabled index's series → upsert reinsurance_pricing rows.

    No-op (0 written) until a source is flipped `enabled: true` in
    config/reinsurance_sources.yaml and its `_fetch_series` parser is live."""
    enabled = [e for e in _load_config() if e.get("enabled", False)]
    if not enabled:
        logger.info("reinsurance: no indices enabled — validate scrapers on the "
                    "Mac mini and flip enabled:true in reinsurance_sources.yaml")
        return {"indices": 0, "written": 0}
    fetched_at = datetime.now(tz=timezone.utc).isoformat()
    rows: list[dict] = []
    for entry in enabled:
        try:
            reduced = reduce_series(_fetch_series(entry))
        except Exception as exc:  # noqa: BLE001
            logger.warning("reinsurance: %s failed: %s", entry.get("index_name"), exc)
            continue
        if reduced:
            rows.append({
                "index_name": entry["index_name"], "segment": entry.get("segment"),
                "source": entry.get("source"), "fetched_at": fetched_at, **reduced,
            })
    written = db.upsert_reinsurance_pricing(rows)
    logger.info("reinsurance: %d indices → %d rows", len(enabled), written)
    return {"indices": len(enabled), "written": written}


def pricing_signal() -> dict:
    """Latest priced reinsurance reading for the regime hook, or {} when none."""
    row = db.latest_reinsurance_pricing()
    if row is None or row["zscore_12m"] is None:
        return {}
    return {"rol_z": float(row["zscore_12m"]), "trend": row["trend"],
            "index_name": row["index_name"]}


def market_cycle_hint(pricing: dict | None = None) -> str | None:
    """Priced market_cycle bias from ROL/spread, or None when no signal.

    Firm-only: priced firming nudges toward a harder cycle; we don't soften on
    price alone (the LLM narrative governs softening, which is slower to confirm).
    """
    if pricing is None:
        pricing = pricing_signal()
    z, trend = pricing.get("rol_z"), pricing.get("trend")
    if z is None or trend != "firming":
        return None
    if z >= _HARD_Z:
        return "hard_market"
    if z >= _FIRMING_Z:
        return "transitioning_to_hard"
    return None
