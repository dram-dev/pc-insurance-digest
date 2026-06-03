"""collision scraper — CCC (Webflow cards) + Mitchell (parent-<a>-wrapped rows).

Network-free: requests.get is stubbed with synthetic HTML in the live shapes
validated 2026-06-02.
"""
from __future__ import annotations

from digest.ingest import collision_data
from digest.ingest.collision_data import _scrape, COLLISION_TITLE_FILTERS


class _Resp:
    def __init__(self, html: str):
        self.text = html
    def raise_for_status(self):
        pass


# CCC: each post card directly wraps its /posts/ link; the featured card and the
# grid cards share that shape. A corporate-PR card ("Announces CFO") must be
# dropped by the keyword filter.
_CCC_HTML = """
<div class="news-list">
  <div class="featured"><a href="/news-and-insights/posts/ccc-crash-course-2026"><img></a>
    <h3>CCC Crash Course 2026 Report Finds Higher Severity</h3></div>
  <div class="news-card"><a href="/news-and-insights/posts/ccc-adds-adas"><img></a>
    <h3>CCC Expands ADAS Insights</h3></div>
  <div class="news-card"><a href="/news-and-insights/posts/ccc-cfo"><img></a>
    <h3>CCC Announces Chief Financial Officer</h3></div>
</div>
"""

# Mitchell: each .listing-item is wrapped in a parent <a> (the link is NOT a
# child of the card) — exercises the parent-anchor fallback in _extract_href.
_MITCHELL_HTML = """
<div class="news-listing">
  <a href="https://www.mitchell.com/insights/ev-claims-rise">
     <div class="listing-item"><h3>Electric Vehicle Collision Claims Rise 14%</h3>
       <time>February 19, 2026</time></div></a>
  <a href="https://www.mitchell.com/insights/cfo">
     <div class="listing-item"><h3>Enlyte Names New Chief Marketing Officer</h3>
       <time>March 1, 2026</time></div></a>
</div>
"""


def test_ccc_catches_featured_and_filters_corporate_pr(monkeypatch):
    monkeypatch.setattr(collision_data.requests, "get", lambda *a, **k: _Resp(_CCC_HTML))
    items = _scrape("ccc", "https://www.cccis.com/news-and-insights/news",
                    collision_data._CCC_SELECTORS, COLLISION_TITLE_FILTERS)
    titles = [i.title for i in items]
    # flagship (severity) + ADAS kept; the CFO corporate PR dropped.
    assert any("Crash Course 2026" in t for t in titles)
    assert any("ADAS" in t for t in titles)
    assert not any("Chief Financial Officer" in t for t in titles)
    assert items[0].url.endswith("/news-and-insights/posts/ccc-crash-course-2026")
    assert items[0].metadata["topic_hint"] == "supply_chain"


def test_mitchell_href_from_parent_anchor(monkeypatch):
    monkeypatch.setattr(collision_data.requests, "get", lambda *a, **k: _Resp(_MITCHELL_HTML))
    items = _scrape("mitchell", "https://www.mitchell.com/about/news",
                    collision_data._MITCHELL_SELECTORS, COLLISION_TITLE_FILTERS)
    assert len(items) == 1                              # CMO appointment filtered out
    it = items[0]
    assert it.title == "MITCHELL: Electric Vehicle Collision Claims Rise 14%"
    # href pulled from the wrapping <a>, not a child anchor.
    assert it.url == "https://www.mitchell.com/insights/ev-claims-rise"
    assert it.published_at is not None and it.published_at.month == 2


def test_no_nodes_returns_empty(monkeypatch):
    monkeypatch.setattr(collision_data.requests, "get", lambda *a, **k: _Resp("<div>nothing</div>"))
    assert _scrape("ccc", "https://x/", collision_data._CCC_SELECTORS, COLLISION_TITLE_FILTERS) == []
