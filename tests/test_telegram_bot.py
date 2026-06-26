"""Interactive ask-bot — authorization, reply formatting, message handling.

PC's bot answers via digest.semantic.ask (not a separate ask module) and reports
a BackendError (Ollama/MLX down) gracefully.
"""
from __future__ import annotations

import pytest

from digest_core.summarize.backends import BackendError

from digest import semantic, telegram_bot as tb


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound messages; stub the typing indicator."""
    msgs: list[str] = []
    monkeypatch.setattr(tb.notifier, "send", lambda text: msgs.append(text) or True)
    monkeypatch.setattr(tb.notifier, "send_chat_action", lambda *a, **k: None)
    return msgs


def test_is_authorized_matches_only_configured_chat(monkeypatch):
    monkeypatch.setattr(tb.settings, "telegram_chat_id", "123")
    assert tb._is_authorized({"message": {"chat": {"id": 123}}}) is True
    assert tb._is_authorized({"message": {"chat": {"id": 999}}}) is False
    assert tb._is_authorized({}) is False  # no message → not authorized


def test_format_reply_renders_answer_and_sources():
    result = {
        "answer": "Pricing is firming <fast> & hot",
        "sources": [
            {"n": 1, "title": "FL reinsurance", "source": "rss",
             "url": "https://x.test/a?b=1&c=2"},
        ],
        "error": None,
    }
    out = tb._format_reply(result)
    assert "Pricing is firming &lt;fast&gt; &amp; hot" in out  # escaped
    assert "[1] FL reinsurance" in out
    assert "b=1&amp;c=2" in out


def test_format_reply_handles_missing_synthesis():
    out = tb._format_reply({"answer": None, "sources": [
        {"n": 1, "title": "T", "source": "rss", "url": None}], "error": None})
    assert "synthesis unavailable" in out
    assert "[1] T" in out


def test_format_reply_surfaces_error_when_no_sources():
    out = tb._format_reply({"answer": "", "sources": [], "error": "no embeddings yet"})
    assert "no embeddings yet" in out


def test_handle_message_answers_question(sent, monkeypatch):
    monkeypatch.setattr(
        semantic, "ask",
        lambda q, **k: {"answer": "A [#1]", "sources": [
            {"n": 1, "title": "Item", "source": "rss", "url": None}], "error": None},
    )
    assert tb._handle_message({"text": "what's up with reinsurance?"}) is True
    assert sent and "A [#1]" in sent[-1]


def test_handle_message_command_sends_help(sent):
    assert tb._handle_message({"text": "/start"}) is True
    assert "Ask the P&C digest archive" in sent[-1]


def test_handle_message_ignores_empty(sent):
    assert tb._handle_message({"text": "   "}) is False
    assert sent == []


def test_handle_message_reports_backend_error(sent, monkeypatch):
    def _raise(q, **k):
        raise BackendError("Ollama embeddings unreachable")

    monkeypatch.setattr(semantic, "ask", _raise)
    assert tb._handle_message({"text": "question?"}) is True
    assert "Ollama embeddings unreachable" in sent[-1]


def test_link_message_routes_to_capture(sent, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        tb.capture, "capture",
        lambda text, **k: captured.update(text=text, author=k.get("author"))
        or {"kind": "article", "chars": 1200, "title": "An article"},
    )
    assert tb._handle_message({"text": "https://example.com/a"}) is True
    assert captured["text"] == "https://example.com/a"
    assert "Captured full article" in sent[-1]


def test_forwarded_message_routes_to_capture(sent, monkeypatch):
    monkeypatch.setattr(
        tb.capture, "capture",
        lambda text, **k: {"kind": "tweet", "chars": 240, "title": "@nicetweet"},
    )
    update = {"text": "some tweet text", "forward_origin": {"sender_user_name": "Jane"}}
    assert tb._handle_message(update) is True
    assert "Captured X post" in sent[-1]


def test_capture_replies_with_inline_takeaway(sent, monkeypatch):
    from digest.summarize import SummaryOutput

    monkeypatch.setattr(
        tb.capture, "capture",
        lambda text, **k: {"kind": "article", "chars": 1500, "title": "Reins news",
                           "url": "https://x", "body": "Renewals priced up 30%."},
    )
    monkeypatch.setattr(
        "digest.summarize.summarize_item",
        lambda item, **k: SummaryOutput(
            topic="reinsurance_cycle", summary="Renewals firmed sharply.",
            why_it_matters="Capacity is tight into wind season.", confidence="high"),
    )
    assert tb._handle_message({"text": "https://x"}) is True
    msg = sent[-1]
    assert "Takeaway" in msg
    assert "Renewals firmed sharply." in msg
    assert "Capacity is tight into wind season." in msg


def test_capture_takeaway_skipped_when_no_body(monkeypatch):
    # No body → no summarizer call, no takeaway, capture still succeeds.
    monkeypatch.setattr(
        "digest.summarize.summarize_item",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not summarize")),
    )
    assert tb._capture_takeaway({"title": "T"}) is None


def test_forward_author_extraction():
    assert tb._forward_author({"forward_origin": {"sender_user": {"first_name": "Ada"}}}) == "Ada"
    assert tb._forward_author({"forward_sender_name": "Hidden User"}) == "Hidden User"
    assert tb._forward_author({}) is None
