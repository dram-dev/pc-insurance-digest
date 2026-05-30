"""InsurTech Capital-Flow (EKG Lead 8) — deal extraction → relax the share cap.

The `ai_insurtech` topic is governed by a flat 35% per-run share cap
(`summarize.TOPIC_CAP_PCT`) because its broad-keyword feeds pull in a lot of
shallow AI/SaaS PR. That cap is blunt: it drops *substantive* funding/M&A items
alongside the noise. This lead extracts structured deal facts (amount, round
type, stage) from the queue's ai_insurtech items, and lets a **substantiated**
deal (one with a real dollar amount) bypass the cap — so the cap keeps culling
keyword chaff while real capital events survive:

    ai_insurtech queue rows
      → extract_deal(text)  → {deal_type, amount_usd, stage, …} | None
      → db.upsert_capital_flow()  (local mirror of pc_silver.capital_flows)
      → run_capital_flows() returns the protected item ids
      → enforce_topic_caps_protected()  (substantiated deals excluded from the cap)

Behavior-preserving: with no substantiated deals the protected set is empty and
the cap behaves exactly as before (delegates straight to the core
`enforce_topic_caps`).

Databricks-native upgrade: Vector Search + `ai_query()` for richer deal
extraction (target, investor list) → silver.capital_flows; this deterministic
regex extractor is the Free-Edition default and powers the cap-relaxation today.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from digest import db
from digest_core.summarize.runner import enforce_topic_caps

logger = logging.getLogger(__name__)

# "$120 million", "$1.5B", "$900M", "raised $40m"
_AMOUNT_RE = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s?(billion|bn|b|million|mm|m)\b", re.IGNORECASE
)
_MULT = {"billion": 1e9, "bn": 1e9, "b": 1e9,
         "million": 1e6, "mm": 1e6, "m": 1e6}

_STAGE_RE = re.compile(
    r"\b(seed|series\s+[a-f]|pre-seed|growth round|series\s?[a-f])\b", re.IGNORECASE
)
_FUNDING_RE = re.compile(
    r"\b(raise[sd]?|funding|round|venture|backed|investment)\b", re.IGNORECASE
)
_MA_RE = re.compile(
    r"\b(acquir\w+|acquisition|merger|merge[sd]?|buys?|to buy|takeover)\b", re.IGNORECASE
)
_IPO_RE = re.compile(r"\b(ipo|initial public offering|goes public|public listing)\b", re.IGNORECASE)


def _amount_usd(text: str) -> float | None:
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    return round(float(m.group(1)) * _MULT[m.group(2).lower()], 2)


def _stage(text: str) -> str | None:
    m = _STAGE_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", "_", m.group(1).strip().lower())


def extract_deal(text: str) -> dict | None:
    """Extract a deal from item text, or None if it isn't a deal.

    A deal is 'substantiated' (and so cap-protected) iff `amount_usd` is not
    None — a named dollar figure is the substance gate that separates a real
    capital event from generic insurtech PR.
    """
    if not text:
        return None
    amount = _amount_usd(text)
    if _MA_RE.search(text):
        deal_type = "m&a"
    elif _IPO_RE.search(text):
        deal_type = "ipo"
    elif _FUNDING_RE.search(text) or amount is not None:
        deal_type = "funding_round"
    else:
        return None
    return {"deal_type": deal_type, "amount_usd": amount,
            "stage": _stage(text), "target": None, "investors": None}


def run_capital_flows(rows: list) -> set[int]:
    """Extract + persist deals from the ai_insurtech queue rows. Returns the set
    of item ids with a *substantiated* deal (real amount) — the cap-protected set."""
    protected: set[int] = set()
    now = datetime.now(tz=timezone.utc).isoformat()
    for r in rows:
        if (r["topic"] or "").lower() != "ai_insurtech":
            continue
        text = f"{r['title'] or ''}. {r['content'] or ''}"
        deal = extract_deal(text)
        if deal is None:
            continue
        deal["as_of"] = now
        db.upsert_capital_flow(int(r["id"]), r["source"], r["source_id"], deal)
        if deal["amount_usd"] is not None:
            protected.add(int(r["id"]))
    if protected:
        logger.info("capital_flows: %d substantiated ai_insurtech deals protected from cap",
                    len(protected))
    return protected


def enforce_topic_caps_protected(rows: list, caps: dict, protected_ids: set[int]):
    """`enforce_topic_caps`, but items in `protected_ids` are excluded from the
    cap math and always kept (a substantiated deal doesn't count against the
    ai_insurtech share). Delegates to the core helper when nothing is protected."""
    if not protected_ids:
        return enforce_topic_caps(rows, caps)
    rest = [r for r in rows if int(r["id"]) not in protected_ids]
    kept_rest, dropped = enforce_topic_caps(rest, caps)
    kept_ids = {int(r["id"]) for r in kept_rest} | protected_ids
    return [r for r in rows if int(r["id"]) in kept_ids], dropped
