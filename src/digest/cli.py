"""CLI entry point for pc-insurance-digest (Wave 1)."""
from __future__ import annotations

import logging
import sys

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from digest import db
from digest.config import settings

console = Console()


def _setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


INGESTORS = {
    "rss":      "digest.ingest.rss:RSSIngestor",
    "edgar":    "digest.ingest.edgar:EdgarIngestor",
    "reddit":   "digest.ingest.reddit:RedditIngestor",
    "substack": "digest.ingest.substack:SubstackIngestor",
    "hn":       "digest.ingest.hackernews:HNIngestor",
    # Wave 2 — direct government hazard feeds
    "nhc":      "digest.ingest.nhc:NHCIngestor",
    "usgs":     "digest.ingest.usgs:USGSIngestor",
    "spc":      "digest.ingest.spc:SPCIngestor",
    "nifc":     "digest.ingest.nifc:NIFCIngestor",
    # Wave 2.x — quantitative cost-driver series (live)
    "fred":     "digest.ingest.fred:FredIngestor",
    # Wave 3 — implemented (courtlistener needs token; collision/state_doi/serff need selector validation)
    "courtlistener":     "digest.ingest.courtlistener:CourtListenerIngestor",
    "collision":         "digest.ingest.collision_data:CollisionDataIngestor",
    "state_doi":         "digest.ingest.state_doi:StateDOIIngestor",
    "industry_research": "digest.ingest.industry_research:IndustryResearchIngestor",
    "serff":             "digest.ingest.serff:SerffIngestor",
    # Wave 3 Phase 3 — actuarial datasets (PDF parsing)
    "investor_supp":     "digest.ingest.investor_supp:InvestorSuppIngestor",
    "naic_schedp":       "digest.ingest.naic_schedp:NAICSchedulePIngestor",
}


def _load(dotted: str):
    module_path, class_name = dotted.split(":")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


@click.group()
def main() -> None:
    """P&C Insurance & Financial Services Digest CLI."""
    _setup_logging()


@main.command()
@click.argument("source", type=click.Choice(list(INGESTORS.keys()) + ["all"]))
@click.option("--run-type", default="manual", help="Tag for run_log (am/pm/manual)")
def ingest(source: str, run_type: str) -> None:
    """Ingest from one source or all."""
    db.init_db()
    targets = list(INGESTORS.keys()) if source == "all" else [source]

    total_fetched = 0
    total_new = 0
    for name in targets:
        console.rule(f"[bold cyan]{name}")
        try:
            cls = _load(INGESTORS[name])
            fetched, new = cls().run(run_type=run_type)
            total_fetched += fetched
            total_new += new
            console.print(f"[green]✓[/green] {name}: fetched={fetched} new={new}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗[/red] {name}: {exc}")

    if source == "all":
        console.rule("[bold]summary")
        console.print(f"total fetched={total_fetched} new={total_new}")


@main.command()
def stats() -> None:
    """Item counts by source plus triage + summarizer status."""
    db.init_db()
    counts = db.item_stats()
    table = Table(title="Items by source")
    table.add_column("Source")
    table.add_column("Count", justify="right")
    for src, n in counts.items():
        table.add_row(src, str(n))
    if not counts:
        console.print("[yellow]No items yet. Try:[/yellow] digest ingest all")
        return
    console.print(table)

    triage = db.triage_stats()
    if triage:
        t2 = Table(title="Triage status")
        t2.add_column("Decision")
        t2.add_column("Count", justify="right")
        for k, v in triage.items():
            t2.add_row(k, str(v))
        console.print(t2)

    sum_stats = db.summarizer_stats(days=7)
    if sum_stats:
        t3 = Table(title="Summarizer activity (7d)")
        t3.add_column("Backend")
        t3.add_column("Items", justify="right")
        for backend, info in sum_stats.items():
            t3.add_row(backend, str(info.get("n", 0)))
        console.print(t3)


@main.command()
@click.option("--source", default=None, help="Filter by source")
@click.option("--limit", default=20, help="Max rows")
def recent(source: str | None, limit: int) -> None:
    """Show most recently ingested items."""
    db.init_db()
    rows = db.recent_items(source=source, limit=limit)
    if not rows:
        console.print("[yellow]No items.[/yellow]")
        return
    table = Table(title="Recent items" + (f" — {source}" if source else ""))
    table.add_column("Source")
    table.add_column("Published", style="dim")
    table.add_column("Title")
    for row in rows:
        table.add_row(
            row["source"],
            (row["published_at"] or "")[:10],
            (row["title"] or "")[:80],
        )
    console.print(table)


@main.command()
@click.option("--limit", default=200, help="Max items to triage in this run")
def triage(limit: int) -> None:
    """Run MLX triage over pending items."""
    from digest.triage import run_triage

    db.init_db()
    console.rule("[bold cyan]triage")
    counts = run_triage(limit=limit)
    console.print(
        f"[green]✓[/green] triage: pending={counts['pending']} "
        f"kept={counts['kept']} dropped={counts['dropped']} errors={counts['errors']}"
    )


@main.command()
@click.option("--limit", default=None, type=int, help="Max items to summarize")
def summarize(limit: int | None) -> None:
    """Summarize top-scored items that passed triage."""
    from digest.summarize import run_summarize

    db.init_db()
    console.rule(f"[bold cyan]summarize ({settings.summarizer_backend})")
    counts = run_summarize(limit=limit)
    console.print(
        f"[green]✓[/green] summarize: ready={counts['ready']} "
        f"succeeded={counts['succeeded']} failed={counts['failed']}"
    )


@main.command()
@click.option("--run-type", default="manual", help="Tag for run_log (am/pm/manual)")
@click.option("--skip-publish", is_flag=True, help="Don't write to Obsidian (debug)")
def pipeline(run_type: str, skip_publish: bool) -> None:
    """Full pipeline: ingest → triage → summarize → regime → signals → publish."""
    from digest.triage import run_triage
    from digest.summarize import run_summarize
    from digest.obsidian import publish as obs_publish
    from digest.regime import compute_regime, is_stale, current_regime
    from digest.signals import run_signals

    db.init_db()

    console.rule("[bold cyan]stage 1: ingest")
    for name in INGESTORS:
        try:
            cls = _load(INGESTORS[name])
            fetched, new = cls().run(run_type=run_type)
            console.print(f"  [green]✓[/green] {name}: {fetched}/{new}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]✗[/red] {name}: {exc}")

    console.rule("[bold cyan]stage 2: triage")
    t = run_triage()
    console.print(
        f"  [green]✓[/green] kept={t['kept']} dropped={t['dropped']} errors={t['errors']}"
    )

    console.rule("[bold cyan]stage 3: summarize")
    s = run_summarize()
    console.print(
        f"  [green]✓[/green] succeeded={s['succeeded']} failed={s['failed']} ready={s['ready']}"
    )

    console.rule("[bold cyan]stage 4: regime")
    try:
        if is_stale():
            r = compute_regime()
            console.print(f"  [green]✓[/green] recomputed: {r.summary_line()}")
        else:
            r = current_regime()
            console.print(f"  [dim]✓ cached:[/dim] {r.summary_line()}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] regime failed: {exc}")

    console.rule("[bold cyan]stage 5: signals")
    try:
        sig = run_signals()
        console.print(f"  [green]✓[/green] scored={sig['scored']}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] signals failed: {exc}")

    if skip_publish:
        console.rule("[bold yellow]stage 6: publish (skipped)")
        return
    console.rule("[bold cyan]stage 6: publish")
    try:
        result = obs_publish()
        console.print(
            f"  [green]✓[/green] daily={result['daily_items']} items, "
            f"topic_archives={result['topic_archives']}"
        )
        console.print(f"  [dim]→ {result['daily_path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] publish failed: {exc}")


@main.command()
@click.option("--force", is_flag=True, help="Recompute even if last signal < 72h old")
def regime(force: bool) -> None:
    """Compute or display the current PC two-axis regime (market_cycle × cat_load)."""
    from digest.regime import compute_regime, current_regime, is_stale

    db.init_db()
    if force or is_stale():
        console.rule("[bold cyan]regime: recompute")
        r = compute_regime(force=force)
    else:
        console.rule("[bold cyan]regime: cached")
        r = current_regime()
    console.print(f"  {r.summary_line()}")
    console.print(f"  [dim]as_of={r.as_of}  source={r.source}[/dim]")
    market_judgment = (r.evidence or {}).get("market_judgment", {})
    if isinstance(market_judgment, dict) and market_judgment.get("evidence"):
        console.print(f"  [dim]evidence: {market_judgment['evidence']}[/dim]")


@main.command()
@click.option("--limit", default=10, help="Top-N to display")
@click.option("--recompute/--no-recompute", default=True,
              help="Recompute scores before displaying (default: on)")
def signals(limit: int, recompute: bool) -> None:
    """Score every kept+summarized item and display the top-N leaderboard."""
    from digest.signals import run_signals

    db.init_db()
    if recompute:
        console.rule("[bold cyan]signals: rescore")
        counts = run_signals()
        console.print(f"  [green]✓[/green] scored={counts['scored']}")

    rows = db.top_signal_scores(limit=limit)
    if not rows:
        console.print("[yellow]No scored items yet. Run `digest pipeline` or `digest signals`.[/yellow]")
        return

    table = Table(title=f"Top {limit} signals")
    table.add_column("Score", justify="right")
    table.add_column("Topic")
    table.add_column("Source")
    table.add_column("Title")
    for r in rows:
        table.add_row(
            f"{r['score']:.2f}",
            (r["topic"] or "")[:20],
            (r["source"] or "")[:10],
            (r["title"] or "")[:70],
        )
    console.print(table)


@main.command()
@click.option("--date", "date_iso", default=None, help="YYYY-MM-DD (default: today UTC)")
@click.option("--topics-only", is_flag=True, help="Refresh topic archives only")
def publish(date_iso: str | None, topics_only: bool) -> None:
    """Write daily note + topic archives to Obsidian vault."""
    from digest.obsidian import Paths, publish as obs_publish, write_topic_archive

    db.init_db()
    if topics_only:
        paths = Paths.resolve()
        paths.ensure()
        console.rule("[bold cyan]publish: topics only")
        for slug in db.topics_with_summaries():
            path, n = write_topic_archive(slug, paths)
            console.print(f"  [green]✓[/green] {path.name}: {n} items")
        return

    console.rule("[bold cyan]publish")
    try:
        result = obs_publish(date_iso=date_iso)
        console.print(
            f"  [green]✓[/green] {result['date']}: "
            f"daily={result['daily_items']} items, "
            f"topic_archives={result['topic_archives']}"
        )
        console.print(f"  [dim]→ {result['daily_path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
@click.option("--date", "date_iso", default=None, help="Any date in target week YYYY-MM-DD")
def weekly(date_iso: str | None) -> None:
    """Generate weekly synthesis note."""
    from digest.obsidian import publish_weekly

    db.init_db()
    console.rule("[bold cyan]weekly digest")
    try:
        result = publish_weekly(date_iso=date_iso)
        console.print(
            f"  [green]✓[/green] week={result['week']} "
            f"items={result['item_count']} themes={result['theme_count']}"
        )
        console.print(f"  [dim]→ {result['path']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
def health() -> None:
    """Check status of subsystems (DB, MLX, vault, launchd, env)."""
    from digest.health import run_health, overall_status

    db.init_db()
    console.rule("[bold cyan]app health")
    report = run_health()
    overall = overall_status(report)
    _STATUS_COLOR = {"ok": "green", "warn": "yellow", "fail": "red"}
    _STATUS_ICON  = {"ok": "✓", "warn": "⚠", "fail": "✗"}

    for component, result in report.items():
        s     = result["status"]
        color = _STATUS_COLOR[s]
        icon  = _STATUS_ICON[s]
        details = result.get("details", {})
        detail_str = "  ".join(f"{k}={v}" for k, v in details.items() if k != "jobs")
        console.print(f"  [{color}]{icon}[/{color}] [bold]{component}[/bold]  [dim]{detail_str[:100]}[/dim]")
        if "jobs" in details:
            for label, jinfo in details["jobs"].items():
                short = label.replace("com.dr.", "")
                if jinfo.get("loaded"):
                    exit_c = jinfo.get("last_exit", "?")
                    jcolor = "green" if exit_c in ("0", "-") else "red"
                    console.print(f"       [{jcolor}]{short}[/{jcolor}]  pid={jinfo['pid']}  exit={exit_c}")
                else:
                    console.print(f"       [dim]{short}  not loaded[/dim]")

    overall_color = _STATUS_COLOR[overall]
    console.rule(f"[{overall_color}]overall: {overall}[/{overall_color}]")


@main.command()
@click.option("--open", "open_after", is_flag=True, help="Open generated files in the OS default app")
def viz(open_after: bool) -> None:
    """Generate claims trend SVG visualizations into the Obsidian vault."""
    from digest.viz import write_viz_pages

    db.init_db()
    console.rule("[bold cyan]viz")
    try:
        paths = write_viz_pages(open_after=open_after)
        for p in paths:
            console.print(f"  [green]✓[/green] {p}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command("init-db")
def init_db_cmd() -> None:
    """Create the SQLite DB and schema."""
    db.init_db()
    console.print(f"[green]✓[/green] DB initialized at {settings.db_path}")


if __name__ == "__main__":
    sys.exit(main())
