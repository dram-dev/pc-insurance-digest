"""Reddit ingestor — defaults to public JSON endpoint (no API key).

Reddit's API now requires pre-approval for personal-use scripts as of late 2025.
While that approval is pending, this ingestor uses the public `.json` endpoint
(`https://www.reddit.com/r/<sub>/top.json?t=day`) which requires no auth.

When/if PRAW approval comes through, set REDDIT_USE_PRAW=true in .env to switch
to the richer PRAW path that also exposes per-post score and comment count.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

SUBREDDITS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "subreddits.yaml"

JSON_URL = "https://www.reddit.com/r/{sub}/top.json"
REQUEST_DELAY_SEC = 1.5  # be polite to the public endpoint
REQUEST_TIMEOUT_SEC = 20


def _use_praw() -> bool:
    return os.getenv("REDDIT_USE_PRAW", "").lower() in ("1", "true", "yes")


class RedditIngestor(IngestorBase):
    name = "reddit"

    def __init__(self) -> None:
        self.config = yaml.safe_load(SUBREDDITS_CONFIG.read_text())
        self.mode = "praw" if _use_praw() else "json"

        if self.mode == "praw":
            if not settings.reddit_client_id:
                raise RuntimeError(
                    "REDDIT_USE_PRAW=true but REDDIT_CLIENT_ID not set. "
                    "Either provide credentials or unset REDDIT_USE_PRAW."
                )
            import praw  # noqa: PLC0415 — defer import; not needed in default mode

            self.reddit = praw.Reddit(
                client_id=settings.reddit_client_id,
                client_secret=settings.reddit_client_secret,
                user_agent=settings.reddit_user_agent,
            )
            self.reddit.read_only = True
        else:
            # User-Agent matters: Reddit blocks generic Python defaults.
            ua = settings.reddit_user_agent or "macro-ai-digest/0.1 (JSON mode)"
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": ua})

    # ── public API ───────────────────────────────────────────────
    def fetch(self) -> list[IngestedItem]:
        if self.mode == "praw":
            return self._fetch_praw()
        return self._fetch_json()

    # ── JSON path (default, no auth) ─────────────────────────────
    def _fetch_json(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for group in self.config["groups"]:
            group_name = group["name"]
            min_score = group.get("min_score", 50)
            min_comments = group.get("min_comments", 10)
            limit = group.get("limit", 10)
            time_filter = group.get("time_filter", "day")

            for sub_name in group["subreddits"]:
                url = JSON_URL.format(sub=sub_name)
                try:
                    r = self.session.get(
                        url,
                        params={"t": time_filter, "limit": limit, "raw_json": 1},
                        timeout=REQUEST_TIMEOUT_SEC,
                    )
                    r.raise_for_status()
                    payload = r.json()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reddit json: failed on r/%s: %s", sub_name, exc)
                    time.sleep(REQUEST_DELAY_SEC)
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
                            source=self.name,
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
                time.sleep(REQUEST_DELAY_SEC)
        return items

    # ── PRAW path (used when REDDIT_USE_PRAW=true) ───────────────
    def _fetch_praw(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for group in self.config["groups"]:
            group_name = group["name"]
            min_score = group.get("min_score", 50)
            min_comments = group.get("min_comments", 10)
            limit = group.get("limit", 10)
            time_filter = group.get("time_filter", "day")

            for sub_name in group["subreddits"]:
                try:
                    sub = self.reddit.subreddit(sub_name)
                    for post in sub.top(time_filter=time_filter, limit=limit):
                        if post.score < min_score or post.num_comments < min_comments:
                            continue
                        if post.stickied:
                            continue
                        items.append(
                            IngestedItem(
                                source=self.name,
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
                    logger.warning("reddit praw: failed on r/%s: %s", sub_name, exc)
        return items
