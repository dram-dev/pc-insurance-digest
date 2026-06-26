"""Ingest .md clippings the user drops into an Obsidian folder.

The expected source is Obsidian Web Clipper output, or a file written by the
Telegram capture inbox (`digest.capture`): a YAML frontmatter block followed by
the post body. Typical frontmatter fields:

    ---
    title: "X post by @whoever"
    source: "https://x.com/whoever/status/12345"
    author: "@whoever"
    created: 2026-04-26T13:42:00
    tags: [telegram, clipping]
    ---
    <post body, often with quoted tweets and links>

Anything we clip is high-signal by definition (the user already triaged it
by the act of clipping), so this ingester:

  • marks each newly-ingested clip as `triage_decision='keep'` with score=1.0
  • bypasses the summarizer cap when the pipeline runs the clipped pass
  • stamps the source .md with `digest_processed_at` on success so re-runs
    are idempotent and the file stays in place (no moves, no deletes)

The dedicated source name `"clipped"` lets the daily-note renderer split
these out into their own section.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from digest import db
from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)


# YAML frontmatter is delimited by --- on its own line at top + closing ---
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*(?:\n|$)",
    re.DOTALL,
)

# Field names we'll look for, in priority order, when extracting the X post URL.
_URL_KEYS = ("source", "url", "permalink", "link")
_TITLE_KEYS = ("title", "name")
_AUTHOR_KEYS = ("author", "by", "creator")
_PUBLISHED_KEYS = ("created", "published", "date", "posted")


def _resolve_clip_dir() -> Path:
    raw = (settings.obsidian_clip_dir or "").strip()
    if not raw:
        raise RuntimeError(
            "OBSIDIAN_CLIP_DIR is empty. Set it in .env (default "
            "'82 P&C Clipped', resolved against OBSIDIAN_VAULT_PATH)."
        )
    p = Path(raw).expanduser()
    if not p.is_absolute():
        if not settings.obsidian_vault_path:
            raise RuntimeError(
                "OBSIDIAN_CLIP_DIR is relative but OBSIDIAN_VAULT_PATH is unset. "
                "Either set the vault path or use an absolute clip dir."
            )
        vault = Path(settings.obsidian_vault_path).expanduser()
        p = vault / raw
        if not p.resolve().is_relative_to(vault.resolve()):
            raise RuntimeError(
                f"OBSIDIAN_CLIP_DIR {raw!r} must be within the vault."
            )
    return p


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body). Empty dict if no frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_yaml = m.group(1)
    body = text[m.end():]
    try:
        parsed = yaml.safe_load(raw_yaml) or {}
        if not isinstance(parsed, dict):
            parsed = {}
    except yaml.YAMLError as exc:
        logger.warning("clipped: bad YAML frontmatter: %s", exc)
        parsed = {}
    return parsed, body


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _parse_published(d: dict[str, Any]) -> datetime | None:
    raw = _first(d, _PUBLISHED_KEYS)
    if not raw:
        return None
    # Accept several common ISO-ish forms; if it parses, great, else None.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _stable_source_id(frontmatter: dict[str, Any], body: str, path: Path) -> str:
    """Pick the most stable id we can: URL > content hash > filename."""
    url = _first(frontmatter, _URL_KEYS)
    if url:
        return url
    h = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"sha1:{h}:{path.name}"


def _build_title(frontmatter: dict[str, Any], body: str, path: Path) -> str:
    t = _first(frontmatter, _TITLE_KEYS)
    if t:
        return t[:300]
    # Fallback: first non-empty line of body, trimmed.
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:300]
    return path.stem


def _stamp_processed(path: Path, when: datetime) -> None:
    """Write `digest_processed_at: <iso>` into the file's YAML frontmatter.

    If frontmatter doesn't exist, prepend a fresh block. Idempotent — if the
    key already exists it's overwritten with the new timestamp.
    """
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    iso = when.replace(microsecond=0).isoformat()
    frontmatter["digest_processed_at"] = iso
    new_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    new_text = f"---\n{new_yaml}\n---\n{body.lstrip(chr(10))}"
    path.write_text(new_text, encoding="utf-8")


class ClippedIngestor(IngestorBase):
    """Walk OBSIDIAN_CLIP_DIR for fresh .md files and ingest each one."""

    name = "clipped"

    def __init__(self, clip_dir: Path | None = None) -> None:
        self.clip_dir = clip_dir or _resolve_clip_dir()
        # Tracks files we successfully fetched this call so run() can stamp
        # them after upsert. Keyed by source_id (matches IngestedItem).
        self._files_to_stamp: dict[str, Path] = {}

    # -- IngestorBase ---------------------------------------------------

    def fetch(self) -> list[IngestedItem]:
        self._files_to_stamp.clear()

        if not self.clip_dir.exists():
            logger.info("clipped: clip dir does not exist yet: %s", self.clip_dir)
            return []

        items: list[IngestedItem] = []
        for path in sorted(self.clip_dir.glob("*.md")):
            try:
                item = self._read_one(path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("clipped: failed to read %s: %s", path.name, exc)
                continue
            if item is None:
                continue
            items.append(item)
            self._files_to_stamp[item.source_id] = path

        logger.info("clipped: scanned %s, %d new file(s)", self.clip_dir, len(items))
        return items

    def run(self, run_type: str = "manual") -> tuple[int, int]:
        # Delegate fetch + upsert + run-log entry to the base class.
        fetched, new = super().run(run_type=run_type)

        # Mark all clipped items pending triage as keep/score=1 — these
        # bypass triage by user intent. Idempotent (only flips the NULLs).
        try:
            updated = db.auto_keep_clipped()
            if updated:
                logger.info("clipped: auto-keep stamped %d row(s)", updated)
        except Exception as exc:  # noqa: BLE001
            logger.exception("clipped: auto-keep failed: %s", exc)

        # Stamp the source files we successfully fetched.
        now = datetime.now(timezone.utc)
        for sid, path in self._files_to_stamp.items():
            try:
                _stamp_processed(path, now)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "clipped: could not stamp %s (%s): %s", path.name, sid, exc
                )

        return fetched, new

    # -- internals ------------------------------------------------------

    def _read_one(self, path: Path) -> IngestedItem | None:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = _split_frontmatter(text)

        # Idempotency: skip files already stamped.
        if frontmatter.get("digest_processed_at"):
            return None

        body = body.strip()
        if not body and not frontmatter:
            return None  # truly empty file — nothing to do

        title = _build_title(frontmatter, body, path)
        url = _first(frontmatter, _URL_KEYS)
        author = _first(frontmatter, _AUTHOR_KEYS)
        published = _parse_published(frontmatter)
        sid = _stable_source_id(frontmatter, body, path)

        return IngestedItem(
            source=self.name,
            source_id=sid,
            title=title,
            url=url,
            author=author,
            content=body,
            published_at=published,
            metadata={
                "clip_path": str(path),
                "frontmatter_keys": sorted(frontmatter.keys()),
            },
        )
