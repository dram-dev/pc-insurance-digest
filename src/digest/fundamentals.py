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


def underwriting_ratios(insurer: str) -> dict:
    """Loss&LAE / expense / combined ratio with earned-premium VALIDATION.

    The earned-premium denominator is the thing to get right, so it's cross-checked
    against net WRITTEN premium — a sound EP has earned ≈ written for a stable book
    (`ep_to_wp` ≈ 1.0; `ep_validated` flags 0.85-1.15). Ratios:
      • loss&LAE ratio = incurred claims + ALAE / earned premium (LAE sits in the
        loss line per ASC 944 — not double-counted on the expense side);
      • expense ratio = (other underwriting + acquisition-cost amortization) /
        earned premium, computed ONLY when BOTH parts are tagged, so it isn't
        understated (and carries no LAE — that's in losses);
      • combined = loss&LAE + expense when both are present.
    Each is plausibility-gated → None when the consolidated loss line or the full
    expense isn't cleanly tagged (multi-line writers). For those, build the ratio
    from per-LOB loss/LAE/expense rolled up, or the carrier-reported EX-99.1 figure,
    via the combined-ratio-bridge skill. A combined ratio is NEVER backed out of a
    GAAP operating-profit line (that nets in investment income)."""
    tk = insurer.upper()
    prem = _consolidated(tk, "premiums_earned_net")
    written = _consolidated(tk, "premiums_written_net")
    losses = _consolidated(tk, "losses_and_lae_incurred")
    other_uw = _consolidated(tk, "underwriting_expense")
    dac = _consolidated(tk, "dac_amortization")

    ep_to_wp = round(prem / written, 3) if (prem and written) else None
    ep_validated = (0.85 <= ep_to_wp <= 1.15) if ep_to_wp is not None else None

    def gated(num, lo, hi):
        if not prem or not num:
            return None
        r = num / prem
        return round(r, 4) if lo <= r <= hi else None

    loss_lae_ratio = gated(losses, 0.15, 1.5)
    # Expense only when BOTH components are present — otherwise it understates.
    expense_ratio = gated((other_uw or 0) + (dac or 0), 0.05, 0.6) if (other_uw and dac) else None
    combined = (round(loss_lae_ratio + expense_ratio, 4)
                if loss_lae_ratio is not None and expense_ratio is not None else None)
    return {
        "insurer": tk, "earned_premium_musd": prem, "written_premium_musd": written,
        "ep_to_wp": ep_to_wp, "ep_validated": ep_validated,
        "losses_lae_musd": losses, "loss_lae_ratio": loss_lae_ratio,
        "other_underwriting_expense_musd": other_uw, "acquisition_cost_amort_musd": dac,
        "expense_ratio": expense_ratio, "combined_ratio": combined,
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
        "underwriting": underwriting_ratios(tk),
        "triangles_by_canonical_lob": triangles,
        "reserve_development": reserving,
    }
