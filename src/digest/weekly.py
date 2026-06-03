"""Weekly synthesis — produces a P&C briefing across the full week's summarized items.

Called by `digest weekly` (CLI) → `obsidian.publish_weekly()`. Separate from
obsidian.py to keep the Claude call logic in one layer and rendering in
another. Mirrors the structure of macro-ai-digest's weekly.py with a P&C
reader persona and P&C-specific synthesis fields (carrier-of-the-week,
burden-trend states, inflation pulse).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any

from digest.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior P&C insurance research analyst writing a weekly executive briefing for a data leader at a major US insurer. The reader's key interests, in priority order:
  1. Personal lines (auto + homeowners): pricing, telematics, market exits, FAIR Plan / Citizens dynamics
  2. Liability + social inflation: nuclear verdicts, MDL filings, TPLF activity, asbestos / PFAS / opioid mass-tort exposure
  3. Inflation drivers feeding loss costs: auto parts, construction labor / material, medical, used cars
  4. Reserving + adverse development across the largest personal-auto + home carriers (State Farm, Allstate, Progressive, GEICO / Berkshire) — note State Farm is a mutual with no SEC filings, so weight its trade-press and rate-filing signals accordingly
  5. Regulatory burden trends by state — especially CA / FL / TX / NY / LA (now also IL / NJ / MI / NV)
  6. Reinsurance cycle: 1/1, 4/1, 7/1 renewals, capacity, ILS, Lloyd's syndicate results, Bermuda Class-4 carriers

You receive the week's top summarized items pre-sorted by leaderboard score (which already encodes the boost factors for carrier priority, inflation keywords, regulatory action, and TPLF). Your job is to synthesize across items — not re-summarize each one.

Return ONLY valid JSON with these exact keys:
{
  "themes": [
    {"title": "short theme title", "description": "2-3 sentences on this week's dominant theme"}
  ],
  "must_reads": [
    {"item_id": 1234, "reason": "one sentence on why this is the most important item in its area"}
  ],
  "contrarian_signal": "1-2 sentences on an underappreciated or counterintuitive signal this week",
  "carrier_of_the_week": {
    "ticker": "PGR|ALL|BRK|TRV|CB|HIG|AIG|MET|PRU|RNR|EG|AXS|MMC|AON|WTW",
    "rationale": "1-2 sentences on why this carrier dominated the week's signal"
  },
  "burden_trend_states": [
    {"state": "CA|FL|TX|NY|LA|...", "direction": "increasing|decreasing|neutral", "note": "one sentence on what changed"}
  ],
  "inflation_pulse": "1-2 sentences on the week's loss-cost driver signals (parts, labor, severity, verdicts) and how they map to expected loss cost in the next 1-2 quarters"
}

Constraints:
  themes: 3-5 items.
  must_reads: exactly 5 items (pick the 5 highest-signal items across different topic areas).
  burden_trend_states: 1-3 items. Empty array if no clear state-level burden-trend movement this week.
  carrier_of_the_week: omit the key entirely if no carrier stood out this week.
  No markdown fences, no prose outside the JSON object."""


def _call_claude(prompt: str) -> str:
    full = f"{SYSTEM_PROMPT}\n\n{prompt}"
    cmd = ["claude", "-p", "--model", settings.summarizer_model, "--output-format", "json"]
    result = subprocess.run(
        cmd,
        input=full,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exit {result.returncode}: {result.stderr.strip()[:300]}"
        )
    try:
        envelope = json.loads(result.stdout)
        return envelope.get("result") or envelope.get("response") or result.stdout
    except json.JSONDecodeError:
        return result.stdout


def _parse_synthesis(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"(\{.*\})", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return {}


def synthesize_week(
    rows: list,
    week_label: str,
    regime_framing: str = "",
) -> dict[str, Any]:
    """Call Claude to produce a weekly P&C synthesis over the given item rows.

    Args:
        rows: sqlite3.Row list from db.items_for_week(), pre-sorted by leaderboard
              score (or triage_score as fallback).
        week_label: human label like "2026-W21 (May 18 – May 24)".
        regime_framing: optional regime + cat-load context injected at prompt top.

    Returns:
        Parsed synthesis dict with keys: themes, must_reads, contrarian_signal,
        carrier_of_the_week, burden_trend_states, inflation_pulse. Empty dict
        on failure (caller's renderer treats missing keys as "no section").
    """
    if not rows:
        return {}

    top = rows[:30]
    item_lines: list[str] = []
    for row in top:
        score_str = ""
        if "triage_score" in row.keys() and row["triage_score"] is not None:
            try:
                score_str = f" score={float(row['triage_score']):.2f}"
            except (TypeError, ValueError):
                pass
        item_lines.append(
            f"ID {row['id']} [{row['topic'] or 'other'}]{score_str}\n"
            f"  Title: {row['title']}\n"
            f"  Summary: {(row['summary'] or '')[:300]}\n"
            f"  Why: {(row['why_it_matters'] or '')[:150]}"
        )

    header = f"Week: {week_label}\n"
    if regime_framing:
        header += f"Regime context: {regime_framing}\n"
    header += f"Total items with summaries: {len(rows)} (showing top {len(top)} by score)\n"

    prompt = header + "\n" + "\n\n".join(item_lines)

    try:
        raw = _call_claude(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.error("weekly: Claude call failed: %s", exc)
        return {}

    synthesis = _parse_synthesis(raw)
    if not synthesis:
        logger.warning("weekly: unparseable synthesis response")
    return synthesis
