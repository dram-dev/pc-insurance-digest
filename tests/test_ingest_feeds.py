"""Tests for the lifted feed/HN fetch logic in digest_core.ingest.

Network is mocked at the core module boundary (feedparser.parse / requests.get),
so these exercise the mapping/dedup/limit/error-handling logic hermetically.
"""
from __future__ import annotations

import time

from digest_core.ingest import hackernews as core_hn
from digest_core.ingest import rss as core_rss


class _Parsed:
    def __init__(self, entries, bozo=0, exc=None):
        self.entries = entries
        self.bozo = bozo
        self.bozo_exception = exc


# ── fetch_feeds ─────────────────────────────────────────────────────────


def test_fetch_feeds_maps_entries(monkeypatch):
    entries = [
        {"id": "e1", "link": "https://x/1", "title": "T1", "author": "A",
         "summary": "sum1", "published_parsed": time.gmtime(0)},
        {"link": "https://x/2", "title": "T2", "content": [{"value": "full2"}]},
    ]
    monkeypatch.setattr(core_rss.feedparser, "parse", lambda url: _Parsed(entries))

    feeds = [{"url": "https://feed", "name": "MyFeed", "topic_hint": "cyber"}]
    items = core_rss.fetch_feeds(feeds, "rss", default_limit=15)

    assert len(items) == 2
    a, b = items
    assert a.source == "rss"
    assert a.source_id.startswith("MyFeed:")
    assert a.title == "T1"
    assert a.url == "https://x/1"
    assert a.content == "sum1"          # summary fallback
    assert a.published_at is not None
    assert a.metadata == {"feed": "MyFeed", "feed_url": "https://feed", "topic_hint": "cyber"}
    assert b.content == "full2"         # full content preferred over summary
    assert b.published_at is None       # no date keys present


def test_fetch_feeds_respects_limit(monkeypatch):
    entries = [{"link": f"https://x/{i}", "title": f"T{i}"} for i in range(20)]
    monkeypatch.setattr(core_rss.feedparser, "parse", lambda url: _Parsed(entries))

    assert len(core_rss.fetch_feeds([{"url": "u"}], "rss", default_limit=5)) == 5
    # per-feed override beats the default
    assert len(core_rss.fetch_feeds([{"url": "u", "limit": 3}], "rss", default_limit=5)) == 3


def test_fetch_feeds_skips_failing_feed(monkeypatch):
    def parse(url):
        if "bad" in url:
            raise RuntimeError("dead feed")
        return _Parsed([{"link": "https://ok/1", "title": "ok"}])
    monkeypatch.setattr(core_rss.feedparser, "parse", parse)

    items = core_rss.fetch_feeds([{"url": "bad"}, {"url": "good"}], "rss")
    assert [i.title for i in items] == ["ok"]


# ── fetch_hn ────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, hits):
        self._hits = hits

    def raise_for_status(self):
        pass

    def json(self):
        return {"hits": self._hits}


def test_fetch_hn_dedups_and_maps(monkeypatch):
    hits = [
        {"objectID": 1, "title": "Story 1", "url": "https://s/1", "author": "u1",
         "points": 200, "num_comments": 10, "created_at": "2026-05-20T00:00:00Z"},
        {"objectID": 1, "title": "dup"},                       # duplicate id → skipped
        {"objectID": 2, "title": "Story 2", "story_text": "body2",
         "created_at": "not-a-date"},                          # no url, bad date
    ]
    calls: list[dict] = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        return _Resp(hits if params["query"] == "q1" else [])

    monkeypatch.setattr(core_hn.requests, "get", fake_get)

    items = core_hn.fetch_hn(["q1", "q2"], min_points=100, hits_per_query=10)

    assert [i.source_id for i in items] == ["1", "2"]                       # dedup by id
    assert items[0].url == "https://s/1"
    assert items[1].url == "https://news.ycombinator.com/item?id=2"        # url fallback
    assert items[1].content == "body2"
    assert items[1].published_at is not None                                # bad date → now()
    assert calls[0]["numericFilters"] == "points>=100"                      # threshold threaded in
