"""state_doi scraper — selector + per-state date_selector extraction.

Network-free: requests.get is stubbed with synthetic HTML in the CA (custom
control + span.secondaryHeader date) and NY (Drupal card + <time datetime>) shapes
validated live on 2026-05-30.
"""
from __future__ import annotations

import pytest

from digest.ingest.state_doi import StateDOIIngestor


@pytest.fixture
def ingestor() -> StateDOIIngestor:
    return StateDOIIngestor()


class _Resp:
    def __init__(self, html: str):
        self.text = html
    def raise_for_status(self):
        pass


_CA_HTML = """
<div class="cs_control CS_Element_Custom">
  <p><span class="secondaryHeader">May 27, 2026</span><br>
     <a href="/0400-news/0100-press-releases/2026/release020-2026.cfm">Staged-crash arraignment</a></p>
  <p><a href="/0400-news/view-by-subject.cfm">View By Subject</a></p>
</div>
"""

_NY_HTML = """
<div class="listing-view__card-content">
  <h3 class="listing-view__card-headline"><span class="field"><a href="/reports_and_publications/press_releases/pr2026052802">FY27 budget statement</a></span></h3>
  <div class="listing-view__card-nothing"><time class="datetime" datetime="2026-05-28T13:42:01-04:00">May 28, 2026</time></div>
</div>
"""


def test_ca_selector_and_date_selector(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get", lambda *a, **k: _Resp(_CA_HTML))
    entry = {
        "selector": ".cs_control.CS_Element_Custom p:has(span.secondaryHeader):has(a[href*='.cfm'])",
        "date_selector": ".secondaryHeader",
    }
    items = ingestor._scrape_state("CA", "California DOI", "https://www.insurance.ca.gov/x/", entry)
    # The ':has(span.secondaryHeader)' requirement drops the dateless "View By Subject".
    assert len(items) == 1
    it = items[0]
    assert it.title == "[CA DOI] Staged-crash arraignment"
    assert it.url.endswith("release020-2026.cfm")
    assert it.published_at is not None and it.published_at.year == 2026
    assert it.metadata == {"topic_hint": "regulatory_rate", "state": "CA", "agency": "California DOI"}


def test_ny_card_with_time_datetime(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get", lambda *a, **k: _Resp(_NY_HTML))
    entry = {"selector": ".listing-view__card-content"}   # generic <time> selector catches the date
    items = ingestor._scrape_state("NY", "NY DFS", "https://www.dfs.ny.gov/x", entry)
    assert len(items) == 1
    assert items[0].title == "[NY DOI] FY27 budget statement"
    assert items[0].published_at is not None and items[0].published_at.month == 5


def test_missing_selector_returns_nothing(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get", lambda *a, **k: _Resp("<div>no matches</div>"))
    assert ingestor._scrape_state("CA", "x", "https://x/", {"selector": ".nope"}) == []
