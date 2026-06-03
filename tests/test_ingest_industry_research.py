"""industry_research scraper — render branch + title_selector override + filter.

Network-free: `digest.ingest.render.fetch_rendered` is monkeypatched with canned
HTML, so no browser is launched.
"""
from __future__ import annotations

from digest.ingest.industry_research import _scrape_source

# LexisNexis Algolia card shape (validated live 2026-06-02). One auto-insurance
# card (passes the "auto insurance" filter) + one identity card (filtered out).
_LN_HTML = """
<div class="masonry-resource-listing-item">
  <div class="masonry-resource-listing-item-title">2026 U.S. Auto Insurance Study Results</div>
  <a class="masonry-resource-listing-item-link" href="/insights-resources/article/auto-study">Read</a>
</div>
<div class="masonry-resource-listing-item">
  <div class="masonry-resource-listing-item-title">The Role of Synthetic Data in Identity</div>
  <a class="masonry-resource-listing-item-link" href="/insights-resources/article/synthetic">Read</a>
</div>
"""

_LN_ENTRY = {
    "name": "lexisnexis_risk", "vendor": "LexisNexis Risk Solutions",
    "url": "https://risk.lexisnexis.com/insights-resources",
    "title_filter": "auto insurance",
    "selector": ".masonry-resource-listing-item",
    "title_selector": ".masonry-resource-listing-item-title",
    "render": True,
}


def test_render_branch_title_selector_and_filter(monkeypatch):
    monkeypatch.setattr("digest.ingest.render.fetch_rendered", lambda url, **k: _LN_HTML)
    items = _scrape_source(_LN_ENTRY)
    # title_selector read the card title div (NOT the "Read" anchor), and the
    # "auto insurance" filter dropped the identity card.
    assert len(items) == 1
    it = items[0]
    assert it.title == "[LexisNexis Risk Solutions] 2026 U.S. Auto Insurance Study Results"
    assert it.url == "https://risk.lexisnexis.com/insights-resources/article/auto-study"
    assert it.metadata["topic_hint"] == "personal_lines"


def test_render_unavailable_skips_source(monkeypatch):
    # fetch_rendered returns None (render extra not installed) → source skipped.
    monkeypatch.setattr("digest.ingest.render.fetch_rendered", lambda url, **k: None)
    assert _scrape_source(_LN_ENTRY) == []


def test_default_title_selector_still_used_without_override(monkeypatch):
    # A plain (non-render) source with no title_selector falls back to the
    # heading/anchor default.
    html = '<article><h3>Auto insurance prices climb</h3><a href="/x">link</a></article>'
    monkeypatch.setattr(
        "digest.ingest.industry_research.requests.get",
        lambda *a, **k: type("R", (), {"text": html, "raise_for_status": lambda self: None})(),
    )
    entry = {"name": "n", "vendor": "V", "url": "https://v.test/", "selector": "article",
             "title_filter": "auto insurance"}
    items = _scrape_source(entry)
    assert len(items) == 1
    assert items[0].title == "[V] Auto insurance prices climb"
