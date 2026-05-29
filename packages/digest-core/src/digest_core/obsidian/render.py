"""Markdown rendering primitives shared by digest Obsidian writers.

Domain-agnostic helpers: text sanitizing, a confidence badge, wikilink
formatting, see_also parsing, a safe sqlite3.Row accessor, and the
"open this item in a new Claude chat" deep-link builder. Topic
labels/emojis/callouts and the note/section layout stay domain-side.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

# claude.ai tolerates several KB in ?q=, but cap to keep the link tidy.
_CHAT_PROMPT_MAX_CHARS = 4000


def safe(text: str | None) -> str:
    """Strip whitespace; return empty string if None."""
    return (text or "").strip()


def row_get(row: Any, key: str) -> str | None:
    """Safe sqlite3.Row accessor — returns None if the column isn't present."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def confidence_badge(c: str | None, score: float | None = None) -> str:
    """Colored badge for a confidence label; appends `· 0.91` when a score is given."""
    label = {"high": "🟢 high", "medium": "🟡 medium", "low": "🟠 low"}.get(
        (c or "").lower(), "—"
    )
    if score is None:
        return label
    try:
        return f"{label} · {float(score):.2f}"
    except (TypeError, ValueError):
        return label


def wikilink(name: str) -> str:
    """[[Name]] for Obsidian graph navigation. Caller resolves the display name."""
    return f"[[{name}]]"


def parse_see_also(raw: str | None) -> list[str]:
    """Parse a JSON list of see-also phrases; [] on missing/invalid input."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(v) for v in val]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def chat_link(row: Any, digest_name: str = "digest") -> str:
    """Build `[#id](https://claude.ai/new?q=…)` — clicking opens a new Claude
    chat seeded with the item's title/source/url/summary for follow-ups.
    """
    item_id = row["id"]
    title = safe(row_get(row, "title")) or "(untitled)"
    url = safe(row_get(row, "url"))
    source = safe(row_get(row, "source"))
    author = safe(row_get(row, "author"))
    published = safe(row_get(row, "published_at"))[:10]
    summary = safe(row_get(row, "summary"))
    why = safe(row_get(row, "why_it_matters"))

    lines = [
        f"I'd like to dig deeper into this item from my {digest_name} "
        f"(digest item #{item_id}).",
        "",
        f"Title: {title}",
    ]
    if source:
        lines.append(f"Source: {source}")
    if author:
        lines.append(f"Author: {author}")
    if published:
        lines.append(f"Published: {published}")
    if url:
        lines.append(f"URL: {url}")
    if summary:
        lines.append("")
        lines.append(f"Summary: {summary}")
    if why:
        lines.append("")
        lines.append(f"Why it matters: {why}")
    lines.append("")
    lines.append(
        "Please help me explore this further — context, second-order "
        "implications, related reading, or anything else worth knowing. "
        "Start by asking me what angle I want to focus on."
    )

    prompt = "\n".join(lines)
    if len(prompt) > _CHAT_PROMPT_MAX_CHARS:
        prompt = prompt[: _CHAT_PROMPT_MAX_CHARS - 1] + "…"

    encoded = urllib.parse.quote(prompt, safe="")
    return f"[#{item_id}](https://claude.ai/new?q={encoded})"
