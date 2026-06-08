"""Component-level insurance facts from a 10-K's XBRL instance — concept registry.

Generalizes the loss-triangle extractor (digest.parse.edgar_triangles) into a
registry that pulls EVERY standardized P&C disclosure out of ONE instance fetch.
The instance (`<ticker>-<date>_htm.xml`) carries hundreds of us-gaap facts, each
bound to a context whose dimensions (segment / product / subsegment / accident
year / geography / investment-type …) give the component breakdown. We map the
insurance-relevant concepts to the 13 review datasets and emit one row per
(concept × dimensional context) — the unique component-level fact.

    extract_facts(instance_xml, insurer) -> [ {insurer, dataset, concept, field,
        period_end, period_type, accident_year, segment, product, subsegment,
        geography, investment_type, instrument, fv_level, value, is_count,
        as_of, fact_key}, … ]

Values are normalized to USD millions (counts kept raw). fact_key makes the
upsert idempotent. triangle_cells_from_facts() reshapes the incurred/paid facts
into loss_triangles cells so the existing reserving chain still feeds.
"""
from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

# ── concept registry: us-gaap localname → (dataset, field) ──────────────────
# Datasets map to the 13 reviewed items; geography (#12) is a DIMENSION cut of
# the premium/loss facts, not its own concept, so it rides the `geography` column.
_CONCEPTS: dict[str, tuple[str, str]] = {
    # (1) combined-ratio components + (13) segment results
    "PolicyholderBenefitsAndClaimsIncurredNet": ("combined_ratio", "losses_and_lae_incurred"),
    "IncurredClaimsPropertyCasualtyAndLiability": ("combined_ratio", "losses_incurred_pc"),
    "OtherUnderwritingExpense": ("combined_ratio", "underwriting_expense"),
    "NetIncomeLoss": ("segment_results", "net_income"),
    "Revenues": ("segment_results", "revenues"),
    "Assets": ("segment_results", "assets"),
    # (2) premiums written / earned by LOB
    "PremiumsEarnedNet": ("premiums", "premiums_earned_net"),
    "PremiumsWrittenNet": ("premiums", "premiums_written_net"),
    "PremiumsEarnedNetPropertyAndCasualty": ("premiums", "premiums_earned_net_pc"),
    "SupplementaryInsuranceInformationPremiumRevenue": ("premiums", "premium_revenue"),
    "SupplementaryInsuranceInformationUnearnedPremiums": ("premiums", "unearned_premiums"),
    # (3) claim counts by accident year  → FREQUENCY (kept raw, not $M)
    "ShortdurationInsuranceContractsNumberOfReportedClaims": ("claim_counts", "reported_claims"),
    # (4) IBNR by accident year / LOB
    "ShortdurationInsuranceContractsIncurredButNotReportedIbnrClaimsLiabilityNet": ("ibnr", "ibnr_net"),
    # (5) prior-year reserve development
    "SupplementalInformationForPropertyCasualtyInsuranceUnderwritersPriorYearClaimsAndClaimsAdjustmentExpense":
        ("reserve_development", "prior_year_development"),
    # (6) unpaid-claims liability (rollforward endpoints, net & gross)
    "LiabilityForClaimsAndClaimsAdjustmentExpense": ("unpaid_claims", "liability_gross"),
    "LiabilityForUnpaidClaimsAndClaimsAdjustmentExpenseNet": ("unpaid_claims", "liability_net"),
    "ShortdurationInsuranceContractsLiabilityForUnpaidClaimsAndAllocatedClaimAdjustmentExpenseNet":
        ("unpaid_claims", "unpaid_acae_net"),
    # (7) reinsurance recoverables + ceded
    "ReinsuranceRecoverableForUnpaidClaimsAndClaimsAdjustments": ("reinsurance", "recoverable_unpaid"),
    "PremiumsCeded": ("reinsurance", "premiums_ceded"),
    "CededPremiumsEarned": ("reinsurance", "ceded_premiums_earned"),
    # (8) net investment income
    "NetInvestmentIncome": ("investment_income", "net_investment_income"),
    "GrossInvestmentIncomeOperating": ("investment_income", "gross_investment_income"),
    # (9) investment portfolio composition
    "InvestmentsFairValueDisclosure": ("investment_portfolio", "investments_fair_value"),
    "AvailableForSaleSecuritiesDebtSecurities": ("investment_portfolio", "afs_debt_securities"),
    # (10) realized gains + AOCI
    "RealizedInvestmentGainsLosses": ("investment_gains", "realized_gains_losses"),
    "OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent": ("aoci", "oci_net"),
    # (11) deferred policy acquisition costs
    "DeferredPolicyAcquisitionCostAmortizationExpense": ("dac", "dac_amortization"),
    "SupplementaryInsuranceInformationDeferredPolicyAcquisitionCosts": ("dac", "dac_balance"),
    # triangle (incurred/paid) — also reshaped into loss_triangles for reserving
    "ShortdurationInsuranceContractsIncurredClaimsAndAllocatedClaimAdjustmentExpenseNet": ("triangle", "incurred"),
    "ShortdurationInsuranceContractsCumulativePaidClaimsAndAllocatedClaimAdjustmentExpenseNet": ("triangle", "paid"),
}

# Concepts reported as counts, not USD → never scale to millions.
_COUNT_CONCEPTS = {"ShortdurationInsuranceContractsNumberOfReportedClaims"}

# Dimensional axis localname → fact column carrying its member.
_AXES: dict[str, str] = {
    "ShortdurationInsuranceContractsAccidentYearAxis": "accident_year",
    "StatementBusinessSegmentsAxis": "segment",
    "ProductOrServiceAxis": "product",
    "SubsegmentsAxis": "subsegment",
    "StatementGeographicalAxis": "geography",
    "InvestmentTypeAxis": "investment_type",
    "InvestmentPortfolioAxis": "investment_type",
    "FinancialInstrumentAxis": "instrument",
    "InformationByCategoryOfDebtSecurityAxis": "instrument",
    "FairValueByFairValueHierarchyLevelAxis": "fv_level",
}

_DIM_COLUMNS = ("accident_year", "segment", "product", "subsegment", "geography",
                "investment_type", "instrument", "fv_level")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _humanize(member: str) -> str:
    """'ns:PropertyAndCasualtyLiabilityMember' → 'property_and_casualty_liability'."""
    base = re.sub(r"Member$", "", member.rsplit(":", 1)[-1])
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", base)
    return re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")


def _parse_contexts(root) -> dict[str, tuple[str | None, str, dict[str, str]]]:
    """context id → (period_end_date, 'instant'|'duration', {axis_localname: member})."""
    out: dict[str, tuple[str | None, str, dict[str, str]]] = {}
    for node in root:
        if _localname(node.tag) != "context":
            continue
        period_end: str | None = None
        period_type = "duration"
        dims: dict[str, str] = {}
        for el in node.iter():
            ln = _localname(el.tag)
            if ln == "instant":
                period_end, period_type = (el.text or "").strip(), "instant"
            elif ln == "endDate":
                period_end = (el.text or "").strip()
            elif ln == "explicitMember":
                ax = el.get("dimension", "").rsplit(":", 1)[-1]
                dims[ax] = (el.text or "").strip()
        out[node.get("id", "")] = (period_end, period_type, dims)
    return out


def _fact_key(f: dict) -> str:
    dims = "|".join(f"{c}={f.get(c)}" for c in _DIM_COLUMNS)
    s = f"{f['insurer']}::{f['concept']}::{f['period_end']}::{f['period_type']}::{dims}"
    return hashlib.sha256(s.encode()).hexdigest()[:24]


def extract_facts(instance_xml: str, *, insurer: str) -> list[dict]:
    """Component-level facts for the registry concepts, one row per dimensional
    context. Monetary values → USD millions; claim counts kept raw."""
    root = ET.fromstring(instance_xml)
    ctx = _parse_contexts(root)

    facts: list[dict] = []
    period_ends: list[str] = []
    for el in root:
        spec = _CONCEPTS.get(_localname(el.tag))
        if not spec:
            continue
        cref = el.get("contextRef")
        if cref not in ctx:
            continue
        text = (el.text or "").strip()
        if not text:
            continue
        try:
            raw = float(text)
        except ValueError:
            continue
        concept = _localname(el.tag)
        period_end, period_type, dims = ctx[cref]
        is_count = concept in _COUNT_CONCEPTS
        row: dict = {
            "insurer": insurer, "dataset": spec[0], "concept": concept,
            "field": spec[1], "period_end": period_end, "period_type": period_type,
            "value": round(raw if is_count else raw / 1_000_000.0, 4),
            "is_count": int(is_count),
        }
        for col in _DIM_COLUMNS:
            row[col] = None
        for axis, col in _AXES.items():
            member = dims.get(axis)
            if member is None:
                continue
            if col == "accident_year":
                m = _YEAR_RE.search(member)
                row["accident_year"] = int(m.group()) if m else None
            else:
                row[col] = _humanize(member)
        if period_end:
            period_ends.append(period_end)
        facts.append(row)

    as_of = max(period_ends) if period_ends else None
    for f in facts:
        f["as_of"] = as_of
        f["fact_key"] = _fact_key(f)
    logger.info("xbrl_facts: %s — %d component facts across %d datasets, as_of=%s",
                insurer, len(facts), len({f["dataset"] for f in facts}), as_of)
    return facts


def triangle_cells_from_facts(facts: list[dict]) -> list[dict]:
    """Reshape the incurred/paid triangle facts into loss_triangles cell dicts
    (lob from segment/product/subsegment, dev = months since accident year)."""
    cells: list[dict] = []
    for f in facts:
        if f["dataset"] != "triangle" or f["accident_year"] is None or f["period_end"] is None:
            continue
        m = _YEAR_RE.search(f["period_end"])
        if not m:
            continue
        val_year = int(m.group())
        ay = f["accident_year"]
        if val_year < ay:
            continue
        lob = "_".join(p for p in (f.get("segment"), f.get("product"),
                                   f.get("subsegment")) if p) or "all_lines"
        cells.append({
            "insurer": f["insurer"], "lob": lob, "metric": f["field"],
            "accident_year": ay, "dev_period": (val_year - ay + 1) * 12,
            "cumulative_value": f["value"], "as_of": f["as_of"],
        })
    return cells
