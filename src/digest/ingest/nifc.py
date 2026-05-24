"""NIFC wildfire ingestor — active incidents via NIFC WFIGS ArcGIS REST API.

InciWeb RSS at /feeds/rss/incidents/ returns 404 (migrated away). The
authoritative replacement is the WFIGS (Wildland Fire Incident Geospatial
Service) public ArcGIS feature service maintained by NIFC.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import requests

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

_WFIGS_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)
_QUERY_PARAMS = {
    "where": "PercentContained < 100 AND IncidentSize >= 1000",
    "outFields": (
        "OBJECTID,IncidentName,POOState,IncidentSize,PercentContained,"
        "FireDiscoveryDateTime,FireBehaviorGeneral,EstimatedCostToDate,"
        "ModifiedOnDateTime_dt"
    ),
    "f": "json",
    "resultRecordCount": 30,
    "orderByFields": "IncidentSize DESC",
}
_REQUEST_TIMEOUT = 30
_WFIGS_BASE = "https://inciweb.wildfire.gov"


class NIFCIngestor(IngestorBase):
    name = "nifc"

    def fetch(self) -> list[IngestedItem]:
        r = requests.get(_WFIGS_URL, params=_QUERY_PARAMS, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        if "error" in data:
            raise RuntimeError(f"WFIGS API error: {data['error']}")

        items: list[IngestedItem] = []
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {})
            name = attrs.get("IncidentName") or "Unknown Fire"
            state = (attrs.get("POOState") or "").replace("US-", "")
            acres = attrs.get("IncidentSize") or 0
            pct = attrs.get("PercentContained")
            behavior = attrs.get("FireBehaviorGeneral") or "Unknown"
            cost = attrs.get("EstimatedCostToDate")
            obj_id = attrs.get("OBJECTID", 0)

            title = (
                f"{name} Fire — {state} — {acres:,.0f} ac, "
                f"{pct}% contained ({behavior})"
            )
            content_parts = [
                f"Active wildfire: {name}, {state}.",
                f"Size: {acres:,.0f} acres, {pct}% contained.",
                f"Fire behavior: {behavior}.",
            ]
            if cost:
                content_parts.append(f"Estimated cost: ${cost:,.0f}.")

            ts_ms = attrs.get("FireDiscoveryDateTime")
            pub = datetime.fromtimestamp(ts_ms / 1000) if ts_ms else None

            source_id = hashlib.sha1(f"wfigs:{obj_id}".encode()).hexdigest()[:16]

            items.append(
                IngestedItem(
                    source=self.name,
                    source_id=source_id,
                    title=title,
                    url=None,
                    content=" ".join(content_parts),
                    published_at=pub,
                    metadata={
                        "topic_hint": "cat_event",
                        "state": state,
                        "acres": acres,
                        "pct_contained": pct,
                        "behavior": behavior,
                        "estimated_cost": cost,
                        "wfigs_id": obj_id,
                    },
                )
            )
        return items
