"""Insurer-fundamentals accessors over the XBRL concept-registry + statutory feeds.

Exposes the ingested datasets (insurer_xbrl_facts, statutory_facts, loss_triangles)
in analysis-ready shapes so the actuarial skills and the Analyst MCP server can
consume them without re-deriving the SQL each time. Read-only.

Dataset → consumer:
  premiums           → ratemaking / BF premium a-priori (combined-ratio-bridge base)
  claim_counts       → FREQUENCY for severity-trend freq×severity decomposition
  combined_ratio     → combined-ratio-bridge inputs (earned premium, losses+LAE)
  ibnr / triangle    → reserving chain-ladder + reserve_deterioration_boost
  reserve_development → prior-year development (favorable/adverse)
  investment_income  → rates_cost_of_capital
  statutory (III)    → the big mutuals' DPW + market share (State Farm et al.)
"""
from __future__ import annotations

from digest import db


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def earned_premium_by_segment(insurer: str) -> list[dict]:
    """Net earned premium by segment × fiscal year ($M) — the combined-ratio /
    BF premium base. Calendar-year earned ≈ accident-year earned for a steady book."""
    return _rows(
        """SELECT segment, period_end, value AS earned_premium_musd
           FROM insurer_xbrl_facts
           WHERE insurer=? AND dataset='premiums' AND field='premiums_earned_net'
           ORDER BY period_end DESC, segment""",
        (insurer.upper(),),
    )


def claim_counts_by_ay(insurer: str) -> list[dict]:
    """Reported claim counts by accident year × product — the FREQUENCY series
    severity-trend needs to split pure-premium trend into frequency × severity."""
    return _rows(
        """SELECT product, accident_year, value AS reported_claims
           FROM insurer_xbrl_facts
           WHERE insurer=? AND dataset='claim_counts'
           ORDER BY product, accident_year""",
        (insurer.upper(),),
    )


def combined_ratio_components(insurer: str) -> list[dict]:
    """Latest-period earned premium + incurred losses & LAE by segment — the
    raw inputs the combined-ratio-bridge decomposes (loss / LAE / expense)."""
    return _rows(
        """SELECT dataset, field, segment, period_end, value AS musd
           FROM insurer_xbrl_facts
           WHERE insurer=? AND dataset IN ('combined_ratio','premiums')
             AND field IN ('losses_and_lae_incurred','premiums_earned_net')
             AND period_end=as_of
           ORDER BY field, segment""",
        (insurer.upper(),),
    )


def statutory_top_writers(canonical_lob: str) -> list[dict]:
    """Top writers' direct premiums written + market share for a canonical line
    (free III feed) — including the mutuals absent from SEC XBRL (State Farm …)."""
    return _rows(
        """SELECT p.insurer, p.value AS dpw_musd, s.value AS market_share_pct, p.period
           FROM statutory_facts p
           LEFT JOIN statutory_facts s
             ON s.insurer=p.insurer AND s.dataset='market_share'
                AND s.canonical_lob=p.canonical_lob AND s.period=p.period
           WHERE p.dataset='premiums' AND p.canonical_lob=?
           ORDER BY p.value DESC""",
        (canonical_lob,),
    )


_CONSOLIDATED = """
    SELECT value FROM insurer_xbrl_facts
    WHERE insurer=? AND field=? AND period_type='duration' AND period_end=as_of
      AND segment IS NULL AND product IS NULL AND subsegment IS NULL
      AND geography IS NULL AND investment_type IS NULL AND instrument IS NULL
      AND fv_level IS NULL
    ORDER BY ABS(value) DESC LIMIT 1"""


def _consolidated(insurer: str, field: str) -> float | None:
    """Latest-period consolidated (un-dimensioned) value for a field. Largest
    magnitude wins — the group total dominates a 0-valued or single-line sibling
    fact that shares the same null-dimension context (e.g. TRV's losses)."""
    rows = _rows(_CONSOLIDATED, (insurer.upper(), field))
    return rows[0]["value"] if rows else None


def loss_ratio(insurer: str) -> dict:
    """Consolidated loss & LAE ratio from the XBRL components — NOT a combined ratio.

    The loss&LAE ratio is one clean line (incurred claims + ALAE, ASC 944) over
    earned premium, so it's sound and not double-counted; it's plausibility-gated
    and returns None where the consolidated incurred-loss line isn't cleanly tagged
    (multi-line writers bury P&C losses in a broader total).

    A COMBINED ratio is deliberately NOT synthesized here. Validation showed the
    structured XBRL doesn't carry the proper component lines (underwriting expense
    is tagged for only a few filers, and inconsistently), so summing what's present
    understates the combined ratio by 10-20 points — and backing into it from a
    GAAP operating-profit line is unsound (it nets in investment income). A correct
    combined ratio is built from per-LOB loss/LAE/expense components rolled up at a
    meaningful level; feed those (or the carrier-reported figure from the EX-99.1
    earnings release) into the combined-ratio-bridge skill. The raw expense
    components below are surfaced for transparency only — not summed into a ratio."""
    tk = insurer.upper()
    prem = _consolidated(tk, "premiums_earned_net")
    losses = _consolidated(tk, "losses_and_lae_incurred")

    lr = None
    if prem and losses and 0.15 <= losses / prem <= 1.5:
        lr = round(losses / prem, 4)
    return {
        "insurer": tk, "earned_premium_musd": prem,
        "losses_lae_musd": losses, "loss_lae_ratio": lr,
        # raw, for transparency — NOT a complete expense ratio (see docstring).
        "other_underwriting_expense_musd": _consolidated(tk, "underwriting_expense"),
        "acquisition_cost_amort_musd": _consolidated(tk, "dac_amortization"),
        "combined_ratio": None,  # not soundly reconstructable from these facts
    }


def insurer_fundamentals(insurer: str) -> dict:
    """Cross-dataset headline summary for one insurer — the MCP `fundamentals`
    tool payload. Per dataset: fact count + a headline figure."""
    tk = insurer.upper()
    datasets = _rows(
        """SELECT dataset, COUNT(*) AS facts, COUNT(DISTINCT field) AS fields,
                  MAX(as_of) AS as_of
           FROM insurer_xbrl_facts WHERE insurer=? GROUP BY dataset ORDER BY facts DESC""",
        (tk,),
    )
    premium = _rows(
        """SELECT SUM(value) AS total_earned_musd FROM insurer_xbrl_facts
           WHERE insurer=? AND dataset='premiums' AND field='premiums_earned_net'
             AND segment IS NULL AND period_end=as_of""",
        (tk,),
    )
    triangles = _rows(
        """SELECT canonical_lob, COUNT(DISTINCT lob) AS raw_lines,
                  COUNT(*) AS cells FROM loss_triangles WHERE insurer=?
           GROUP BY canonical_lob ORDER BY cells DESC""",
        (tk,),
    )
    reserving = _rows(
        """SELECT canonical_lob, metric, ibnr, deterioration_pct, direction
           FROM reserving_signals r
           JOIN (SELECT DISTINCT lob, canonical_lob FROM loss_triangles) m ON m.lob=r.lob
           WHERE r.insurer=? AND r.deterioration_pct IS NOT NULL
           ORDER BY r.deterioration_pct DESC LIMIT 8""",
        (tk,),
    )
    return {
        "insurer": tk,
        "datasets": datasets,
        "total_earned_premium_musd": (premium[0]["total_earned_musd"] if premium else None),
        "loss_ratio": loss_ratio(tk),
        "triangles_by_canonical_lob": triangles,
        "reserve_development": reserving,
    }
