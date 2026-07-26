"""Full-text extraction — excerpt gating + graceful-fallback contract.

These tests never touch the network. The mechanics live in
`digest_core.ingest.fulltext`, so that is where `fetch_fulltext` is patched;
`digest.ingest.fulltext` is the domain shell that binds the FULLTEXT_* settings.
"""
from __future__ import annotations

import pytest
from digest_core.ingest import fulltext as core

from digest.ingest import fulltext


@pytest.fixture(autouse=True)
def _fulltext_settings(monkeypatch: pytest.MonkeyPatch):
    """Deterministic thresholds regardless of the developer's .env."""
    monkeypatch.setattr(fulltext.settings, "fulltext_enabled", True)
    monkeypatch.setattr(fulltext.settings, "fulltext_min_chars", 50)
    monkeypatch.setattr(fulltext.settings, "fulltext_max_chars", 8000)


def _fetches(value):
    """Stub for the core fetcher, which takes keyword-only tuning args."""
    return lambda url, **kw: value


def test_looks_like_excerpt():
    assert core.looks_like_excerpt(None, 50)
    assert core.looks_like_excerpt("", 50)
    assert core.looks_like_excerpt("short teaser", 50)
    assert not core.looks_like_excerpt("x" * 60, 50)


def test_enrich_expands_short_excerpt(monkeypatch):
    full = "FULL ARTICLE BODY " * 20
    monkeypatch.setattr(core, "fetch_fulltext", _fetches(full))
    assert fulltext.enrich("teaser", "https://example.test/a") == full


def test_enrich_keeps_full_content_without_fetching(monkeypatch):
    called = False

    def _boom(url, **kw):
        nonlocal called
        called = True
        return "nope"

    monkeypatch.setattr(core, "fetch_fulltext", _boom)
    body = "x" * 100  # already above min_chars → treated as complete
    assert fulltext.enrich(body, "https://example.test/a") == body
    assert called is False


def test_enrich_no_url_returns_base(monkeypatch):
    monkeypatch.setattr(core, "fetch_fulltext", _fetches("should not be used"))
    assert fulltext.enrich("teaser", None) == "teaser"


def test_enrich_disabled_returns_base(monkeypatch):
    monkeypatch.setattr(fulltext.settings, "fulltext_enabled", False)
    monkeypatch.setattr(core, "fetch_fulltext", _fetches("should not be used"))
    assert fulltext.enrich("teaser", "https://example.test/a") == "teaser"


def test_enrich_fetch_failure_falls_back(monkeypatch):
    monkeypatch.setattr(core, "fetch_fulltext", _fetches(None))
    assert fulltext.enrich("teaser", "https://example.test/a") == "teaser"


def test_enrich_ignores_shorter_extraction(monkeypatch):
    monkeypatch.setattr(core, "fetch_fulltext", _fetches("tiny"))
    long_excerpt = "a longer teaser than the extraction returned"
    assert fulltext.enrich(long_excerpt, "https://example.test/a") == long_excerpt


def test_enrich_none_content_becomes_empty(monkeypatch):
    monkeypatch.setattr(core, "fetch_fulltext", _fetches(None))
    assert fulltext.enrich(None, None) == ""


def test_fetch_fulltext_network_error_returns_none(monkeypatch):
    def _raise(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(core.requests, "get", _raise)
    assert fulltext.fetch_fulltext("https://example.test/a") is None
