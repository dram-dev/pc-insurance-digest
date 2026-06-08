"""FRED ingestor — monthly CPI/PPI series tracking P&C loss-cost drivers.

Each configured series in `config/fred_series.yaml` is fetched (trailing
24 months), the latest m/m % change is computed, and an `IngestedItem`
is emitted ONLY when |z-score| over the trailing 12 months exceeds
`fred_zscore_threshold` (default 1.5σ). Routine sub-σ prints are
discarded so the daily note shows only anomalous loss-cost moves.

Auto-keep is handled by `db.auto_keep_quantitative()` — FRED is in
`QUANT_SOURCES`, so items reach the daily note without going through
Qwen triage. Topic is locked at `topic_hint` from the YAML config.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "fred_series.yaml"
_FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
_LOOKBACK_MONTHS = 24      # fetch 2y; compute z-score over trailing 12 of m/m %
_REQUEST_TIMEOUT = 20


def _fetch_series(series_id: str, limit: int = _LOOKBACK_MONTHS) -> list[dict[str, Any]]:
    """Return the last `limit` observations for a FRED series, oldest first.

    Defaults to `_LOOKBACK_MONTHS` (the 2y window the anomaly ingestor needs);
    the Severity Tape passes a longer `limit` to backfill a trend-able history.
    """
    params = {
        "series_id":      series_id,
        "api_key":        settings.fred_api_key,
        "file_type":      "json",
        "sort_order":     "desc",
        "limit":          limit,
    }
    r = requests.get(_FRED_API_URL, params=params, timeout=_REQUEST_TIMEOUT)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    # API returns desc; flip to asc for series math
    return list(reversed(obs))


def _mom_pct_changes(observations: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """Return [(date, m/m % change)] for the observations, skipping missing values."""
    out: list[tuple[str, float]] = []
    prev: float | None = None
    for o in observations:
        raw = o.get("value")
        if raw is None or raw == "." or raw == "":
            prev = None
            continue
        try:
            v = float(raw)
        except ValueError:
            prev = None
            continue
        if prev is not None and prev != 0:
            pct = (v - prev) / prev * 100.0
            out.append((o.get("date", ""), pct))
        prev = v
    return out


class FredIngestor(IngestorBase):
    name = "fred"

    def __init__(self) -> None:
        if not settings.fred_api_key:
            raise RuntimeError(
                "FRED_API_KEY not set. Get a free key at "
                "https://fred.stlouisfed.org/docs/api/api_key.html"
            )
        if not _CONFIG_PATH.exists():
            raise RuntimeError(f"FRED series config missing: {_CONFIG_PATH}")
        self.config = yaml.safe_load(_CONFIG_PATH.read_text())
        self.threshold = float(settings.fred_zscore_threshold)

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for entry in self.config.get("series", []):
            series_id  = entry["id"]
            label      = entry.get("label", series_id)
            topic_hint = entry.get("topic_hint", "supply_chain")

            try:
                obs = _fetch_series(series_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("fred: fetch failed for %s: %s", series_id, exc)
                continue

            changes = _mom_pct_changes(obs)
            if len(changes) < 6:
                logger.debug("fred: %s has only %d m/m points; skipping", series_id, len(changes))
                continue

            window = [pct for _, pct in changes[-12:]]
            latest_date, latest_pct = changes[-1]

            # z-score of latest m/m change vs prior 12-month window
            history = window[:-1] if len(window) > 1 else window
            if len(history) < 3:
                continue
            mean = statistics.fmean(history)
            stdev = statistics.pstdev(history)
            if stdev == 0:
                continue
            z = (latest_pct - mean) / stdev

            if abs(z) < self.threshold:
                logger.debug(
                    "fred: %s latest=%.2f%% z=%.2f below threshold; skipping",
                    series_id, latest_pct, z,
                )
                continue

            direction = "rose" if latest_pct > 0 else "fell"
            sigma_str = f"{z:+.2f}σ"
            title = (
                f"{label} {direction} {abs(latest_pct):.2f}% m/m ({latest_date}, {sigma_str})"
            )
            content = (
                f"FRED series {series_id} ({label}) {direction} "
                f"{abs(latest_pct):.2f}% month-over-month in {latest_date}. "
                f"That is {sigma_str} versus the trailing 12-month distribution "
                f"(mean {mean:+.2f}%, stdev {stdev:.2f}%). "
                f"Higher loss-cost inflation in {label.lower()} typically flows "
                f"into P&C personal-auto and homeowners severity within 1-2 quarters."
            )

            # Parse YYYY-MM-DD to a UTC datetime so the recency factor works.
            try:
                pub = datetime.strptime(latest_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pub = None

            items.append(
                IngestedItem(
                    source=self.name,
                    source_id=f"{series_id}:{latest_date}",
                    title=title,
                    url=f"https://fred.stlouisfed.org/series/{series_id}",
                    author="Federal Reserve Economic Data",
                    content=content,
                    published_at=pub,
                    metadata={
                        "topic_hint":    topic_hint,
                        "series_id":     series_id,
                        "label":         label,
                        "latest_date":   latest_date,
                        "mom_pct":       round(latest_pct, 3),
                        "z_score":       round(z, 3),
                        "window_mean":   round(mean, 3),
                        "window_stdev":  round(stdev, 3),
                    },
                )
            )
            logger.info(
                "fred: %s anomaly latest=%+.2f%% z=%+.2f → emitted",
                series_id, latest_pct, z,
            )
        return items
