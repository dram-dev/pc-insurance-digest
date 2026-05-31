"""Generic Reddit fetching — RSS feed, public JSON endpoint, or PRAW.

Three fetch routines over a list of subreddit "group" configs. The domain owns
the group list (which subreddits, thresholds) and supplies a ready-to-use
transport — an HTTP session for the RSS / JSON endpoints, or a configured PRAW
client — so credentials and the mode decision stay domain-side.

A group config: ``{"name": str, "subreddits": [str], "min_score"?: int,
"min_comments"?: int, "limit"?: int, "time_filter"?: str}``.

Endpoint reality (2026): Reddit 403s the unauthenticated `.json` endpoint even
from residential IPs, and OAuth (PRAW) needs Responsible-Builder approval. The
`.rss` Atom feed is still served unauthenticated — so RSS is the default path.
Its trade-off is that it carries no score / comment counts.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from digest_core.types import IngestedItem

logger = logging.getLogger(__name__)

JSON_URL = "https://www.reddit.com/r/{sub}/top.json"
RSS_URL = "https://www.reddit.com/r/{sub}/top/.rss"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(value: str, *, limit: int = 2000) -> str:
    """Flatten the HTML body Reddit's Atom feed carries into plain text."""
    if not value:
        return ""
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()
    return text[:limit]


def _group_thresholds(group: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        group.get("min_score", 50),
        group.get("min_comments", 10),
        group.get("limit", 10),
        group.get("time_filter", "day"),
    )


def fetch_reddit_json(
    groups: list[dict[str, Any]],
    session: Any,
    source_name: str = "reddit",
    delay_sec: float = 1.5,
    timeout_sec: int = 20,
) -> list[IngestedItem]:
    """Pull top posts per subreddit via the public `.json` endpoint.

    `session` is a requests-style session (must set a non-default User-Agent;
    Reddit blocks the stdlib default). Per-subreddit failures are logged and
    skipped. Sleeps `delay_sec` between requests to stay polite.
    """
    items: list[IngestedItem] = []
    for group in groups:
        group_name = group["name"]
        min_score, min_comments, limit, time_filter = _group_thresholds(group)
        for sub_name in group["subreddits"]:
            try:
                r = session.get(
                    JSON_URL.format(sub=sub_name),
                    params={"t": time_filter, "limit": limit, "raw_json": 1},
                    timeout=timeout_sec,
                )
                r.raise_for_status()
                payload = r.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s json: failed on r/%s: %s", source_name, sub_name, exc)
                time.sleep(delay_sec)
                continue

            for child in payload.get("data", {}).get("children", []):
                p = child.get("data", {})
                if p.get("stickied"):
                    continue
                score = p.get("score", 0) or 0
                n_comments = p.get("num_comments", 0) or 0
                if score < min_score or n_comments < min_comments:
                    continue
                permalink = p.get("permalink", "")
                items.append(
                    IngestedItem(
                        source=source_name,
                        source_id=p.get("id", ""),
                        title=p.get("title", "(no title)"),
                        url=f"https://reddit.com{permalink}",
                        author=p.get("author"),
                        content=p.get("selftext") or "",
                        published_at=datetime.fromtimestamp(
                            p.get("created_utc", 0), tz=timezone.utc
                        ),
                        metadata={
                            "subreddit": sub_name,
                            "group": group_name,
                            "score": score,
                            "num_comments": n_comments,
                            "external_url": p.get("url") if not p.get("is_self") else None,
                            "fetched_via": "json",
                        },
                    )
                )
            time.sleep(delay_sec)
    return items


def fetch_reddit_rss(
    groups: list[dict[str, Any]],
    session: Any,
    source_name: str = "reddit",
    delay_sec: float = 1.5,
    timeout_sec: int = 20,
) -> list[IngestedItem]:
    """Pull top posts per subreddit via Reddit's Atom RSS feed.

    `session` is a requests-style session (set a non-default User-Agent). The
    `.json` endpoint now 403s from residential IPs and OAuth needs approval, but
    `.rss` is still served. Trade-off: RSS exposes no score / comment counts, so
    the per-group `min_score` / `min_comments` thresholds are inert here —
    `top/.rss?t=<window>` already returns the window's *top* posts, and triage +
    the 0.7 source multiplier handle residual noise. `limit` still caps posts per
    subreddit. Per-subreddit failures are logged and skipped.
    """
    import feedparser  # noqa: PLC0415 — heavy import, only needed on the RSS path

    items: list[IngestedItem] = []
    for group in groups:
        group_name = group["name"]
        _, _, limit, time_filter = _group_thresholds(group)
        for sub_name in group["subreddits"]:
            try:
                r = session.get(
                    RSS_URL.format(sub=sub_name),
                    params={"t": time_filter, "limit": limit},
                    timeout=timeout_sec,
                )
                r.raise_for_status()
                feed = feedparser.parse(r.content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s rss: failed on r/%s: %s", source_name, sub_name, exc)
                time.sleep(delay_sec)
                continue

            for entry in feed.entries[:limit]:
                raw_id = entry.get("id", "")            # e.g. "t3_1ts9u38"
                post_id = raw_id.split("_", 1)[-1] if raw_id else ""
                if not post_id:
                    continue
                author = entry.get("author") or None
                if author and author.startswith("/u/"):
                    author = author[3:]
                ts = entry.get("updated_parsed") or entry.get("published_parsed")
                published_at = datetime(*ts[:6], tzinfo=timezone.utc) if ts else None
                content = entry.get("content") or []
                body = content[0].get("value", "") if content else ""
                items.append(
                    IngestedItem(
                        source=source_name,
                        source_id=post_id,
                        title=entry.get("title", "(no title)"),
                        url=entry.get("link", ""),
                        author=author,
                        content=_html_to_text(body),
                        published_at=published_at,
                        metadata={
                            "subreddit": sub_name,
                            "group": group_name,
                            "score": None,         # not exposed by RSS
                            "num_comments": None,  # not exposed by RSS
                            "external_url": None,  # RSS doesn't split self vs link
                            "fetched_via": "rss",
                        },
                    )
                )
            time.sleep(delay_sec)
    return items


def fetch_reddit_praw(
    groups: list[dict[str, Any]],
    reddit_client: Any,
    source_name: str = "reddit",
) -> list[IngestedItem]:
    """Pull top posts per subreddit via a configured (read-only) PRAW client.

    `reddit_client` is a `praw.Reddit` instance — built domain-side so this
    module never imports praw. Per-subreddit failures are logged and skipped.
    """
    items: list[IngestedItem] = []
    for group in groups:
        group_name = group["name"]
        min_score, min_comments, limit, time_filter = _group_thresholds(group)
        for sub_name in group["subreddits"]:
            try:
                sub = reddit_client.subreddit(sub_name)
                for post in sub.top(time_filter=time_filter, limit=limit):
                    if post.score < min_score or post.num_comments < min_comments:
                        continue
                    if post.stickied:
                        continue
                    items.append(
                        IngestedItem(
                            source=source_name,
                            source_id=post.id,
                            title=post.title,
                            url=f"https://reddit.com{post.permalink}",
                            author=str(post.author) if post.author else None,
                            content=post.selftext or "",
                            published_at=datetime.fromtimestamp(
                                post.created_utc, tz=timezone.utc
                            ),
                            metadata={
                                "subreddit": sub_name,
                                "group": group_name,
                                "score": post.score,
                                "num_comments": post.num_comments,
                                "external_url": post.url if not post.is_self else None,
                                "fetched_via": "praw",
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s praw: failed on r/%s: %s", source_name, sub_name, exc)
    return items
