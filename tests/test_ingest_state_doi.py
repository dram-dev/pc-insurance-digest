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
    def __init__(self, html: str, payload: dict | None = None):
        self.text = html
        self._payload = payload
    def json(self):
        return self._payload
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

# FL FLOIR (floir.gov, validated live 2026-06-02): headline in span.newsSummary.h-3,
# the only anchor is a "Full story" button, date only in the item-details URL path.
# The second <li> has no item-details anchor → the :has() filter must drop it.
_FL_HTML = """
<ul class="list-unstyled">
  <li><div class="newsSummary">
    <span class="newsSummary h-3">Commissioner Approves More Auto Rate Cuts for Consumers</span>
    <div class="col-xs-12 col-3 px-0">
      <a class="btn newsSummary btn-primary"
         href="http://floir.gov/newsroom/archives/item-details/2026/01/28/commissioner-approves-auto-rate-cuts">Full story</a>
    </div>
  </div></li>
  <li><a href="/newsroom/subscribe">Subscribe to updates</a></li>
</ul>
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


def test_fl_title_selector_and_url_path_date(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get", lambda *a, **k: _Resp(_FL_HTML))
    entry = {
        "selector": "ul.list-unstyled li:has(a[href*='item-details'])",
        "title_selector": "span.newsSummary.h-3",
    }
    items = ingestor._scrape_state("FL", "Florida OIR", "https://floir.gov/newsroom", entry)
    # The :has(item-details) filter drops the "Subscribe" li.
    assert len(items) == 1
    it = items[0]
    # title_selector picked the headline span, NOT the "Full story" anchor text.
    assert it.title == "[FL DOI] Commissioner Approves More Auto Rate Cuts for Consumers"
    assert it.url.endswith("commissioner-approves-auto-rate-cuts")
    # date came from the /2026/01/28/ URL path (no date element on the card).
    assert it.published_at is not None
    assert (it.published_at.year, it.published_at.month, it.published_at.day) == (2026, 1, 28)


def test_missing_selector_returns_nothing(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get", lambda *a, **k: _Resp("<div>no matches</div>"))
    assert ingestor._scrape_state("CA", "x", "https://x/", {"selector": ".nope"}) == []


# TX year-index shape (validated live 2026-06-02): <h2 class="news-list-title">
# wrapping the article anchor with a relative href.
_TX_HTML = """
<h2 class="news-list-title"><a href="tdi05282026.html">Insurance tips for hurricane season</a></h2>
<h2 class="news-list-title"><a href="tdi04162026.html">Insurance tips for spring storms</a></h2>
"""


def test_render_branch_routes_through_fetch_rendered(monkeypatch, ingestor):
    # render:true must use the headless-fetch path, not requests.get.
    monkeypatch.setattr("digest.ingest.render.fetch_rendered", lambda url, **k: _TX_HTML)
    monkeypatch.setattr("digest.ingest.state_doi.requests.get",
                        lambda *a, **k: pytest.fail("requests.get used despite render:true"))
    entry = {"selector": ".news-list-title", "render": True}
    items = ingestor._scrape_state("TX", "Texas DOI",
                                   "https://www.tdi.texas.gov/news/2026/index.html", entry)
    assert len(items) == 2
    assert items[0].title == "[TX DOI] Insurance tips for hurricane season"
    assert items[0].url.endswith("/news/2026/tdi05282026.html")


def test_render_unavailable_skips_state(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.render.fetch_rendered", lambda url, **k: None)
    entry = {"selector": ".news-list-title", "render": True}
    assert ingestor._scrape_state("TX", "x", "https://x/", entry) == []


# LA LDI shape (validated live 2026-06-02): each release is a <p> in .sfContentBlock
# whose own text is the date-prefixed headline; its only anchor is the date link.
# A trailing "Subscribe" <p> (link to /subscriptions/) must not match.
_LA_HTML = """
<div class="sfContentBlock">
  <p><a href="/news/press-releases/6-1-26-media-advisory">June 1, 2026</a> - MEDIA ADVISORY - Commissioner acts</p>
  <p><a href="/news/press-releases/5-27-26-fortify">May 27, 2026</a> - Fortify Homes lottery goes live</p>
  <p><a href="/news/press-releases/5-13-26-arrests">May 13, 2026</a> - LDI referral leads to arrests</p>
  <p>Sign up to receive LDI press releases <a href="/subscriptions/x">Subscribe</a></p>
</div>
"""


def test_la_title_from_node_date_selector_and_max_items(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.render.fetch_rendered", lambda url, **k: _LA_HTML)
    entry = {
        "selector": ".sfContentBlock p:has(a[href*='/news/press-releases/'])",
        "title_from_node": True,
        "date_selector": "a[href*='/news/press-releases/']",
        "max_items": 2,
        "render": True,
    }
    items = ingestor._scrape_state("LA", "Louisiana DOI",
                                   "https://www.ldi.la.gov/news/press-releases", entry)
    # max_items=2 keeps the two newest; the Subscribe <p> never matched (no release link).
    assert len(items) == 2
    # title_from_node → the <p>'s own text (date-prefixed headline), not the date link alone.
    assert items[0].title == "[LA DOI] June 1, 2026 - MEDIA ADVISORY - Commissioner acts"
    # date_selector read the link text.
    assert items[0].published_at is not None
    assert (items[0].published_at.year, items[0].published_at.month, items[0].published_at.day) == (2026, 6, 1)
    assert items[1].title.startswith("[LA DOI] May 27, 2026")
    assert items[0].url.endswith("/news/press-releases/6-1-26-media-advisory")


# IL IDOI shape (validated live 2026-06-03): a Sling model JSON whose
# newsFeedItemList carries title/type/date/year/description/url. The feed is
# mostly ACA/health ("Get Covered Illinois") so a pc_keywords allowlist must
# keep only the P&C-relevant releases.
_IL_FEED = {
    "newsFeedItemList": [
        {"title": "Get Covered Illinois Extends Open Enrollment Deadline",
         "type": "Press Release", "date": "Monday, December 15", "year": "2025",
         "description": "residents now have until December 31 to enroll in health insurance coverage.",
         "url": "https://www.illinois.gov/news/press-release.32010.html"},
        {"title": "IDOI Calls on Insurance Companies to Provide Policyholders Relief",
         "type": "Press Release", "date": "Thursday, October 16", "year": "2025",
         "description": "IDOI is calling on insurance companies to give homeowners relief during the disaster.",
         "url": "https://www.illinois.gov/news/press-release.31888.html"},
        {"title": "IDOI Kicks Off Its Annual Mental Health Parity Campaign",
         "type": "Press Release", "date": "Monday, May 05", "year": "2025",
         "description": "consumer education campaign highlighting mental health parity.",
         "url": "https://www.illinois.gov/news/press-release.31000.html"},
        {"title": "Department Approves Personal Auto Rate Filing for Carrier",
         "type": "Press Release", "date": "Monday, March 10", "year": "2025",
         "description": "a rate filing affecting automobile premiums statewide.",
         "url": "https://www.illinois.gov/news/press-release.30777.html"},
    ]
}


def test_il_json_feed_filters_health_parses_date(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get",
                        lambda *a, **k: _Resp("", payload=_IL_FEED))
    # pc_allowlist pulls the shared defaults.pc_keywords from the loaded config.
    entry = {"json_feed": True, "pc_allowlist": True, "max_items": 20}
    items = ingestor._scrape_state("IL", "Illinois DOI",
                                   "https://idoi.illinois.gov/x/news_feed.model.json", entry)
    titles = [it.title for it in items]
    # The disaster-relief (policyholder/homeowners) and the auto rate-filing items
    # are kept; the ACA-enrollment and mental-health-parity items are dropped.
    assert titles == [
        "[IL DOI] IDOI Calls on Insurance Companies to Provide Policyholders Relief",
        "[IL DOI] Department Approves Personal Auto Rate Filing for Carrier",
    ]
    it = items[0]
    assert it.metadata == {"topic_hint": "regulatory_rate", "state": "IL", "agency": "Illinois DOI"}
    # "Thursday, October 16" + year "2025" → strptime-parseable October 16, 2025.
    assert (it.published_at.year, it.published_at.month, it.published_at.day) == (2025, 10, 16)
    assert it.source_id == "IL:/news/press-release.31888.html"


def test_il_json_feed_health_dropped_even_without_allowlist(monkeypatch, ingestor):
    # No P&C allowlist, but the no-health rule still drops the ACA + mental-health
    # items; only the two non-health releases survive.
    monkeypatch.setattr("digest.ingest.state_doi.requests.get",
                        lambda *a, **k: _Resp("", payload=_IL_FEED))
    items = ingestor._scrape_state("IL", "Illinois DOI", "https://x/feed.json",
                                   {"json_feed": True})
    assert len(items) == 2


def test_il_json_feed_drop_health_opt_out_keeps_all(monkeypatch, ingestor):
    # drop_health:false disables the no-health rule → all four pass.
    monkeypatch.setattr("digest.ingest.state_doi.requests.get",
                        lambda *a, **k: _Resp("", payload=_IL_FEED))
    items = ingestor._scrape_state("IL", "Illinois DOI", "https://x/feed.json",
                                   {"json_feed": True, "drop_health": False})
    assert len(items) == 4


def test_il_json_feed_respects_max_items(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get",
                        lambda *a, **k: _Resp("", payload=_IL_FEED))
    items = ingestor._scrape_state("IL", "Illinois DOI", "https://x/feed.json",
                                   {"json_feed": True, "max_items": 1})
    assert len(items) == 1


# ── NJ DOBI: per-year static index (td + pr<YYMMDD>.html, date in cell text) ──
# Validated live 2026-06-03. NJ DOBI also runs Get Covered NJ → health-heavy.
_NJ_HTML = """
<table><tr>
  <td>May 12, 2026 - <a href="pr260512.html">DOBI Approves Homeowners Rate Filing for Coastal Carriers</a></td>
  <td>April 21, 2026 - <a href="pr260421.html">Residents Encouraged to Enroll in Get Covered New Jersey</a></td>
  <td>March 02, 2026 - <a href="pr260302.html">Commissioner Statement on Auto Insurance Reforms</a></td>
</tr></table>
"""


def test_nj_td_index_date_from_text_and_health_allowlist(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get", lambda *a, **k: _Resp(_NJ_HTML))
    entry = {"selector": "td:has(a[href^='pr'])", "date_from_text": True,
             "pc_allowlist": True, "max_items": 20}
    items = ingestor._scrape_state("NJ", "New Jersey DOBI",
                                   "https://www.nj.gov/dobi/pressreleases/2026.html", entry)
    titles = [it.title for it in items]
    # Get-Covered-NJ (health) dropped; the homeowners rate filing + auto reforms kept.
    assert titles == [
        "[NJ DOI] DOBI Approves Homeowners Rate Filing for Coastal Carriers",
        "[NJ DOI] Commissioner Statement on Auto Insurance Reforms",
    ]
    # title is the anchor text (no date prefix — NJ's date is sibling cell text);
    # date_from_text read "May 12, 2026" out of the cell.
    it = items[0]
    assert (it.published_at.year, it.published_at.month, it.published_at.day) == (2026, 5, 12)
    assert it.url.endswith("/dobi/pressreleases/pr260512.html")


# ── MI DIFS: Sitecore SXA results JSON (Html fragment title, URL-path date) ──
# Validated live 2026-06-03. DIFS regulates insurance + banking → mixed feed.
def _mi(url, title):
    return {"Url": url, "Html": f'<a class="content-title-link" href="{url}">{title}</a>'}

_MI_JSON = {"Results": [
    _mi("/difs/news-and-outreach/press-releases/2026/06/01/difs-mortgage-tips",
        "DIFS Shares Key Mortgage Tips for National Homeownership Month"),
    _mi("/difs/news-and-outreach/press-releases/2026/05/13/difs-shop-health",
        "DIFS to Michiganders: Shop Smart for Health Insurance"),
    _mi("/difs/news-and-outreach/press-releases/2026/04/09/difs-home-inventory",
        "DIFS Encourages Michiganders to Complete a Home Inventory"),
    _mi("/difs/news-and-outreach/press-releases/2026/03/02/difs-no-fault",
        "DIFS Issues Guidance on Auto No-Fault Reforms"),
]}


def test_mi_sxa_json_html_title_urlpath_date_and_filters(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get",
                        lambda *a, **k: _Resp("", payload=_MI_JSON))
    entry = {"json_feed": True, "json_list_key": "Results", "json_url_field": "Url",
             "json_html_field": "Html", "json_html_title_selector": ".content-title-link",
             "pc_allowlist": True, "pc_keywords_extra": ["no-fault", "auto"], "max_items": 20}
    items = ingestor._scrape_state(
        "MI", "Michigan DIFS",
        "https://www.michigan.gov/difs/sxa/search/results/?p=30", entry)
    titles = [it.title for it in items]
    # Mortgage/banking dropped (word-boundary: "Homeownership" ≠ "homeowners"),
    # "Health Insurance" dropped (no-health); "Home Inventory" + "Auto No-Fault" kept.
    assert titles == [
        "[MI DOI] DIFS Encourages Michiganders to Complete a Home Inventory",
        "[MI DOI] DIFS Issues Guidance on Auto No-Fault Reforms",
    ]
    it = items[0]
    # date came from the /2026/04/09/ URL path; href resolved against the domain.
    assert (it.published_at.year, it.published_at.month, it.published_at.day) == (2026, 4, 9)
    assert it.url == ("https://www.michigan.gov/difs/news-and-outreach/"
                      "press-releases/2026/04/09/difs-home-inventory")


# ── NV DOI: static div.article, date baked into the title text ──
# Validated live 2026-06-03. NV is insurance-only → no P&C allowlist, just no-health.
_NV_HTML = """
<div class="article"><a href="/News_Notices/Press_Releases/May_13,_2026_-_Fraud/">May 13, 2026 - 2026 Annual Fraud Assessment</a></div>
<div class="teaser"><a href="/News_Notices/Press_Releases/May_13,_2026_-_Fraud/">Read More</a></div>
<div class="article"><a href="/News_Notices/Press_Releases/April_28,_2026_-_Medicare/">April 28, 2026 - Commissioner presents at Medicare conference</a></div>
<div class="article"><a href="/News_Notices/Press_Releases/March_04,_2026_-_Dividend/">March 04, 2026 - Nevada Auto Policyholders to Receive Dividend</a></div>
"""


def test_nv_article_strip_date_prefix_and_health_drop(monkeypatch, ingestor):
    monkeypatch.setattr("digest.ingest.state_doi.requests.get", lambda *a, **k: _Resp(_NV_HTML))
    entry = {"selector": "div.article:has(a[href*='Press_Releases'])",
             "date_from_text": True, "strip_date_prefix": True, "max_items": 20}
    items = ingestor._scrape_state("NV", "Nevada DOI",
                                   "https://doi.nv.gov/News-Notices/Press-Releases/", entry)
    titles = [it.title for it in items]
    # Medicare item dropped (no-health); the date prefix is stripped off the rest.
    assert titles == [
        "[NV DOI] 2026 Annual Fraud Assessment",
        "[NV DOI] Nevada Auto Policyholders to Receive Dividend",
    ]
    assert (items[0].published_at.year, items[0].published_at.month, items[0].published_at.day) == (2026, 5, 13)


# ── filter primitives ──

def test_health_denylist_word_boundary():
    from digest.ingest.state_doi import _kw_hit, _HEALTH_DENYLIST
    assert _kw_hit("shop smart for health insurance", _HEALTH_DENYLIST)
    assert _kw_hit("Get Covered New Jersey enrollment", _HEALTH_DENYLIST)
    assert _kw_hit("Medicare fraud prevention week", _HEALTH_DENYLIST)
    # P&C headlines must NOT trip the health filter.
    assert not _kw_hit("homeowners rate filing approved", _HEALTH_DENYLIST)
    assert not _kw_hit("national homeownership month mortgage tips", _HEALTH_DENYLIST)


def test_pc_allowlist_word_boundary_excludes_homeownership(ingestor):
    # "homeowners" must not match inside "homeownership" (mortgage/banking noise).
    entry = {"pc_allowlist": True}
    assert ingestor._passes_filters("DIFS approves homeowners rate filing", entry)
    assert not ingestor._passes_filters("Tips for National Homeownership Month", entry)
