"""Mobile-first daily Brief for the P&C digest — the ~one-screen front page.

Writes `Brief/<date> Brief.md`: the two-axis P&C regime + vital signs, the
day's top leaderboard signals (each with the stake), a regulatory-pressure
flag, and the cross-item connection threads — all linking back into the full
daily note [[<date>]] for depth. Built to read on a phone, so the heavy EKG
panels, per-topic groups and reserve grids stay in the Daily note.

Ported from macro-ai-digest's brief.py and adapted to PC: the regime is the
two-axis market-cycle × cat-load multiplier, top picks come from the leaderboard
(not raw triage score), and the Regulatory Sonar callout is P&C-specific. The
console `digest brief` command (cli.py) is a separate, complementary terminal
view — this writes a vault note.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from digest import db
from digest.obsidian import (
    TOPIC_CALLOUT,
    Paths,
    _chat_link,
    _render_sonar_callout,
    topic_label,
)
from digest_core.obsidian.render import row_get as _row_get, safe as _safe

logger = logging.getLogger(__name__)

TOP_PICKS = 5
PER_TOPIC_CAP = 2
SIGNAL_WINDOW_HOURS = 36   # matches the daily note's leaderboard window


def _clean_title(title: str) -> str:
    """Sanitise a title for inline markdown: no newlines, pipes or link brackets."""
    return (
        title.replace("\n", " ").replace("|", "│")
             .replace("[", "(").replace("]", ")")[:110]
    )


def _top_picks(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Leaderboard winners, at most PER_TOPIC_CAP per topic, up to TOP_PICKS.

    `rows` arrive score-descending from db.top_signal_scores; the per-topic cap
    keeps one busy topic (e.g. regulatory_rate) from crowding out the page.
    """
    picks: list[sqlite3.Row] = []
    per_topic: dict[str, int] = {}
    for row in rows:
        slug = _row_get(row, "topic") or "other"
        if per_topic.get(slug, 0) >= PER_TOPIC_CAP:
            continue
        picks.append(row)
        per_topic[slug] = per_topic.get(slug, 0) + 1
        if len(picks) >= TOP_PICKS:
            break
    return picks


def _render_pick(row: sqlite3.Row) -> list[str]:
    """One top signal: title, slim meta line (with chat link), and the stake."""
    title = _clean_title(_safe(row["title"]) or "(untitled)")
    url = _safe(row["url"])
    slug = _row_get(row, "topic") or "other"
    why = _safe(_row_get(row, "why_it_matters")) or _safe(_row_get(row, "summary"))
    callout = TOPIC_CALLOUT.get(slug, "note")

    heading = f"> [!{callout}]+ [{title}]({url})" if url else f"> [!{callout}]+ {title}"
    meta = [f"`{topic_label(slug)}`"]
    score = _row_get(row, "score")
    if score is not None:
        try:
            meta.append(f"`⭐ {float(score):.2f}`")
        except (TypeError, ValueError):
            pass
    if _safe(row["source"]):
        meta.append(_safe(row["source"]))
    meta.append(_chat_link(row))

    lines = [heading, "> " + " · ".join(meta)]
    if why:
        lines += [">", f"> {why}"]
    return lines


def _regime_evidence(reg: sqlite3.Row) -> str | None:
    """The market-cycle judgment one-liner stored in evidence_json, if any."""
    raw = _row_get(reg, "evidence_json")
    if not raw:
        return None
    try:
        ev = json.loads(raw)
    except (TypeError, ValueError):
        return None
    mj = ev.get("market_judgment") if isinstance(ev, dict) else None
    text = mj.get("evidence") if isinstance(mj, dict) else None
    if text and _is_backend_error(text):
        return None   # don't surface "MLX server not reachable" on the front page
    return text


def _is_backend_error(text: str) -> bool:
    """The regime detector stores the LLM error string as evidence when a backend
    is down; keep that plumbing noise off the rendered note."""
    low = text.lower()
    return low.startswith("backend error") or "not reachable" in low


def _vitals() -> list[str]:
    """Compact vital-signs chips — the EKG essence, distilled for mobile."""
    out: list[str] = []
    try:
        from digest.severity_tape import severity_regime
        sev_z = severity_regime()   # canonical latest blended z (zscore_12m)
        if sev_z is not None:
            out.append(f"severity {float(sev_z):+.2f}σ")
    except Exception:  # noqa: BLE001 — a vitals chip must never break the brief
        pass
    try:
        burden = db.burden_by_state(90)
        if burden:
            out.append(f"top burden {burden[0]['state']}")
    except Exception:  # noqa: BLE001
        pass
    try:
        dv = db.courtlistener_docket_velocity(30)
        if dv:
            out.append(f"docket {float(dv):.1f}/day")
    except Exception:  # noqa: BLE001
        pass
    return out


def _render_regime_and_vitals() -> list[str]:
    """Two-axis regime one-liner + a vitals chip strip."""
    out: list[str] = []
    reg = db.latest_regime_signal()
    if reg:
        cycle = (reg["market_cycle"] or "").replace("_", " ").title()
        cat = (reg["cat_load"] or "").replace("_", " ").title()
        out.append(f"> [!info] 📡 **{cycle}** × **{cat}** · regime ×{reg['multiplier']:.2f}")
        evidence = _regime_evidence(reg)
        if evidence:
            out.append(f"> {evidence}")
        out.append("")
    vitals = _vitals()
    if vitals:
        out.append("**Vitals** · " + "  ·  ".join(vitals))
        out.append("")
    return out


def render_brief_note(date_iso: str) -> tuple[str, int]:
    """Build the Brief markdown. Returns (text, number of top picks)."""
    bundle = db.items_for_publish(date_iso)
    summarized = bundle["summarized"]
    kept_unsum = bundle["kept_unsummarized"]

    since = (datetime.now(timezone.utc) - timedelta(hours=SIGNAL_WINDOW_HOURS)).isoformat()
    try:
        ranked = db.top_signal_scores(limit=50, since_iso=since)
    except Exception as exc:  # noqa: BLE001
        logger.warning("brief: leaderboard fetch failed (%s); omitting top signals", exc)
        ranked = []
    picks = _top_picks(ranked)

    front = {
        "date": date_iso,
        "kind": "digest-brief",
        "top_picks": len(picks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# ⚡ Brief — {date_iso}")
    lines.append("")

    # ── Regime one-liner + vitals ────────────────────────────────────
    try:
        lines.extend(_render_regime_and_vitals())
    except Exception:  # noqa: BLE001
        pass

    n_total = len(summarized) + len(kept_unsum)
    lines.append(
        f"_{len(summarized)} summarized + {len(kept_unsum)} kept today — "
        f"full detail in [[{date_iso}]]._"
    )
    lines.append("")

    if not n_total and not picks:
        lines.append("_No items kept by triage on this date._")
        return "\n".join(lines).rstrip() + "\n", 0

    # ── Regulatory pressure (P&C-specific) ───────────────────────────
    sonar = _render_sonar_callout(list(summarized) + list(kept_unsum))
    if sonar:
        lines.append(sonar)
        lines.append("")

    # ── Top signals ──────────────────────────────────────────────────
    if picks:
        lines.append("## 🎯 Top Signals")
        lines.append("")
        for row in picks:
            lines.extend(_render_pick(row))
            lines.append("")

    # ── Connection threads ───────────────────────────────────────────
    try:
        threads = db.get_connections(date_iso)
    except Exception:  # noqa: BLE001
        threads = []
    if threads:
        lines.append("## 🔗 Connection Threads")
        lines.append("")
        for thread in threads:
            theme = (thread.get("theme") or "").strip()
            insight = (thread.get("insight") or "").strip()
            ids = thread.get("item_ids") or []
            if not theme:
                continue
            lines.append(f"> [!abstract]+ 🔗 {theme}")
            if insight:
                lines.append(f"> {insight}")
            if ids:
                lines.append("> — " + " · ".join(f"`#{i}`" for i in ids))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n", len(picks)


def write_brief_note(date_iso: str, paths: Paths) -> tuple[Path, int]:
    """Write the Brief note. Returns (path_written, num_top_picks).

    Filename is '<date> Brief.md' (not bare '<date>.md') so `[[<date>]]`
    wikilinks keep resolving unambiguously to the Daily note.
    """
    text, n_picks = render_brief_note(date_iso)
    target = paths.brief_dir / f"{date_iso} Brief.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, n_picks
