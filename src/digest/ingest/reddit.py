"""Reddit ingestor — defaults to the Atom RSS feed (no API key).

Endpoint reality (2026): Reddit 403s the unauthenticated `.json` endpoint even
from residential IPs, and OAuth (PRAW) needs Responsible-Builder approval that
personal-use scripts increasingly can't get. The `.rss` Atom feed is still
served unauthenticated, so it's the default. Trade-off: RSS carries no score /
comment counts, so the per-group min_score / min_comments thresholds are inert
on that path (top/.rss already pre-sorts by top-of-window).

Mode selection:
- ``REDDIT_USE_PRAW=true`` → PRAW (needs REDDIT_CLIENT_ID/SECRET); richest data.
- ``REDDIT_MODE=json``     → legacy public JSON endpoint (currently 403s).
- otherwise               → RSS (default).

This shell owns credentials, the mode decision, and the subreddit config; the
fetch loops live in `digest_core.ingest.reddit`.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest.reddit import (
    fetch_reddit_json,
    fetch_reddit_praw,
    fetch_reddit_rss,
)

logger = logging.getLogger(__name__)

SUBREDDITS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "subreddits.yaml"


def _use_praw() -> bool:
    return os.getenv("REDDIT_USE_PRAW", "").lower() in ("1", "true", "yes")


def _reddit_mode() -> str:
    if _use_praw():
        return "praw"
    mode = os.getenv("REDDIT_MODE", "rss").lower()
    return mode if mode in ("rss", "json") else "rss"


class RedditIngestor(IngestorBase):
    name = "reddit"

    def __init__(self) -> None:
        self.config = yaml.safe_load(SUBREDDITS_CONFIG.read_text())
        self.mode = _reddit_mode()

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
            ua = settings.reddit_user_agent or "pc-insurance-digest/0.1"
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": ua})

    def fetch(self) -> list[IngestedItem]:
        groups = self.config["groups"]
        if self.mode == "praw":
            return fetch_reddit_praw(groups, self.reddit, source_name=self.name)
        if self.mode == "json":
            return fetch_reddit_json(groups, self.session, source_name=self.name)
        return fetch_reddit_rss(groups, self.session, source_name=self.name)
