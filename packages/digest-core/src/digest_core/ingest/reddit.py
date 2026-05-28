"""Generic Reddit fetching — public JSON endpoint or PRAW.

Two fetch routines over a list of subreddit "group" configs. The domain owns
the group list (which subreddits, thresholds) and supplies a ready-to-use
transport — an HTTP session for the auth-free JSON endpoint, or a configured
PRAW client — so credentials and the json/praw mode decision stay domain-side.

A group config: ``{"name": str, "subreddits": [str], "min_score"?: int,
"min_comments"?: int, "limit"?: int, "time_filter"?: str}``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from digest_core.types import IngestedItem

logger = logging.getLogger(__name__)

JSON_URL = "https://www.reddit.com/r/{sub}/top.json"


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
