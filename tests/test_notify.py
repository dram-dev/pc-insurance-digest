"""Telegram notify sink — send contract, dedup, threshold, and HTML escaping.

Never touches the network: requests.post is monkeypatched. DB-backed tests use
the `fresh_db` fixture so dedup runs against a real notify_log + signal_scores.
PC pushes off the unbounded leaderboard score (signal_scores), not triage_score.
"""
from __future__ import annotations

import pytest

from digest import db
from digest.config import Settings
from digest_core.sinks import telegram as _tg

from digest.sinks import notify


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("bot8814:ABCdef", "8814:ABCdef"),   # doubled 'bot' prefix → stripped
        ("BOT8814:ABCdef", "8814:ABCdef"),   # case-insensitive
        ("8814:ABCdef", "8814:ABCdef"),      # clean token → untouched
        ("  8814:ABCdef  ", "8814:ABCdef"),  # surrounding whitespace trimmed
        ("", ""),                            # empty stays empty (disabled)
    ],
)
def test_token_bot_prefix_is_stripped(raw, expected):
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN=raw)
    assert s.telegram_bot_token == expected


class _Resp:
    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def captured(monkeypatch):
    """Capture sent payloads; return the list. Notifier is enabled."""
    sent: list[dict] = []

    def _post(url, json, timeout):  # noqa: A002 - mirrors requests.post kwarg
        sent.append(json)
        return _Resp()

    monkeypatch.setattr(_tg.requests, "post", _post)
    monkeypatch.setattr(notify.notifier, "enabled", True)
    monkeypatch.setattr(notify.notifier, "token", "t")
    monkeypatch.setattr(notify.notifier, "chat_id", "c")
    # Bypass quiet hours so send-path tests don't depend on wall-clock time.
    monkeypatch.setattr(notify, "_pushing_allowed", lambda *a, **k: True)
    return sent


def test_disabled_notifier_is_noop(monkeypatch):
    monkeypatch.setattr(notify.notifier, "enabled", False)
    # Even if requests.post would blow up, disabled short-circuits before it.
    monkeypatch.setattr(
        _tg.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert notify.notifier.send("hi") is False


def test_send_posts_html_payload(captured):
    assert notify.notifier.send("<b>hello</b>") is True
    assert captured[0]["parse_mode"] == "HTML"
    assert captured[0]["text"] == "<b>hello</b>"
    assert captured[0]["chat_id"] == "c"


def test_send_swallows_network_error(monkeypatch):
    monkeypatch.setattr(notify.notifier, "enabled", True)

    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(_tg.requests, "post", _boom)
    assert notify.notifier.send("hi") is False


def test_html_escaping_in_signal():
    row = {
        "id": 1,
        "topic": "M&A & capital",
        "title": "Carrier <beats> & raises",
        "why_it_matters": "combined > expected",
        "score": 2.0,
        "url": "https://x.test/a?b=1&c=2",
    }
    out = notify._format_signal(row)
    assert "M&amp;A &amp; capital" in out
    assert "&lt;beats&gt;" in out
    assert "combined &gt; expected" in out
    # the href URL is attribute-escaped too
    assert "b=1&amp;c=2" in out


def test_signal_header_has_tier_and_score():
    row = {"id": 1, "topic": "reserving", "title": "T", "why_it_matters": "w",
           "score": 2.0, "tier": "high", "url": "https://x"}
    out = notify._format_signal(row)
    assert out.startswith("<b>Top signal</b> · reserving")
    assert "(🔴 High · 2.00)" in out      # persisted tier badge + leaderboard score


def test_signal_meta_line_fields():
    row = {
        "id": 1, "topic": "reinsurance", "title": "Cat bond pricing", "why_it_matters": "big",
        "score": 1.9, "tier": "high", "url": "https://x", "source": "rss",
        "metadata_json": '{"feed": "Artemis"}',
        "published_at": "2026-06-24T13:00:00", "sentiment_label": "bullish",
    }
    out = notify._format_signal(row, regime="soft × elevated")
    assert "Artemis" in out             # feed name preferred over raw source
    assert "2026-06-24" in out          # publication date
    assert "🟢 bullish" in out          # sentiment + emoji
    assert "🌀 soft × elevated" in out  # regime tag


def test_source_name_resolution():
    assert notify._source_name("rss", '{"feed": "Artemis"}') == "Artemis"
    assert notify._source_name("courtlistener", None) == "CourtListener"  # pretty map
    assert notify._source_name("weirdsrc", None) == "Weirdsrc"           # title-case


def _seed_signal(source_id: str, score: float, *, title: str = "T",
                 tier: str = "high", summarized: str = "datetime('now')") -> int:
    """Insert a kept + summarized item plus its latest signal_scores row."""
    with db.get_conn() as conn:
        cur = conn.execute(
            f"""INSERT INTO items (source, source_id, title, url, content,
                                   triage_decision, triage_score, summary,
                                   why_it_matters, topic, summarized_at, ingested_at)
                VALUES ('rss', ?, ?, 'https://x.test/a', 'c',
                        'keep', 0.9, 'summary', 'why', 'reserving',
                        {summarized}, {summarized})""",
            (source_id, title),
        )
        item_id = cur.lastrowid
        conn.execute(
            "INSERT INTO signal_scores (item_id, computed_at, score, tier) "
            "VALUES (?, datetime('now'), ?, ?)",
            (item_id, score, tier),
        )
    return item_id


def test_top_signals_threshold_and_dedup(fresh_db, captured, monkeypatch):
    monkeypatch.setattr(notify.settings, "notify_min_score", 1.0)
    monkeypatch.setattr(notify.settings, "notify_max_per_run", 5)
    _seed_signal("hi", 2.0)     # above threshold → sent
    _seed_signal("lo", 0.5)     # below threshold → ignored

    first = notify.notify_top_signals()
    assert first == {"candidates": 1, "sent": 1, "suppressed": False}
    assert len(captured) == 1

    # Second run (simulating the pm pass) must not re-fire the same item.
    second = notify.notify_top_signals()
    assert second == {"candidates": 0, "sent": 0, "suppressed": False}
    assert len(captured) == 1


def test_top_signals_respects_max_per_run(fresh_db, captured, monkeypatch):
    monkeypatch.setattr(notify.settings, "notify_min_score", 1.0)
    monkeypatch.setattr(notify.settings, "notify_max_per_run", 2)
    for i in range(4):
        _seed_signal(f"s{i}", 2.0)
    res = notify.notify_top_signals()
    assert res["candidates"] == 2
    assert res["sent"] == 2


def test_pushing_allowed_only_8am_to_10pm(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(notify.settings, "notify_quiet_start_hour", 22)
    monkeypatch.setattr(notify.settings, "notify_quiet_end_hour", 8)

    def ok(h):
        return notify._pushing_allowed(datetime(2026, 6, 26, h, 30))

    assert ok(8) and ok(12) and ok(21)          # daytime → allowed
    assert not ok(7) and not ok(22) and not ok(23) and not ok(2)  # night → quiet


def test_quiet_hours_suppress_send_and_record(fresh_db, captured, monkeypatch):
    monkeypatch.setattr(notify, "_pushing_allowed", lambda *a, **k: False)
    monkeypatch.setattr(notify.settings, "notify_min_score", 1.0)
    _seed_signal("hi", 2.0)
    res = notify.notify_top_signals()
    # `suppressed` distinguishes quiet hours from "nothing scored high enough".
    assert res == {"candidates": 0, "sent": 0, "suppressed": True}
    assert captured == []  # nothing sent
    with db.get_conn() as conn:  # nothing recorded → can still fire later in-window
        assert conn.execute("SELECT COUNT(*) FROM notify_log").fetchone()[0] == 0


def test_recency_window_excludes_old_items(fresh_db, captured, monkeypatch):
    monkeypatch.setattr(notify.settings, "notify_min_score", 1.0)
    monkeypatch.setattr(notify.settings, "notify_lookback_hours", 24)
    _seed_signal("fresh", 2.0)  # summarized now
    _seed_signal("old", 3.0, summarized="datetime('now','-72 hours')")  # aged out
    res = notify.notify_top_signals()
    assert res["candidates"] == 1  # only the fresh one
    assert res["sent"] == 1


def test_quiet_hours_disabled_when_start_equals_end(monkeypatch):
    """A zero-width window means "no quiet hours", not "never push"."""
    from datetime import datetime
    monkeypatch.setattr(notify.settings, "notify_quiet_start_hour", 0)
    monkeypatch.setattr(notify.settings, "notify_quiet_end_hour", 0)
    assert all(
        notify._pushing_allowed(datetime(2026, 6, 26, h, 30)) for h in range(24)
    )


def test_href_rejects_non_web_schemes():
    """Telegram 400s the whole message on a non-http href."""
    assert notify._href("https://example.com/a") == "https://example.com/a"
    assert notify._href("obsidian://open?vault=V&file=F") is None
    assert notify._href("telegram:capture") is None
    assert notify._href(None) is None
    # A quote would otherwise terminate the attribute.
    assert '"' not in (notify._href('https://e.com/?q="x"') or "")


def test_why_it_matters_truncated_before_escaping():
    """Escaping first and slicing after can cut an &amp; in half."""
    row = {
        "topic": "personal_lines", "title": "T", "score": 2.0, "tier": "high",
        "why_it_matters": "P&C " * 200,   # 800 chars, ampersand-dense
        "source": "rss", "url": None, "published_at": None,
        "sentiment_label": None, "metadata_json": None,
    }
    out = notify._format_signal(row)
    assert "&am\n" not in out and not out.endswith("&am")
    # Every ampersand that survived is a complete entity.
    assert out.count("&") == out.count("&amp;")
