"""Tests for the lifted Obsidian render primitives in digest_core.obsidian.render."""
from __future__ import annotations

import urllib.parse

from digest_core.obsidian import render


def test_safe():
    assert render.safe("  hi  ") == "hi"
    assert render.safe(None) == ""


def test_row_get_missing_key_returns_none():
    assert render.row_get({"a": "x"}, "a") == "x"
    assert render.row_get({"a": "x"}, "missing") is None


def test_confidence_badge():
    assert render.confidence_badge("high") == "🟢 high"
    assert render.confidence_badge("medium", 0.912) == "🟡 medium · 0.91"
    assert render.confidence_badge("low", None) == "🟠 low"
    assert render.confidence_badge("bogus") == "—"
    assert render.confidence_badge(None) == "—"


def test_wikilink():
    assert render.wikilink("Personal Lines") == "[[Personal Lines]]"


def test_parse_see_also():
    assert render.parse_see_also('["a", "b"]') == ["a", "b"]
    assert render.parse_see_also(None) == []
    assert render.parse_see_also("not json") == []
    assert render.parse_see_also('{"not": "a list"}') == []


def test_chat_link_threads_digest_name_and_fields():
    row = {
        "id": 7, "title": "PGR 8-K", "url": "https://x/1", "source": "edgar",
        "author": "Progressive", "published_at": "2026-05-20T00:00:00",
        "summary": "Rate action", "why_it_matters": "Margin pressure",
    }
    link = render.chat_link(row, digest_name="P&C digest")
    assert link.startswith("[#7](https://claude.ai/new?q=")
    decoded = urllib.parse.unquote(link.split("?q=", 1)[1].rstrip(")"))
    assert "from my P&C digest" in decoded   # the macro/AI residue is gone
    assert "Title: PGR 8-K" in decoded
    assert "Source: edgar" in decoded
    assert "Published: 2026-05-20" in decoded


def test_chat_link_caps_prompt_length():
    row = {
        "id": 1, "title": "T", "url": "", "source": "", "author": "",
        "published_at": "", "summary": "x" * 10000, "why_it_matters": "",
    }
    q = render.chat_link(row).split("?q=", 1)[1].rstrip(")")
    assert len(urllib.parse.unquote(q)) <= render._CHAT_PROMPT_MAX_CHARS
