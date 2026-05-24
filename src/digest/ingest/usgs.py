"""USGS earthquake ingestor — M≥5.0 events from the past day via GeoJSON feed."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import requests

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

# M4.5+ past-day feed; we filter to M≥5.0 inside fetch() to keep noise down
# while still capturing moderate events that can affect underwriting sentiment.
_FEED_URL = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
)
_MIN_MAGNITUDE = 5.0
_REQUEST_TIMEOUT = 30


class USGSIngestor(IngestorBase):
    name = "usgs"

    def fetch(self) -> list[IngestedItem]:
        r = requests.get(_FEED_URL, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        items: list[IngestedItem] = []
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            mag = props.get("mag")
            if mag is None or mag < _MIN_MAGNITUDE:
                continue

            eq_id = feat.get("id", "")
            place = props.get("place") or "unknown location"
            title = props.get("title") or f"M{mag:.1f} - {place}"
            url = props.get("url")

            ts_ms = props.get("time")
            pub = datetime.fromtimestamp(ts_ms / 1000) if ts_ms else None

            coords = (feat.get("geometry") or {}).get("coordinates") or []
            depth_km: float | None = coords[2] if len(coords) >= 3 else None

            content = (
                f"M{mag:.1f} earthquake near {place}. "
                f"Depth: {f'{depth_km:.1f} km' if depth_km is not None else 'unknown'}. "
                f"Felt reports: {props.get('felt') or 0}. "
                f"Alert level: {props.get('alert') or 'none'}."
            )

            source_id = (
                hashlib.sha1(eq_id.encode()).hexdigest()[:16]
                if eq_id
                else hashlib.sha1(title.encode()).hexdigest()[:16]
            )
            items.append(
                IngestedItem(
                    source=self.name,
                    source_id=source_id,
                    title=title,
                    url=url,
                    content=content,
                    published_at=pub,
                    metadata={
                        "topic_hint": "cat_event",
                        "magnitude": mag,
                        "place": place,
                        "depth_km": depth_km,
                        "usgs_id": eq_id,
                        "alert": props.get("alert"),
                    },
                )
            )
        return items
