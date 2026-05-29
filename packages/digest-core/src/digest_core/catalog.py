"""Source catalog — the payoff of the ingestor registry, made visible.

`digest sources` shows every registered ingestor with a live pulse: its status
(active / stale / never-run / error / import-failed), when it last ran, a 7-day
sparkline of newly-ingested items, and its lifetime item count. Because sources
self-register (see `ingest.registry`), dropping a new ingestor file makes it
appear here automatically — "never run" until its first ingest, then it starts
breathing. Sources whose module won't import (missing optional dep) are listed
explicitly instead of silently vanishing.

Data collection is dependency-free; rendering imports `rich` lazily (both
domains already depend on it), so importing this module never requires rich.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from digest_core.ingest import discover, import_failures

_SPARK_TICKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int], width: int = 7) -> str:
    """Render counts as a unicode block sparkline, right-aligned to `width`.

    All-zero (or empty) renders as flat lowest ticks so an idle source still
    shows a baseline rather than blank space.
    """
    vals = list(values)[-width:]
    vals = [0] * (width - len(vals)) + vals  # left-pad so today is right-most
    peak = max(vals)
    if peak <= 0:
        return _SPARK_TICKS[0] * width
    out = []
    for v in vals:
        if v <= 0:
            out.append(_SPARK_TICKS[0])
        else:
            idx = round(v / peak * (len(_SPARK_TICKS) - 1))
            out.append(_SPARK_TICKS[idx])
    return "".join(out)


@dataclass
class SourceSummary:
    """One row of the catalog."""

    name: str
    doc: str = ""
    tags: tuple[str, ...] = ()
    total_items: int = 0
    last_run: str | None = None          # ISO timestamp of most recent run_log row
    last_fetched: int = 0
    last_new: int = 0
    last_status: str = ""                # 'ok' | 'error' | ''
    daily_new: list[int] = field(default_factory=list)   # oldest→newest, len=window
    import_error: str | None = None

    @property
    def status(self) -> str:
        """Coarse health label derived from import state + recency + last status."""
        if self.import_error:
            return "import-failed"
        if self.last_run is None:
            return "never-run"
        if self.last_status == "error":
            return "error"
        return "active" if _age_hours(self.last_run) <= 36 else "stale"


def _age_hours(iso: str) -> float:
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return 1e9
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def collect(
    get_conn: Callable[[], Any],
    package: str,
    window_days: int = 7,
) -> tuple[list[SourceSummary], dict[str, str]]:
    """Build the catalog: discover registered ingestors, then enrich each with
    run_log recency + a `window_days` sparkline + lifetime item counts.

    `get_conn` is a domain connection-cm factory (e.g. `digest.db.get_conn`);
    queries only touch the framework-owned `items` and `run_log` tables.
    Returns (summaries ordered by registry order, import_failures).
    """
    specs = discover(package)
    failures = import_failures()

    summaries: dict[str, SourceSummary] = {
        name: SourceSummary(name=name, doc=spec.doc, tags=spec.tags)
        for name, spec in specs.items()
    }
    # Represent import-failed modules too, keyed by their module leaf name.
    for mod_path, err in failures.items():
        leaf = mod_path.rsplit(".", 1)[-1]
        summaries.setdefault(leaf, SourceSummary(name=leaf))
        summaries[leaf].import_error = err

    with get_conn() as conn:
        for row in conn.execute(
            "SELECT source, COUNT(*) AS n FROM items GROUP BY source"
        ).fetchall():
            if row["source"] in summaries:
                summaries[row["source"]].total_items = row["n"]

        # Most-recent run per source (max id == latest insert).
        for row in conn.execute(
            """SELECT source, run_at, items_fetched, items_new, status
               FROM run_log
               WHERE id IN (SELECT MAX(id) FROM run_log GROUP BY source)"""
        ).fetchall():
            s = summaries.get(row["source"])
            if s:
                s.last_run = row["run_at"]
                s.last_fetched = row["items_fetched"] or 0
                s.last_new = row["items_new"] or 0
                s.last_status = row["status"] or ""

        # Per-source daily new-item counts over the window, for the sparkline.
        buckets: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            """SELECT source, date(run_at) AS d, SUM(COALESCE(items_new, 0)) AS n
               FROM run_log
               WHERE run_at >= datetime('now', ?)
               GROUP BY source, d""",
            (f"-{window_days} days",),
        ).fetchall():
            buckets.setdefault(row["source"], {})[row["d"]] = row["n"]

    days = _recent_days(window_days)
    for name, s in summaries.items():
        per_day = buckets.get(name, {})
        s.daily_new = [per_day.get(d, 0) for d in days]

    ordered = [summaries[n] for n in specs]                      # registry order
    ordered += [s for n, s in summaries.items() if n not in specs]  # import-failed tail
    return ordered, failures


def _recent_days(window_days: int) -> list[str]:
    """List of YYYY-MM-DD strings, oldest→newest, ending today (UTC)."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(window_days - 1, -1, -1)]


# ── Rendering (lazy rich import) ────────────────────────────────────────

_STATUS_STYLE = {
    "active":        ("●", "green"),
    "stale":         ("●", "yellow"),
    "never-run":     ("○", "dim"),
    "error":         ("✗", "red"),
    "import-failed": ("⚠", "red"),
}


def render(summaries: list[SourceSummary], failures: dict[str, str], console: Any = None) -> None:
    """Pretty-print the catalog as a rich table + an import-failure note."""
    from rich.console import Console
    from rich.table import Table

    console = console or Console()
    table = Table(title="Source catalog", title_style="bold", expand=False)
    table.add_column("", width=1)
    table.add_column("Source", style="bold cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Last run", style="dim", no_wrap=True)
    table.add_column("7-day new", no_wrap=True)
    table.add_column("Total", justify="right")
    table.add_column("What it pulls", style="dim", no_wrap=True, overflow="ellipsis", max_width=46)

    active = 0
    for s in summaries:
        glyph, style = _STATUS_STYLE.get(s.status, ("?", "white"))
        if s.status == "active":
            active += 1
        last = "—"
        if s.import_error:
            last = "—"
        elif s.last_run:
            last = _humanize_age(s.last_run)
        spark = "" if s.import_error else sparkline(s.daily_new)
        doc = s.import_error or s.doc or ""
        table.add_row(
            f"[{style}]{glyph}[/{style}]",
            s.name,
            f"[{style}]{s.status}[/{style}]",
            last,
            f"[green]{spark}[/green]" if spark else "",
            str(s.total_items) if not s.import_error else "—",
            doc,
        )

    console.print(table)
    total = len(summaries)
    console.print(
        f"[dim]{active}/{total} active[/dim]"
        + (f"  ·  [red]{len(failures)} failed to import[/red]" if failures else "")
    )


def _humanize_age(iso: str) -> str:
    h = _age_hours(iso)
    if h >= 1e8:
        return "?"
    if h < 1:
        return f"{int(h * 60)}m ago"
    if h < 48:
        return f"{int(h)}h ago"
    return f"{int(h / 24)}d ago"


def print_sources(get_conn: Callable[[], Any], package: str, console: Any = None) -> None:
    """Collect + render in one call — the body of a domain's `sources` command."""
    summaries, failures = collect(get_conn, package)
    render(summaries, failures, console=console)
