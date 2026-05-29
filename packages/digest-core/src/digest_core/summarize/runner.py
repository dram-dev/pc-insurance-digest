"""Summarizer runner mechanics — generic JSON repair + per-topic share caps.

Both are domain-agnostic: `extract_json` salvages a JSON object from messy model
output; `enforce_topic_caps` trims a scored queue so no capped topic exceeds its
share. The cap *values* (which topic, what fraction) and the topic taxonomy stay
domain-side — this only provides the mechanics.
"""
from __future__ import annotations

import json
import re
from typing import Any


def extract_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON-object extraction from model output.

    Tries, in order: the whole string, a ```json fenced block, then a greedy
    first-brace-to-last-brace span. Returns None if nothing parses.
    """
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Greedy capture across whole string in case of multi-line JSON
    m = re.search(r"(\{.*\})", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def enforce_topic_caps(
    rows: list,
    caps: dict[str, float],
) -> tuple[list, dict[str, int]]:
    """Drop lowest-score items from over-cap topics so each capped topic's
    share of the final queue stays ≤ its max_pct.

    Args:
        rows: queue rows (each indexable by 'topic', 'id', 'triage_score'),
              already sorted by triage_score desc.
        caps: {topic: max_pct} where max_pct ∈ (0, 1) is the maximum fraction
              of the final queue that topic may occupy (0.35 = 35%).

    Returns: (filtered_rows_in_original_order, {topic: count_dropped}).
    """
    if not caps or not rows:
        return list(rows), {}

    by_topic: dict[str, list] = {}
    for r in rows:
        by_topic.setdefault(r["topic"] or "", []).append(r)

    keep_ids: set = {r["id"] for r in rows}
    dropped: dict[str, int] = {}

    for topic, max_pct in caps.items():
        if not 0.0 < max_pct < 1.0 or topic not in by_topic:
            continue
        target_count = len(by_topic[topic])
        other_count = len(rows) - target_count
        # target ≤ max_pct × (target + other)  ⇒  target ≤ (max_pct / (1 - max_pct)) × other
        max_allowed = int((max_pct / (1.0 - max_pct)) * other_count)
        if target_count <= max_allowed:
            continue
        # Drop the lowest-score items from this topic.
        ranked = sorted(by_topic[topic], key=lambda r: r["triage_score"] or 0, reverse=True)
        to_drop = ranked[max_allowed:]
        dropped[topic] = len(to_drop)
        for r in to_drop:
            keep_ids.discard(r["id"])

    return [r for r in rows if r["id"] in keep_ids], dropped
