"""Capture forwarded content into the Obsidian clipped flow.

A message forwarded to the ask-bot — an X/Twitter post, a link, a paragraph —
is written as a frontmatter .md into OBSIDIAN_CLIP_DIR, exactly like an Obsidian
Web Clipper file. The `clipped` ingestor then picks it up on the next pipeline
run (auto-kept, summarized, surfaced in the daily note), so the bot becomes a
one-tap capture inbox for the digest.

Resolution order for a URL:
  • X/Twitter status → tweet text via the fxtwitter API (X is login-walled, so
    trafilatura can't read it). Free, no auth.
  • any other URL → full article via the full-text extractor.
  • neither → file the forwarded text / bare URL as-is.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests
import yaml

from digest.config import settings
from digest.ingest.clipped import _resolve_clip_dir
from digest.ingest.fulltext import fetch_fulltext

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s)>\]]+")
# user + status id from x.com / twitter.com / fx|vxtwitter / nitter links
_X_STATUS_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter|fxtwitter|vxtwitter|nitter\.[^/]+)\."
    r"(?:com|net)/([^/]+)/status/(\d+)",
    re.I,
)
_UA = "pc-insurance-digest/0.1 (+https://github.com/dram-dev/pc-insurance-digest)"


def first_url(text: str | None) -> str | None:
    """First http(s) URL in `text`, or None."""
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def fetch_tweet(url: str) -> dict | None:
    """Resolve an X/Twitter status URL to {text, author} via fxtwitter. None on miss."""
    m = _X_STATUS_RE.search(url or "")
    if not m:
        return None
    user, tweet_id = m.group(1), m.group(2)
    try:
        r = requests.get(
            f"{settings.x_api_base.rstrip('/')}/{user}/status/{tweet_id}",
            headers={"User-Agent": _UA},
            timeout=12,
        )
        r.raise_for_status()
        tw = (r.json() or {}).get("tweet") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture: tweet fetch failed for %s: %s", url, exc)
        return None
    text = (tw.get("text") or "").strip()
    if not text:
        return None
    author = tw.get("author") or {}
    handle = author.get("screen_name")
    quote = tw.get("quote") or {}
    if quote.get("text"):
        qh = (quote.get("author") or {}).get("screen_name", "")
        text += f"\n\n— quoting @{qh}: {quote['text'].strip()}"
    return {"text": text, "author": f"@{handle}" if handle else author.get("name")}


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()[:n] or "clip"


def capture(
    text: str = "",
    *,
    url: str | None = None,
    author: str | None = None,
    title: str | None = None,
    source_label: str = "telegram",
) -> dict:
    """Write forwarded content into the clip dir. Returns a summary dict.

    Keys: path, title, url, chars, kind ('tweet' | 'article' | 'text'), body.
    """
    text = (text or "").strip()
    url = url or first_url(text)

    body = text
    kind = "text"
    if url:
        tweet = fetch_tweet(url)
        if tweet:
            body, kind = tweet["text"], "tweet"
            author = author or tweet["author"]
        else:
            full = fetch_fulltext(url)
            if full:
                body, kind = full, "article"
            elif not body:
                body = url
    if not body:
        raise ValueError("nothing to capture (no text or URL)")

    if not title:
        title = (body.splitlines()[0].strip()[:200] if body else url) or "Telegram capture"

    clip_dir = _resolve_clip_dir()
    clip_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc)
    path = clip_dir / f"{ts:%Y%m%d-%H%M%S}-{_slug(title)}.md"

    # Only a real URL goes in `source`: the clipped ingestor reads that key as
    # both the item's unique id and its link, so a constant sentinel would make
    # every URL-less capture collide on one source_id (all but the first are
    # then dropped by INSERT OR IGNORE) and render a dead non-http link.
    # Omitting it lets text captures fall through to the body-hash id.
    frontmatter: dict = {"title": title}
    if url:
        frontmatter["source"] = url
    frontmatter |= {
        "author": author or source_label,
        "created": ts.replace(microsecond=0).isoformat(),
        "tags": [source_label, "clipping"],
    }
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    path.write_text(f"---\n{fm_yaml}\n---\n{body}\n", encoding="utf-8")
    logger.info("capture: wrote %s (%d chars, kind=%s)", path.name, len(body), kind)

    return {"path": path, "title": title, "url": url, "chars": len(body),
            "kind": kind, "body": body}
