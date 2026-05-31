"""Tests for the lifted Reddit fetch loops in digest_core.ingest.reddit.

Both transports are faked (an HTTP session for JSON, a stand-in client for
PRAW) so the mapping/threshold/stickied logic is exercised without network or
the praw dependency. delay_sec=0 keeps the JSON path instant.
"""
from __future__ import annotations

import digest_core.ingest.reddit as core_reddit

GROUPS = [{
    "name": "g1", "subreddits": ["Insurance"],
    "min_score": 50, "min_comments": 10, "limit": 5,
}]


# ── JSON path ───────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, children):
        self._children = children

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"children": self._children}}


def _child(**kw):
    data = {
        "id": "x", "title": "T", "permalink": "/r/x/c/1", "author": "a",
        "selftext": "body", "score": 100, "num_comments": 20, "created_utc": 0,
        "is_self": True, "stickied": False, "url": "https://ext",
    }
    data.update(kw)
    return {"data": data}


def test_fetch_reddit_json_maps_and_filters():
    children = [
        _child(id="keep"),
        _child(id="lowscore", score=10),         # below min_score
        _child(id="lowcomments", num_comments=2),  # below min_comments
        _child(id="sticky", stickied=True),       # stickied skipped
    ]

    class _Session:
        def get(self, url, params=None, timeout=None):
            return _Resp(children)

    items = core_reddit.fetch_reddit_json(GROUPS, _Session(), delay_sec=0)
    assert [i.source_id for i in items] == ["keep"]
    it = items[0]
    assert it.source == "reddit"
    assert it.url == "https://reddit.com/r/x/c/1"
    assert it.metadata["fetched_via"] == "json"
    assert it.metadata["subreddit"] == "Insurance"
    assert it.metadata["external_url"] is None  # is_self → no external url


def test_fetch_reddit_json_skips_failing_subreddit():
    class _Session:
        def get(self, url, params=None, timeout=None):
            raise RuntimeError("429 rate limited")

    assert core_reddit.fetch_reddit_json(GROUPS, _Session(), delay_sec=0) == []


# ── RSS path ────────────────────────────────────────────────────────────


_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_keep1</id>
    <title>Homeowners rate filing approved</title>
    <link href="https://old.reddit.com/r/Insurance/comments/keep1/x/"/>
    <author><name>/u/poster</name></author>
    <updated>2026-05-30T19:59:09+00:00</updated>
    <content type="html">&lt;div&gt;&lt;p&gt;Body &amp;amp; text&lt;/p&gt;&lt;/div&gt;</content>
  </entry>
  <entry>
    <id>t3_keep2</id>
    <title>Second post</title>
    <link href="https://old.reddit.com/r/Insurance/comments/keep2/y/"/>
    <author><name>/u/other</name></author>
    <updated>2026-05-30T10:00:00+00:00</updated>
    <content type="html">&lt;p&gt;more&lt;/p&gt;</content>
  </entry>
</feed>"""


class _RssResp:
    content = _ATOM.encode("utf-8")

    def raise_for_status(self):
        pass


def test_fetch_reddit_rss_maps_and_ignores_score_filters():
    """RSS has no score/comments, so those thresholds must NOT drop posts."""
    class _Session:
        def get(self, url, params=None, timeout=None):
            assert url.endswith("/r/Insurance/top/.rss")
            return _RssResp()

    items = core_reddit.fetch_reddit_rss(GROUPS, _Session(), delay_sec=0)
    assert [i.source_id for i in items] == ["keep1", "keep2"]   # t3_ stripped, none filtered
    it = items[0]
    assert it.source == "reddit"
    assert it.author == "poster"                                # /u/ stripped
    assert it.url == "https://old.reddit.com/r/Insurance/comments/keep1/x/"
    assert it.content == "Body & text"                          # HTML flattened + unescaped
    assert it.metadata["fetched_via"] == "rss"
    assert it.metadata["score"] is None and it.metadata["num_comments"] is None
    assert it.published_at is not None


def test_fetch_reddit_rss_respects_limit():
    groups = [{"name": "g", "subreddits": ["Insurance"], "limit": 1}]

    class _Session:
        def get(self, url, params=None, timeout=None):
            return _RssResp()

    assert len(core_reddit.fetch_reddit_rss(groups, _Session(), delay_sec=0)) == 1


def test_fetch_reddit_rss_skips_failing_subreddit():
    class _Session:
        def get(self, url, params=None, timeout=None):
            raise RuntimeError("503 unavailable")

    assert core_reddit.fetch_reddit_rss(GROUPS, _Session(), delay_sec=0) == []


# ── PRAW path ───────────────────────────────────────────────────────────


class _Post:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Sub:
    def __init__(self, posts):
        self._posts = posts

    def top(self, time_filter=None, limit=None):
        return iter(self._posts)


class _Client:
    def __init__(self, posts):
        self._posts = posts

    def subreddit(self, name):
        return _Sub(self._posts)


def test_fetch_reddit_praw_maps_and_filters():
    posts = [
        _Post(id="p1", title="P1", permalink="/r/x/1", author="u", selftext="b",
              score=100, num_comments=20, created_utc=0, is_self=False,
              url="https://ext", stickied=False),
        _Post(id="low", title="L", permalink="/r/x/2", author="u", selftext="",
              score=5, num_comments=1, created_utc=0, is_self=True, url="",
              stickied=False),
    ]
    items = core_reddit.fetch_reddit_praw(GROUPS, _Client(posts))
    assert [i.source_id for i in items] == ["p1"]
    assert items[0].metadata["fetched_via"] == "praw"
    assert items[0].metadata["external_url"] == "https://ext"  # not is_self
