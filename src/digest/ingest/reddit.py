"""Reddit ingestor — defaults to the public JSON endpoint (no API key).

Reddit's API now requires pre-approval for personal-use scripts as of late 2025.
While that approval is pending, this ingestor uses the public `.json` endpoint
(`https://www.reddit.com/r/<sub>/top.json?t=day`) which requires no auth.

When/if PRAW approval comes through, set REDDIT_USE_PRAW=true in .env to switch
to the richer PRAW path that also exposes per-post score and comment count.

This shell owns credentials, the json/praw mode decision, and the subreddit
config; the fetch loops live in `digest_core.ingest.reddit`.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest.reddit import fetch_reddit_json, fetch_reddit_praw

logger = logging.getLogger(__name__)

SUBREDDITS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "subreddits.yaml"


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
            ua = settings.reddit_user_agent or "pc-insurance-digest/0.1 (JSON mode)"
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": ua})

    def fetch(self) -> list[IngestedItem]:
        groups = self.config["groups"]
        if self.mode == "praw":
            return fetch_reddit_praw(groups, self.reddit, source_name=self.name)
        return fetch_reddit_json(groups, self.session, source_name=self.name)
