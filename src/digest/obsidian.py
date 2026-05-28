"""Phase 3 — Obsidian writer.

Writes triage + summarizer output to an Obsidian vault as Markdown.

Layout:
    <vault>/<digest_dir>/
    ├── Daily/YYYY-MM-DD.md         — daily note, regenerated each run
    ├── Topics/<topic>.md           — topic archives, newest-on-top, YAML index
    └── _meta/Run Log.md            — append-only operations log

Daily notes are idempotent: rewriting the same day's note with the same data
produces byte-identical output. Topic archives use a marker-block strategy so
re-runs upsert items by ID rather than appending duplicates.

Topic display labels (e.g. "AI & Semis") differ from internal slugs
(e.g. "ai_semis"); the mapping is centralized in TOPIC_LABELS.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml

from digest import db, signals
from digest.config import settings

logger = logging.getLogger(__name__)


# ── Topic taxonomy → human labels ──────────────────────────────────────

# Internal slug → display name (file name + heading text)
TOPIC_LABELS: dict[str, str] = {
    "cat_event":             "Catastrophe Events",
    "reinsurance_cycle":     "Reinsurance Cycle",
    "regulatory_rate":       "Regulatory & Rate Filings",
    "underwriting_results":  "Underwriting Results",
    "reserving":             "Reserving",
    "ma_capital":            "M&A & Capital",
    "climate_risk":          "Climate Risk",
    "cyber":                 "Cyber",
    "social_inflation":      "Social Inflation",
    "ai_insurtech":          "AI & Insurtech",
    "distribution":          "Distribution",
    "personal_lines":        "Personal Lines",
    "commercial_specialty":  "Commercial & Specialty",
    "macro_linkage":         "Macro Linkage",
    "rates_cost_of_capital": "Rates & Cost of Capital",
    "supply_chain":          "Supply Chain",
    "analytics_modeling":    "Analytics & Modeling",
}

# Maps topic slug → Obsidian callout type (colour-coded by urgency/category)
TOPIC_CALLOUT: dict[str, str] = {
    "cat_event":             "danger",
    "reinsurance_cycle":     "warning",
    "regulatory_rate":       "warning",
    "underwriting_results":  "info",
    "reserving":             "info",
    "ma_capital":            "info",
    "climate_risk":          "danger",
    "cyber":                 "danger",
    "social_inflation":      "warning",
    "ai_insurtech":          "tip",
    "distribution":          "example",
    "personal_lines":        "example",
    "commercial_specialty":  "example",
    "macro_linkage":         "abstract",
    "rates_cost_of_capital": "abstract",
    "supply_chain":          "note",
    "analytics_modeling":    "success",
}

TOPIC_EMOJI: dict[str, str] = {
    "cat_event":             "🌀",
    "reinsurance_cycle":     "🔁",
    "regulatory_rate":       "📜",
    "underwriting_results":  "📊",
    "reserving":             "💼",
    "ma_capital":            "🤝",
    "climate_risk":          "🌎",
    "cyber":                 "🔐",
    "social_inflation":      "⚖️",
    "ai_insurtech":          "🤖",
    "distribution":          "🛒",
    "personal_lines":        "🚗",
    "commercial_specialty":  "🏭",
    "macro_linkage":         "🔗",
    "rates_cost_of_capital": "💵",
    "supply_chain":          "📦",
    "analytics_modeling":    "🧮",
}

# Display order in daily notes — leads with most time-sensitive, ends with research
TOPIC_ORDER = [
    "cat_event",
    "reinsurance_cycle",
    "regulatory_rate",
    "underwriting_results",
    "reserving",
    "ma_capital",
    "social_inflation",
    "cyber",
    "climate_risk",
    "personal_lines",
    "commercial_specialty",
    "distribution",
    "ai_insurtech",
    "rates_cost_of_capital",
    "supply_chain",
    "macro_linkage",
    "analytics_modeling",
]


def topic_label(slug: str) -> str:
    return TOPIC_LABELS.get(slug, slug.replace("_", " ").title())


def topic_filename(slug: str) -> str:
    """Topic archive filename — uses display label so the wikilink reads naturally."""
    return f"{topic_label(slug)}.md"


# ── Path resolution ────────────────────────────────────────────────────


@dataclass
class Paths:
    vault: Path
    digest_root: Path
    daily_dir: Path
    topics_dir: Path
    weekly_dir: Path
    meta_dir: Path

    @classmethod
    def resolve(cls) -> "Paths":
        if not settings.obsidian_vault_path:
            raise RuntimeError(
                "OBSIDIAN_VAULT_PATH is not set in .env. "
                "Set it to the absolute vault path (e.g. "
                "'/Users/you/Documents/Obsidian Vault/vault_build')."
            )
        vault = Path(settings.obsidian_vault_path).expanduser()
        if not vault.exists():
            raise RuntimeError(f"Obsidian vault not found at: {vault}")

        digest_root = vault / settings.obsidian_digest_dir
        if not digest_root.resolve().is_relative_to(vault.resolve()):
            raise RuntimeError(
                f"OBSIDIAN_DIGEST_DIR {settings.obsidian_digest_dir!r} must be within the vault."
            )
        return cls(
            vault=vault,
            digest_root=digest_root,
            daily_dir=digest_root / "Daily",
            topics_dir=digest_root / "Topics",
            weekly_dir=digest_root / "Weekly",
            meta_dir=digest_root / "_meta",
        )

    def ensure(self) -> None:
        for p in (self.digest_root, self.daily_dir, self.topics_dir, self.weekly_dir, self.meta_dir):
            p.mkdir(parents=True, exist_ok=True)


# ── Markdown rendering ─────────────────────────────────────────────────


def _safe(text: str | None) -> str:
    """Strip whitespace; return empty string if None."""
    return (text or "").strip()


def _wikilink(topic_slug: str) -> str:
    """[[Topic Name]] for graph navigation."""
    return f"[[{topic_label(topic_slug)}]]"


def _confidence_badge(c: str | None, score: float | None = None) -> str:
    """Colored badge for the summarizer's confidence label.

    If a numeric `score` is provided (typically the row's `triage_score`),
    it's appended to two decimal places — e.g. `🟢 high · 0.91`.
    """
    label = {"high": "🟢 high", "medium": "🟡 medium", "low": "🟠 low"}.get(
        (c or "").lower(), "—"
    )
    if score is None:
        return label
    try:
        return f"{label} · {float(score):.2f}"
    except (TypeError, ValueError):
        return label


# Max prompt length kept comfortably under typical URL length limits.
# claude.ai tolerates several KB in ?q=, but we cap to keep the link tidy.
_CHAT_PROMPT_MAX_CHARS = 4000


def _row_get(row: sqlite3.Row, key: str) -> str | None:
    """Safe sqlite3.Row accessor — returns None if column isn't present."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _chat_link(row: sqlite3.Row) -> str:
    """Build `[#id](https://claude.ai/new?q=…)` — clicking opens a new
    Claude chat seeded with this item's title, source, URL, and summary
    so you can immediately ask follow-up questions about it.
    """
    item_id = row["id"]
    title = _safe(_row_get(row, "title")) or "(untitled)"
    url = _safe(_row_get(row, "url"))
    source = _safe(_row_get(row, "source"))
    author = _safe(_row_get(row, "author"))
    published = _safe(_row_get(row, "published_at"))[:10]
    summary = _safe(_row_get(row, "summary"))
    why = _safe(_row_get(row, "why_it_matters"))

    lines = [
        "I'd like to dig deeper into this item from my macro/AI digest "
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


def _parse_see_also(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(v) for v in val]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _render_summary_item(row: sqlite3.Row) -> str:
    """Render one summarized item as a topic-coloured Obsidian callout block."""
    title      = _safe(row["title"]) or "(untitled)"
    url        = _safe(row["url"])
    summary    = _safe(row["summary"])
    why        = _safe(row["why_it_matters"])
    confidence = row["confidence"]
    score      = _row_get(row, "triage_score")
    see_also   = _parse_see_also(row["see_also"])
    source     = _safe(row["source"])
    author     = _safe(row["author"])
    published  = _safe(row["published_at"])[:10]
    topic_slug = _safe(_row_get(row, "topic")) or "other"

    callout_type  = TOPIC_CALLOUT.get(topic_slug, "note")
    # Sanitise title: strip pipes (break tables) and square brackets (break link syntax)
    title_display = (
        title.replace("\n", " ").replace("|", "│")
             .replace("[", "(").replace("]", ")")[:110]
    )
    heading = (
        f"> [!{callout_type}]+ [{title_display}]({url})" if url
        else f"> [!{callout_type}]+ {title_display}"
    )

    meta_parts = [f"`{topic_label(topic_slug)}`"]
    if confidence:
        meta_parts.append(f"`{confidence}`")
    if score is not None:
        try:
            meta_parts.append(f"`⭐ {float(score):.2f}`")
        except (TypeError, ValueError):
            pass
    if source:
        meta_parts.append(source)
    if author:
        meta_parts.append(author)
    if published:
        meta_parts.append(published)
    meta_parts.append(_chat_link(row))
    meta_line = "> " + " · ".join(meta_parts)

    lines = [
        heading,
        meta_line,
        ">",
        f"> {summary}" if summary else "> *(no summary)*",
    ]
    if why:
        lines += [">", f"> **Why it matters**: {why}"]
    if see_also:
        lines += [">", "> **See also**: " + " · ".join(f"`{s}`" for s in see_also[:3])]

    return "\n".join(lines)


def _render_sonar_callout(rows: list) -> str | None:
    """One-liner callout when a high-intensity regulatory_rate item lands.

    Wave 2 lite: just count + list titles. Wave 3's full Regulatory Sonar
    detector replaces this with the per-state burden-pressure index callout
    when trend-fires fire.
    """
    high = [
        r for r in rows
        if (_row_get(r, "topic") == "regulatory_rate"
            and (_row_get(r, "burden_intensity") or "").lower() == "high")
    ]
    if not high:
        return None
    n = len(high)
    word = "item" if n == 1 else "items"
    lines = [
        f"> [!warning]+ 📡 Regulatory pressure today: {n} high-intensity oversight {word}",
    ]
    for r in high[:5]:
        title = (_safe(r["title"]) or "(untitled)")[:100]
        url   = _safe(r["url"])
        link  = f"[{title}]({url})" if url else title
        direction = (_row_get(r, "burden_direction") or "?").lower()
        lines.append(f"> - **{direction}** — {link}")
    return "\n".join(lines)


def _render_regime_callout(regime) -> str:
    """One-block summary of the current PC two-axis regime."""
    cycle = regime.market_cycle.replace("_", " ").title()
    cat   = regime.cat_load.replace("_", " ").title()
    src   = regime.source
    note  = "" if src == "detector" else f" _(source: {src})_"
    lines = [
        "> [!abstract]+ 📡 P&C Regime",
        f"> **Market cycle:** {cycle}  ·  **CAT load:** {cat}  ·  **Multiplier:** ×{regime.multiplier:.2f}{note}",
    ]
    market_judgment = (regime.evidence or {}).get("market_judgment", {})
    evidence_txt = market_judgment.get("evidence") if isinstance(market_judgment, dict) else None
    if evidence_txt:
        lines.append(f"> _{evidence_txt}_")
    return "\n".join(lines)


def _render_leaderboard_item(row: sqlite3.Row, rank: int) -> str:
    title = _safe(row["title"]) or "(untitled)"
    url   = _safe(row["url"])
    slug  = _safe(_row_get(row, "topic")) or "other"
    score = row["score"] if "score" in row.keys() else None
    title_display = (
        title.replace("\n", " ").replace("|", "│")
             .replace("[", "(").replace("]", ")")[:110]
    )
    link = f"[{title_display}]({url})" if url else title_display
    score_part = f"`⭐ {float(score):.2f}`" if score is not None else ""
    badge = signals.tier_badge(float(score)) if score is not None else ""
    badge_part = f"{badge}  ·  " if badge else ""
    return (
        f"{rank}. {badge_part}**{link}**  ·  `{topic_label(slug)}`  ·  "
        f"{score_part}  ·  {_chat_link(row)}"
    )


def _render_leaderboard_section(rows: list, header: str, intro: str | None = None) -> list[str]:
    if not rows:
        return []
    out = [f"## {header}", ""]
    if intro:
        out.append(f"_{intro}_")
        out.append("")
    for i, row in enumerate(rows, 1):
        out.append(_render_leaderboard_item(row, i))
    out.append("")
    return out


def _render_source_quality_table(rows: list) -> list[str]:
    if not rows:
        return []
    out = ["## 📊 Signal Quality by Source", "",
           "_Which feeds earned their keep this week — average leaderboard score by source._", "",
           "| Source | Items | Avg score | Max score |",
           "|---|---:|---:|---:|"]
    for r in rows:
        try:
            avg = float(r["avg_score"])
            mx  = float(r["max_score"])
        except (TypeError, ValueError):
            continue
        out.append(f"| {r['source']} | {r['n']} | {avg:.2f} | {mx:.2f} |")
    out.append("")
    return out


def _render_unsummarized_item(row: sqlite3.Row) -> str:
    """One-line bullet for kept-but-not-summarized items."""
    title  = _safe(row["title"]) or "(untitled)"
    url    = _safe(row["url"])
    source = _safe(row["source"]) or "?"
    score  = row["triage_score"]
    link   = f"[{title}]({url})" if url else title
    parts  = [f"- {link}", f"*{source}*"]
    if score is not None:
        parts.append(f"`⭐ {score:.2f}`")
    parts.append(_chat_link(row))
    return "  ·  ".join(parts)


# ── Daily note ─────────────────────────────────────────────────────────


def _group_by_topic(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Group summarized rows by topic slug, preserving sort order within each."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["topic"] or "other", []).append(row)
    return groups


def render_daily_note(
    date_iso: str,
    market_snapshot_md: str = "",
    regime=None,
    top_signals: list | None = None,
) -> tuple[str, list[int]]:
    """Build the markdown for a daily note. Returns (text, list of item IDs touched).

    market_snapshot_md: pre-rendered ## Market Snapshot section (Obsidian Charts
        blocks + PNG embed). Pass empty string to omit the section.
    regime:        optional RegimeSignal — if provided, renders a callout block
        near the top of the daily note.
    top_signals:   optional list of rows (top_signal_scores output) — if provided,
        renders a "Top Signals" leaderboard section above the topic groups.
    """
    bundle = db.items_for_publish(date_iso)
    summarized = bundle["summarized"]
    kept_unsum = bundle["kept_unsummarized"]
    item_ids = [r["id"] for r in summarized] + [r["id"] for r in kept_unsum]

    # User-clipped items (source='clipped') get their own headline section
    # above the auto-curated topic groups. They still carry a topic (so the
    # topic archives pick them up too) but don't double-render in the daily.
    clipped_rows = [r for r in summarized if (r["source"] or "") == "clipped"]
    auto_rows    = [r for r in summarized if (r["source"] or "") != "clipped"]

    front = {
        "date": date_iso,
        "kind": "digest-daily",
        "summarized_count": len(summarized),
        "clipped_count": len(clipped_rows),
        "kept_unsummarized_count": len(kept_unsum),
        "topics": sorted({r["topic"] or "other" for r in summarized}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# Digest — {date_iso}")
    lines.append("")

    # ── Regime callout (Wave 2) ──────────────────────────────────────────
    if regime is not None:
        lines.append(_render_regime_callout(regime))
        lines.append("")

    # ── Regulatory Sonar lite callout (Wave 2 lite) ──────────────────
    sonar_md = _render_sonar_callout(list(summarized) + list(kept_unsum))
    if sonar_md:
        lines.append(sonar_md)
        lines.append("")

    # ── Market Snapshot (charts) ─────────────────────────────────────────
    if market_snapshot_md:
        lines.append("## Market Snapshot")
        lines.append("")
        lines.append(market_snapshot_md)
        lines.append("")

    if not summarized and not kept_unsum:
        lines.append("_No items kept by triage on this date._")
        lines.append("")
        return "\n".join(lines), item_ids

    # ── Connection threads (cross-item synthesis) ─────────────────────
    threads = db.get_connections(date_iso)
    if threads:
        lines.append("## 🔗 Connection Threads")
        lines.append("")
        lines.append(
            "_Cross-item patterns identified by Claude. Click a `#id` link to open a seeded chat._"
        )
        lines.append("")
        for thread in threads:
            theme   = (thread.get("theme") or "").strip()
            insight = (thread.get("insight") or "").strip()
            ids     = thread.get("item_ids") or []
            id_refs = " · ".join(f"`#{i}`" for i in ids)
            if theme:
                lines.append(f"> [!abstract]+ 🔗 {theme}")
                if id_refs:
                    lines.append(f"> **Items**: {id_refs}")
                if insight:
                    lines.append(">")
                    lines.append(f"> {insight}")
                lines.append("")

    # ── Top signals leaderboard (Wave 2) ─────────────────────────────
    if top_signals:
        lines.extend(_render_leaderboard_section(
            top_signals,
            header="🏆 Top Signals",
            intro="Highest-scored items by leaderboard formula (source × regime × topic × recency × materiality).",
        ))

    # ── Clipped-for-investigation section (always on top) ────────────
    if clipped_rows:
        lines.append("## 📎 Clipped for Investigation")
        lines.append("")
        lines.append(
            "_Posts you flagged from `77_Claude_Investigate` — each `#id` link opens a Claude chat seeded with the context._"
        )
        lines.append("")
        for row in clipped_rows:
            lines.append(_render_summary_item(row))
            lines.append("")

    # ── Auto-curated summarized section, grouped by topic ────────────
    groups = _group_by_topic(auto_rows)
    if auto_rows:
        lines.append("## 📑 Summarized")
        lines.append("")
        for slug in TOPIC_ORDER:
            rows = groups.get(slug)
            if not rows:
                continue
            emoji = TOPIC_EMOJI.get(slug, "📌")
            n     = len(rows)
            lines.append(f"## {emoji} {topic_label(slug)}  ·  {_wikilink(slug)}  ·  {n} item{'s' if n > 1 else ''}")
            lines.append("")
            for row in rows:
                lines.append(_render_summary_item(row))
                lines.append("")
        # Any topics not in canonical order (shouldn't normally happen)
        leftover = [s for s in groups if s not in TOPIC_ORDER]
        for slug in sorted(leftover):
            emoji = TOPIC_EMOJI.get(slug, "📌")
            n     = len(groups[slug])
            lines.append(f"## {emoji} {topic_label(slug)}  ·  {_wikilink(slug)}  ·  {n} item{'s' if n > 1 else ''}")
            lines.append("")
            for row in groups[slug]:
                lines.append(_render_summary_item(row))
                lines.append("")

    # ── Kept-unsummarized section ────────────────────────────────────
    if kept_unsum:
        lines.append("## 📋 Kept — Not Summarized")
        lines.append("")
        lines.append(
            "_Passed triage but exceeded the summarizer cap. Sorted by triage score descending._"
        )
        lines.append("")
        for row in kept_unsum:
            lines.append(_render_unsummarized_item(row))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n", item_ids


def write_daily_note(date_iso: str, paths: Paths) -> tuple[Path, int]:
    """Write the daily note. Returns (path_written, num_items)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
        raise ValueError(f"date_iso must be YYYY-MM-DD, got: {date_iso!r}")

    # Wave 2: pull regime + top-5 leaderboard so the daily note has the
    # P&C two-axis multiplier in plain sight.
    try:
        from digest.regime import current_regime
        regime = current_regime()
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily: regime lookup failed (%s); rendering without callout", exc)
        regime = None

    try:
        # Top signals ingested in last 36h — covers both AM and PM runs without
        # double-counting prior days' winners.
        top_signals = db.top_signal_scores(
            limit=5,
            since_iso=(datetime.now(timezone.utc) - timedelta(hours=36)).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily: leaderboard fetch failed (%s); omitting top signals", exc)
        top_signals = []

    text, item_ids = render_daily_note(
        date_iso,
        market_snapshot_md="",
        regime=regime,
        top_signals=top_signals,
    )
    target = paths.daily_dir / f"{date_iso}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, len(item_ids)


# ── Topic archives (newest-on-top, YAML index) ─────────────────────────

# Marker syntax: each item in a topic archive is wrapped in HTML comments
# with its DB id, so re-runs upsert by ID rather than duplicating.
ITEM_BEGIN = "<!-- digest:item:{id}:begin -->"
ITEM_END   = "<!-- digest:item:{id}:end -->"
INDEX_BEGIN = "<!-- digest:index:begin -->"
INDEX_END   = "<!-- digest:index:end -->"


def _render_topic_item(row: sqlite3.Row, topic_slug: str) -> str:
    """Render one item for a topic archive as a callout block, wrapped in idempotency markers."""
    title      = _safe(row["title"]) or "(untitled)"
    url        = _safe(row["url"])
    summary    = _safe(row["summary"])
    why        = _safe(row["why_it_matters"])
    confidence = row["confidence"]
    score      = _row_get(row, "triage_score")
    see_also   = _parse_see_also(row["see_also"])
    source     = _safe(row["source"])
    author     = _safe(row["author"])
    ingested   = _safe(row["ingested_at"])[:10]
    published  = _safe(row["published_at"])[:10]

    callout_type  = TOPIC_CALLOUT.get(topic_slug, "note")
    title_display = (
        title.replace("\n", " ").replace("|", "│")
             .replace("[", "(").replace("]", ")")[:110]
    )
    daily_link = f"[[{ingested}]]" if ingested else ""
    heading = (
        f"> [!{callout_type}]+ [{title_display}]({url})" if url
        else f"> [!{callout_type}]+ {title_display}"
    )

    meta_parts = []
    if source:
        meta_parts.append(source)
    if author:
        meta_parts.append(author)
    if published:
        meta_parts.append(published)
    if daily_link:
        meta_parts.append(f"in {daily_link}")
    if confidence:
        meta_parts.append(f"`{confidence}`")
    if score is not None:
        try:
            meta_parts.append(f"`⭐ {float(score):.2f}`")
        except (TypeError, ValueError):
            pass
    meta_parts.append(_chat_link(row))
    meta_line = "> " + " · ".join(meta_parts)

    parts = [
        ITEM_BEGIN.format(id=row["id"]),
        heading,
        meta_line,
        ">",
        f"> {summary}" if summary else "> *(no summary)*",
    ]
    if why:
        parts += [">", f"> **Why it matters**: {why}"]
    if see_also:
        parts += [">", "> **See also**: " + " · ".join(f"`{s}`" for s in see_also[:3])]
    parts.append(ITEM_END.format(id=row["id"]))
    return "\n".join(p for p in parts if p is not None)


def _build_index_block(rows: Iterable[sqlite3.Row]) -> str:
    """YAML index block listing every dated entry in this topic archive."""
    entries = []
    for row in rows:
        entries.append({
            "id": row["id"],
            "date": _safe(row["ingested_at"])[:10],
            "title": (_safe(row["title"]) or "(untitled)")[:120],
            "source": _safe(row["source"]),
        })
    payload = {"entries": entries}
    yaml_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).strip()
    return f"{INDEX_BEGIN}\n```yaml\n{yaml_text}\n```\n{INDEX_END}"


def render_topic_archive(topic_slug: str) -> tuple[str, list[int]]:
    """Render the full topic archive markdown. Returns (text, item_ids)."""
    rows = db.items_by_topic(topic_slug)
    item_ids = [r["id"] for r in rows]

    front = {
        "topic": topic_slug,
        "label": topic_label(topic_slug),
        "kind": "digest-topic-archive",
        "item_count": len(rows),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    emoji = TOPIC_EMOJI.get(topic_slug, "📌")
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# {emoji} {topic_label(topic_slug)}")
    lines.append("")
    lines.append("_Newest first. Each entry is upserted by ID; re-runs are idempotent._")
    lines.append("")
    lines.append("## Entries")
    lines.append("")

    for row in rows:
        lines.append(_render_topic_item(row, topic_slug))
        lines.append("")

    lines.append("## Index")
    lines.append("")
    lines.append(_build_index_block(rows))
    lines.append("")

    return "\n".join(lines).rstrip() + "\n", item_ids


def write_topic_archive(topic_slug: str, paths: Paths) -> tuple[Path, int]:
    text, item_ids = render_topic_archive(topic_slug)
    target = paths.topics_dir / topic_filename(topic_slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, len(item_ids)


# ── Run log ────────────────────────────────────────────────────────────


RUN_LOG_HEADER = "# Digest Run Log\n\n_Append-only operations log._\n\n"


def append_run_log(paths: Paths, message: str) -> None:
    target = paths.meta_dir / "Run Log.md"
    if not target.exists():
        target.write_text(RUN_LOG_HEADER, encoding="utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with target.open("a", encoding="utf-8") as fp:
        fp.write(f"- `{ts}` — {message}\n")


# ── Public entry point ────────────────────────────────────────────────


def publish(date_iso: str | None = None) -> dict[str, int | str]:
    """Write daily note + all topic archives. Stamp items as published.

    If date_iso is None, uses today (UTC).
    """
    paths = Paths.resolve()
    paths.ensure()

    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
        raise ValueError(f"date_iso must be YYYY-MM-DD, got: {date_iso!r}")

    # Daily note
    daily_path, daily_count = write_daily_note(date_iso, paths)
    logger.info("obsidian: wrote daily %s (%d items)", daily_path.name, daily_count)

    # Topic archives — only those with summaries
    topic_results: list[tuple[str, Path, int]] = []
    for slug in db.topics_with_summaries():
        path, count = write_topic_archive(slug, paths)
        topic_results.append((slug, path, count))
        logger.info("obsidian: wrote topic %s (%d items)", path.name, count)

    # Stamp items in DB so we know what's been pushed (informational only)
    bundle = db.items_for_publish(date_iso)
    stamped = [r["id"] for r in bundle["summarized"]] + [
        r["id"] for r in bundle["kept_unsummarized"]
    ]
    db.mark_published(stamped)

    append_run_log(
        paths,
        f"published {date_iso}: {daily_count} items in daily, "
        f"{len(topic_results)} topic archives refreshed",
    )

    return {
        "date": date_iso,
        "daily_path": str(daily_path),
        "daily_items": daily_count,
        "topic_archives": len(topic_results),
        "items_stamped": len(stamped),
    }


# ── Weekly note ────────────────────────────────────────────────────────


def _week_bounds(ref_date: date) -> tuple[date, date]:
    """Return (monday, sunday) for the ISO week containing ref_date."""
    monday = ref_date - timedelta(days=ref_date.weekday())
    return monday, monday + timedelta(days=6)


def render_weekly_note(
    week_iso: str,
    monday: date,
    sunday: date,
    synthesis: dict,
    rows: list[sqlite3.Row],
    regime_md: str | None = None,
    top_signals: list | None = None,
    source_quality: list | None = None,
) -> str:
    """Build the Markdown for a weekly digest note."""
    period = f"{monday.isoformat()} – {sunday.isoformat()}"
    front = {
        "week": week_iso,
        "period": period,
        "kind": "digest-weekly",
        "item_count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# Weekly Digest — {week_iso}")
    lines.append(f"_{period}_")
    lines.append("")
    if regime_md:
        lines.append(regime_md)
        lines.append("")

    if not rows:
        lines.append("_No summarized items this week._")
        return "\n".join(lines).rstrip() + "\n"

    # ── Top 15 signals of the week (Wave 2) ──────────────────────────
    if top_signals:
        lines.extend(_render_leaderboard_section(
            top_signals,
            header="🏆 Top 15 Signals This Week",
            intro="Ranked by leaderboard formula across all items ingested this week.",
        ))

    # ── Per-source signal quality (Wave 2) ───────────────────────────
    if source_quality:
        lines.extend(_render_source_quality_table(source_quality))

    # ── Themes ──────────────────────────────────────────────────────
    themes = synthesis.get("themes") or []
    if themes:
        lines.append("## 🎯 Themes of the Week")
        lines.append("")
        for i, t in enumerate(themes, 1):
            title = (t.get("title") or "").strip()
            desc  = (t.get("description") or "").strip()
            lines.append(f"> [!tip]+ 🎯 Theme {i}: {title}")
            if desc:
                lines.append(f"> {desc}")
            lines.append("")

    # ── Must-reads ──────────────────────────────────────────────────
    must_reads = synthesis.get("must_reads") or []
    if must_reads:
        row_by_id = {r["id"]: r for r in rows}
        lines.append("## 📌 Must-Reads")
        lines.append("")
        for mr in must_reads:
            item_id = mr.get("item_id")
            reason  = (mr.get("reason") or "").strip()
            row     = row_by_id.get(item_id)
            if row:
                title       = _safe(row["title"]) or "(untitled)"
                url         = _safe(row["url"])
                slug        = row["topic"] or "other"
                link        = f"[{title}]({url})" if url else title
                topic_disp  = topic_label(slug)
                callout_t   = TOPIC_CALLOUT.get(slug, "note")
                lines.append(f"> [!{callout_t}]+ 📌 {link}")
                lines.append(f"> `{topic_disp}` — {reason}")
            else:
                lines.append(f"> [!note]+ 📌 Item #{item_id}")
                lines.append(f"> {reason}")
            lines.append("")

    # ── Contrarian signal ────────────────────────────────────────────
    contrarian = (synthesis.get("contrarian_signal") or "").strip()
    if contrarian:
        lines.append("## ⚠️ Contrarian Signal")
        lines.append("")
        lines.append("> [!danger] ⚠️ Contrarian Signal")
        lines.append(f"> {contrarian}")
        lines.append("")

    # ── Carrier of the Week (P&C synthesis) ──────────────────────────
    carrier = synthesis.get("carrier_of_the_week") or {}
    ticker = (carrier.get("ticker") or "").strip()
    rationale = (carrier.get("rationale") or "").strip()
    if ticker and rationale:
        lines.append("## 🏢 Carrier of the Week")
        lines.append("")
        lines.append(f"> [!example]+ 🏢 {ticker}")
        lines.append(f"> {rationale}")
        lines.append("")

    # ── Burden-trend states (Regulatory Sonar lite synthesis) ────────
    burden_states = synthesis.get("burden_trend_states") or []
    if burden_states:
        lines.append("## 🏛️ Regulatory Burden Trends")
        lines.append("")
        for bt in burden_states:
            state = (bt.get("state") or "").strip()
            direction = (bt.get("direction") or "").strip()
            note = (bt.get("note") or "").strip()
            if not (state and note):
                continue
            arrow = {"increasing": "↑", "decreasing": "↓", "neutral": "→"}.get(direction, "•")
            cl = "warning" if direction == "increasing" else ("success" if direction == "decreasing" else "info")
            lines.append(f"> [!{cl}]+ 🏛️ {state} burden {arrow} {direction or '—'}")
            lines.append(f"> {note}")
            lines.append("")

    # ── Inflation pulse (loss-cost driver synthesis) ─────────────────
    inflation_pulse = (synthesis.get("inflation_pulse") or "").strip()
    if inflation_pulse:
        lines.append("## 📈 Inflation Pulse")
        lines.append("")
        lines.append("> [!abstract] 📈 Inflation Pulse")
        lines.append(f"> {inflation_pulse}")
        lines.append("")

    # ── All items grouped by topic ───────────────────────────────────
    groups = _group_by_topic(list(rows))
    lines.append("## 📑 All Items This Week")
    lines.append("")
    for slug in TOPIC_ORDER:
        topic_rows = groups.get(slug)
        if not topic_rows:
            continue
        emoji = TOPIC_EMOJI.get(slug, "📌")
        n     = len(topic_rows)
        lines.append(f"### {emoji} {topic_label(slug)}  ·  {_wikilink(slug)}  ·  {n} item{'s' if n > 1 else ''}")
        lines.append("")
        for row in topic_rows:
            lines.append(_render_summary_item(row))
            lines.append("")
    leftover = [s for s in groups if s not in TOPIC_ORDER]
    for slug in sorted(leftover):
        emoji = TOPIC_EMOJI.get(slug, "📌")
        n     = len(groups[slug])
        lines.append(f"### {emoji} {topic_label(slug)}  ·  {n} item{'s' if n > 1 else ''}")
        lines.append("")
        for row in groups[slug]:
            lines.append(_render_summary_item(row))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def publish_weekly(date_iso: str | None = None) -> dict:
    """Generate and write the weekly digest note for the week containing date_iso.

    Wave 1: items grouped by topic, no MLX synthesis (themes/must-reads/etc.
    arrive in Wave 2 alongside regime detection).

    Returns: dict with keys: week, path, item_count, theme_count.
    """
    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ref = date.fromisoformat(date_iso)
    monday, sunday = _week_bounds(ref)
    week_iso = monday.strftime("%G-W%V")

    rows = db.items_for_week(monday.isoformat(), sunday.isoformat())
    logger.info("weekly: %d items for %s", len(rows), week_iso)

    paths = Paths.resolve()
    paths.ensure()

    # Wave 2: regime + top-15 leaderboard + per-source quality
    regime_md = None
    try:
        from digest.regime import current_regime
        regime_md = _render_regime_callout(current_regime())
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly: regime lookup failed (%s)", exc)

    try:
        top_signals = db.top_signal_scores(limit=15, since_iso=monday.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly: leaderboard fetch failed (%s)", exc)
        top_signals = []

    try:
        source_quality = db.signal_quality_by_source(since_iso=monday.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly: source-quality fetch failed (%s)", exc)
        source_quality = []

    # Wave 3 Phase 2 item 5: weekly synthesis port. Calls Claude with a P&C
    # reader persona to produce themes, must-reads, contrarian, carrier-of-the-
    # week, burden-trend states, and inflation pulse. Returns {} on failure;
    # render_weekly_note treats missing keys as omitted sections.
    synthesis: dict = {}
    try:
        from digest.weekly import synthesize_week
        week_label = f"{week_iso} ({monday.isoformat()} – {sunday.isoformat()})"
        regime_framing = regime_md.strip() if regime_md else ""
        synthesis = synthesize_week(rows, week_label, regime_framing=regime_framing) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly: synthesis failed (%s) — rendering without it", exc)
        synthesis = {}

    text = render_weekly_note(
        week_iso, monday, sunday, synthesis, rows,
        regime_md=regime_md,
        top_signals=top_signals,
        source_quality=source_quality,
    )
    target = paths.weekly_dir / f"{week_iso}.md"
    target.write_text(text, encoding="utf-8")
    logger.info("obsidian: wrote weekly %s (%d items)", target.name, len(rows))

    append_run_log(paths, f"weekly {week_iso}: {len(rows)} items")

    return {
        "week": week_iso,
        "path": str(target),
        "item_count": len(rows),
        "theme_count": len(synthesis.get("themes") or []),
    }
