"""Canonical line-of-business mapping — unify the messy, source-specific LOB
strings (SEC-XBRL member slugs, NAIC Schedule P lines, III line names) onto one
comparison taxonomy, so a cross-insurer rollup (State Farm homeowners vs Allstate
home_owners vs CINF personal_lines_insurance_homeowner) actually lines up.

`canonicalize_lob(raw)` returns one of CANONICAL_LOBS. Strategy: an exact-string
override map (for the handful the heuristics get wrong) over an ordered set of
keyword rules (first match wins, most distinctive lines first). Pure / testable.
"""
from __future__ import annotations

import re

# The canonical comparison taxonomy. Aggregates (personal_lines / commercial_lines)
# catch segment-level rows that can't be split to a single line; `other` is the
# explicit fallback so an unmapped string is visible, not silently miscategorized.
CANONICAL_LOBS = (
    "personal_auto", "homeowners", "personal_lines",
    "commercial_auto", "commercial_property", "commercial_multi_peril",
    "general_liability", "workers_comp", "professional_liability",
    "medical_malpractice", "specialty", "umbrella", "reinsurance",
    "commercial_lines", "other",
)

# Exact raw-string → canonical overrides (win over the heuristics).
_OVERRIDES: dict[str, str] = {
    "group_policies": "other",
    # Markel's "Insurance EXCLUDING global reinsurance" is the primary/specialty
    # segment, not reinsurance — the keyword rule would otherwise mis-hit it.
    "markel_insurance_excluding_global_reinsurance_division": "commercial_lines",
}

# Ordered keyword rules — first match wins. Order matters: workers_comp before
# casualty; reinsurance/assumed before the line it reinsures; commercial/business
# auto before bare auto; non_casualty before casualty.
_RULES: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pat), lob) for pat, lob in [
        (r"reinsur|assumed", "reinsurance"),
        (r"workers?_?comp", "workers_comp"),
        (r"medical_?prof|malpractice", "medical_malpractice"),
        (r"financial_?lines|professional|management_?liab|\bd_?o\b|\be_?o\b|errors", "professional_liability"),
        (r"bond|surety|fidelity|marine|aviation|inland", "specialty"),
        (r"excess_?(and_?)?surplus|\be_?s_?lines\b|surplus_?lines", "specialty"),
        (r"umbrella", "umbrella"),
        (r"non_?casualty", "commercial_property"),                 # "non-casualty" = property/short-tail
        (r"(commercial|business).*(auto|vehicle|automobile)", "commercial_auto"),
        (r"(multi_?peril|package|\bcmp\b)", "commercial_multi_peril"),
        (r"commercial.*propert", "commercial_property"),
        (r"general_?liab|other_?(liab|casualty)|excess_?casualty|\bgl\b", "general_liability"),
        (r"home_?owner|farmowner", "homeowners"),
        (r"(personal|private_?passenger).*(auto|vehicle)|geico|"
         r"(auto|vehicle|automobile).*(liab|physical_?damage)|physical_?damage|collision", "personal_auto"),
        (r"auto|vehicle|automobile", "personal_auto"),             # bare auto → personal (most common)
        (r"personal.*(insurance|lines|\bpc\b|property)", "personal_lines"),
        (r"propert|special_?risk", "commercial_property"),         # remaining property → commercial
        # Underscores are word chars, so plain substrings (not \b…\b) match
        # tokens like 'general_casualty' / 'monoline_excess'.
        (r"casualty|excess", "general_liability"),                 # remaining casualty / excess liability → GL
        (r"(commercial|business).*(insurance|lines|\bpc\b)", "commercial_lines"),
    ]
)


def canonicalize_lob(raw: str | None) -> str:
    """Map a raw LOB slug onto CANONICAL_LOBS (override map, then keyword rules)."""
    if not raw:
        return "other"
    key = raw.strip().lower()
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    for pattern, lob in _RULES:
        if pattern.search(key):
            return lob
    return "other"
