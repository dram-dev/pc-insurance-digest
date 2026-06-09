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
from digest_core.cli.base import discover_ingestors, run_ingest

console = Console()


def _setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


# Sources are no longer hand-listed: every IngestorBase subclass under
# digest.ingest self-registers (see digest_core.ingest.registry). Drop a new
# ingestor file in that package and it appears here automatically — and in
# `digest sources`. A source whose module fails to import (missing optional dep)
# is reported by `digest sources` rather than silently vanishing.
INGESTORS = discover_ingestors("digest.ingest")


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
    total_fetched, total_new = run_ingest(INGESTORS, targets, run_type, console)
    if source == "all":
        console.rule("[bold]summary")
        console.print(f"total fetched={total_fetched} new={total_new}")


@main.command()
def sources() -> None:
    """Live source catalog: every registered ingestor + its 7-day pulse.

    Auto-discovered from the registry — a newly added ingestor shows up here on
    its own (as 'never-run' until its first ingest). Sources whose module can't
    import (missing optional dep) are flagged rather than silently dropped.
    """
    from digest_core import catalog

    db.init_db()
    catalog.print_sources(db.get_conn, "digest.ingest", console=console)


@main.command()
@click.argument("item_id", type=int)
@click.argument("rating", type=click.FloatRange(1.0, 5.0))
@click.option("--note", default=None, help="Optional context for the rating")
def rate(item_id: int, rating: float, note: str | None) -> None:
    """Rate an item 1-5 — the calibration input behind score_calibration.

    Records what you think an item was worth so the lakehouse can compare it to
    the system's computed score (gold.score_calibration). Example:
    `digest rate 1423 5 --note "exactly the FAIR Plan signal I want surfaced"`.
    """
    db.init_db()
    with db.get_conn() as conn:
        item = conn.execute(
            "SELECT id, title, topic FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    if item is None:
        console.print(f"[red]✗[/red] no item with id {item_id}")
        raise SystemExit(1)
    db.upsert_manual_rating(item_id, rating, note)
    console.print(
        f"[green]✓[/green] rated #{item_id} [bold]{rating:.1f}[/bold] "
        f"([dim]{item['topic'] or '?'}[/dim]) — {item['title'][:70]}"
    )


@main.command()
@click.option("--limit", default=30, help="Max rated items to show")
def calibration(limit: int) -> None:
    """How your manual ratings line up with the system's computed scores.

    The local mirror of gold.score_calibration: rate items with `digest rate`,
    then run this to see where the leaderboard over- or under-valued an item vs.
    your judgement (Δ = system − user). Drives scoring-weight tuning.
    """
    db.init_db()
    rows = db.calibration_rows(limit=limit)
    if not rows:
        console.print("[yellow]No rated items yet.[/yellow] Rate one: digest rate <id> <1-5>")
        return
    table = Table(title="Score calibration (system vs. your rating)")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Topic", no_wrap=True)
    table.add_column("You", justify="right")
    table.add_column("System", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("Title", no_wrap=True, overflow="ellipsis", max_width=52)
    deltas: list[float] = []
    for r in rows:
        sys_score = r["system_score"]
        if sys_score is None:
            sys_cell, delta_cell = "[dim]—[/dim]", "[dim]—[/dim]"
        else:
            delta = sys_score - r["user_rating"]
            deltas.append(delta)
            colour = "yellow" if abs(delta) >= 1.0 else "green"
            sys_cell = f"{sys_score:.2f}"
            delta_cell = f"[{colour}]{delta:+.2f}[/{colour}]"
        table.add_row(
            str(r["item_id"]), r["topic"] or "?", f"{r['user_rating']:.1f}",
            sys_cell, delta_cell, r["title"] or "(untitled)",
        )
    console.print(table)
    scored = len(deltas)
    if scored:
        mean_abs = sum(abs(d) for d in deltas) / scored
        console.print(
            f"[dim]{scored}/{len(rows)} rated items scored · "
            f"mean |Δ| = {mean_abs:.2f}[/dim]"
        )


@main.command()
@click.option("--hours", default=48, help="Lookback window for the alert watchlist")
@click.option("--top", default=5, help="How many top signals to show")
def brief(hours: int, top: int) -> None:
    """Today's signal brief — regime, top signals, and the alert watchlist.

    The local, offline analog of the Databricks Genie/Alerts layer (Option 2):
    reads SQLite directly, so it works with no warehouse. Surfaces the prevailing
    regime, the top-scored items, and watch conditions (high-burden regulatory
    items, nuclear-verdict/TPLF signals, FRED anomalies, degraded sources).
    """
    from digest import signals

    db.init_db()

    # Regime banner
    reg = db.latest_regime_signal()
    if reg:
        console.rule(
            f"[bold]P&C brief[/bold] · regime: "
            f"[cyan]{reg['market_cycle']}[/cyan] × [cyan]{reg['cat_load']}[/cyan] "
            f"= [bold]{reg['multiplier']:.2f}×[/bold]  [dim]({str(reg['as_of'])[:10]})[/dim]"
        )
    else:
        console.rule("[bold]P&C brief[/bold] [dim](no regime computed yet)[/dim]")

    # Top signals
    rows = db.top_signal_scores(limit=top)
    if rows:
        table = Table(title=f"Top {len(rows)} signals")
        table.add_column("Tier", no_wrap=True)
        table.add_column("Score", justify="right")
        table.add_column("Topic", no_wrap=True)
        table.add_column("Source", no_wrap=True, style="dim")
        table.add_column("Title", no_wrap=True, overflow="ellipsis", max_width=58)
        for r in rows:
            table.add_row(
                signals.tier_badge_for_row(r), f"{r['score']:.2f}",
                r["topic"] or "?", r["source"], r["title"] or "(untitled)",
            )
        console.print(table)
    else:
        console.print("[dim]No scored items yet — run `digest signals`.[/dim]")

    # Alert watchlist
    alerts = db.brief_alerts(hours=hours)
    fired = False
    if alerts["high_burden"]:
        fired = True
        console.print(f"\n[bold red]⚠ High regulatory burden[/bold red] ({len(alerts['high_burden'])}):")
        for r in alerts["high_burden"]:
            arrow = {"increasing": "↑", "decreasing": "↓"}.get(r["burden_direction"], "·")
            console.print(f"  [red]{arrow}[/red] [{r['source']}] {r['title'][:80]}")
    if alerts["tplf"]:
        fired = True
        console.print(f"\n[bold magenta]⚖ Litigation / TPLF[/bold magenta] ({len(alerts['tplf'])}):")
        for r in alerts["tplf"]:
            console.print(f"  [magenta]•[/magenta] [{r['source']}] {r['title'][:80]}")
    if alerts["fred"]:
        fired = True
        console.print(f"\n[bold yellow]📈 FRED cost-driver anomalies[/bold yellow] ({len(alerts['fred'])}):")
        for r in alerts["fred"]:
            console.print(f"  [yellow]•[/yellow] {r['title'][:90]}")
    if alerts["degraded"]:
        fired = True
        console.print(f"\n[bold red]✗ Degraded sources[/bold red] ({len(alerts['degraded'])}):")
        for r in alerts["degraded"]:
            console.print(f"  [red]✗[/red] {r['source']}: {(r['error'] or 'error')[:70]}")
    if not fired:
        console.print(f"\n[green]✓ No alerts in the last {hours}h.[/green]")


@main.command()
@click.option("--limit", default=500, help="Max items to embed this run")
def embed(limit: int) -> None:
    """Compute embeddings for kept items that lack them (semantic layer).

    Uses the local Ollama server (EMBEDDING_MODEL, default nomic-embed-text) —
    `ollama pull nomic-embed-text` first. Powers `digest related` / `digest ask`.
    """
    from digest import semantic

    db.init_db()
    counts = semantic.run_embed(limit=limit)
    console.print(
        f"[green]✓[/green] embed: needed={counts['needed']} embedded={counts['embedded']}"
    )


@main.command()
@click.argument("item_id", type=int)
@click.option("--k", default=5, help="How many related items to show")
def related(item_id: int, k: int) -> None:
    """Items semantically closest to ITEM_ID (more-like-this / see-also)."""
    from digest import semantic

    db.init_db()
    hits = semantic.related(item_id, k=k)
    if not hits:
        console.print("[yellow]No neighbours (item unembedded? run `digest embed`).[/yellow]")
        return
    table = Table(title=f"Related to #{item_id}")
    table.add_column("Sim", justify="right")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Topic", no_wrap=True)
    table.add_column("Title", no_wrap=True, overflow="ellipsis", max_width=60)
    for h in hits:
        table.add_row(f"{h['score']:.2f}", str(h["item_id"]), h["topic"] or "?",
                      h["title"] or "(untitled)")
    console.print(table)


@main.command()
@click.argument("question")
@click.option("--k", default=8, help="How many items to retrieve as context")
def ask(question: str, k: int) -> None:
    """Ask a question answered from your own digest corpus (RAG).

    Embeds the question, retrieves the most relevant items, and answers with the
    configured summarizer backend — citing the item numbers it used.
    """
    from digest import semantic

    db.init_db()
    result = semantic.ask(question, k=k)
    if result.get("error"):
        console.print(f"[yellow]{result['error']}[/yellow]")
    if result.get("answer"):
        console.print(f"\n{result['answer']}\n")
    if result.get("sources"):
        console.rule("[dim]sources")
        for s in result["sources"]:
            console.print(
                f"[dim][#{s['n']}][/dim] [cyan]{s['score']:.2f}[/cyan] "
                f"[dim]({s['source']})[/dim] {(s['title'] or '')[:80]}"
            )


@main.command()
@click.option("--horizons", default="7,30", help="Comma-separated horizon days")
@click.option("--limit", default=500, help="Max matured items per horizon")
def outcomes(horizons: str, limit: int) -> None:
    """Backtest: did ranked items actually matter? (Option 1b)

    For each scored item whose horizon (default 7d + 30d) has elapsed, checks 5
    corroboration signals — follow-on coverage, same-insurer EDGAR filing, regime
    shift, your rating ≥4, and a ≥1σ insurer stock move — and records whether it
    corroborated. Feeds gold.outcome_hit_rate + the learned scorer's labels.
    Weekly cadence is plenty (outcomes need the window to mature).
    """
    from digest.outcomes import run_outcomes

    db.init_db()
    hs = tuple(int(h) for h in horizons.split(",") if h.strip())
    console.rule("[bold cyan]outcomes backtest")
    counts = run_outcomes(horizons=hs, limit=limit)
    for h, n in counts.items():
        console.print(f"  [green]✓[/green] horizon={h}d: checked={n}")


@main.command()
@click.option("--horizon", default=30, help="Outcome horizon to train against (days)")
def learn(horizon: int) -> None:
    """Train the learned relevance scorer + A/B it vs the heuristic (Option 4).

    Fits a numpy logistic regression on the boost factors + heuristic score to
    predict corroboration (from `digest outcomes`), reports holdout AUC and
    top-N precision (heuristic vs learned), then writes a learned_score for every
    scored item. The heuristic stays authoritative; this is advisory until the
    A/B proves a lift. Needs ≥12 labeled items — run `digest outcomes` first.
    """
    from digest import learn as learn_mod

    db.init_db()
    console.rule("[bold cyan]learned scorer")
    s = learn_mod.run(horizon_days=horizon)
    if not s.get("model_id"):
        console.print(f"[yellow]{s.get('note', 'training skipped')}[/yellow] "
                      f"(n={s.get('n_samples', 0)})")
        return

    def _p(x):
        return f"{x:.3f}" if isinstance(x, (int, float)) else "—"

    console.print(f"[green]✓[/green] model #{s['model_id']} trained on {s['n_samples']} items")
    console.print(f"  holdout AUC: {_p(s.get('auc'))}")
    h, learned = s.get("heuristic_precision"), s.get("learned_precision")
    arrow = ""
    if isinstance(h, (int, float)) and isinstance(learned, (int, float)):
        arrow = " [green]↑ learned wins[/green]" if learned > h else (
            " [yellow]→ heuristic holds[/yellow]" if learned == h else " [dim]↓ heuristic better[/dim]")
    console.print(f"  top-{s.get('k', 5)} precision — heuristic {_p(h)} vs learned {_p(learned)}{arrow}")
    console.print(f"  [green]✓[/green] learned_score written for {s.get('scored', 0)} items")


@main.group()
def forecast() -> None:
    """Alpha engine — predict insurer returns from digest data + signal scores.

    Local-only ML (LightGBM + optional Apple MLX). The heuristic leaderboard
    stays authoritative; these forecasts are an advisory early-warning layer.
    Pipeline: `prices` (build the price store) → `train` (fit + honest
    walk-forward backtest) → `predict` (write per-insurer forecasts).
    """


@forecast.command(name="prices")
@click.option("--full", is_flag=True, help="Re-upsert the full ~2y window (else just the tail)")
@click.option("--ticker", "tickers", multiple=True, help="Limit to these insurer tickers")
def forecast_prices(full: bool, tickers: tuple[str, ...]) -> None:
    """Refresh the daily price store (14 insurers + IAK/SPY benchmarks).

    Reuses the free Yahoo/Stooq fetch the outcomes σ signal uses; Yahoo returns
    ~2y so the first run backfills for free. Datacenter IPs get throttled — run
    this from the residential Mac-mini host.
    """
    from digest.prices import run_prices

    db.init_db()
    console.rule("[bold cyan]price store")
    res = run_prices(tickers=list(tickers) or None, full=full)
    console.print(f"[green]✓[/green] {res['rows']:,} rows across {res['tickers']} tickers")
    if res["skipped"]:
        console.print(f"  [yellow]skipped (no data):[/yellow] {', '.join(res['skipped'])}")


def _fmt(x):
    return f"{x:+.4f}" if isinstance(x, (int, float)) else "—"


@forecast.command(name="backtest")
@click.option("--horizon", default=20, help="Forward trading-day horizon")
@click.option("--splits", default=3, help="Walk-forward folds")
def forecast_backtest(horizon: int, splits: int) -> None:
    """Honest walk-forward scorecard (IC / hit-rate / long-short vs baselines).

    Purged + embargoed expanding-window backtest — no model is saved. The model
    must beat the momentum and signal-only baselines before the forecasts mean
    anything; this command is how you check.
    """
    from digest import alpha

    db.init_db()
    console.rule("[bold cyan]return backtest")
    r = alpha.backtest(horizon=horizon, n_splits=splits)
    if not r.get("ok"):
        console.print(f"[yellow]{r.get('note')}[/yellow] (n={r.get('n', 0)})")
        return
    from digest import alpha as _alpha
    base = r.get("baseline_ic")
    verdict = ("[green]↑ real edge (positive IC, beats baselines)[/green]"
               if _alpha.has_edge(r.get("ic"), base)
               else "[yellow]→ no edge — IC not positive / no lift over baselines (treat as noise)[/yellow]")
    console.print(f"[green]✓[/green] {r['oos_rows']} OOS rows over {r['folds']} folds (n={r['n']})")
    console.print(f"  IC: {_fmt(r.get('ic'))}   hit-rate: {_fmt(r.get('hit_rate'))}   "
                  f"long-short: {_fmt(r.get('long_short'))}")
    console.print(f"  baselines — momentum IC {_fmt(r.get('momentum_ic'))}, "
                  f"signal IC {_fmt(r.get('signal_ic'))}  →  {verdict}")


@forecast.command(name="train")
@click.option("--horizon", default=20, help="Forward trading-day horizon")
@click.option("--splits", default=3, help="Walk-forward folds for the scorecard")
def forecast_train(horizon: int, splits: int) -> None:
    """Backtest, then fit + persist a final model on all labeled rows."""
    from digest import alpha

    db.init_db()
    console.rule("[bold cyan]return model — train")
    r = alpha.train(horizon=horizon, n_splits=splits)
    if not r.get("model_id"):
        console.print(f"[yellow]{r.get('note', 'training skipped')}[/yellow] (n={r.get('n', 0)})")
        return
    console.print(f"[green]✓[/green] model #{r['model_id']} ({r['algo']}) on {r['n']} rows; "
                  f"classifier={'yes' if r.get('has_classifier') else 'no'}")
    console.print(f"  OOS IC {_fmt(r.get('ic'))} vs baseline {_fmt(r.get('baseline_ic'))}; "
                  f"long-short {_fmt(r.get('long_short'))}")


@forecast.command(name="predict")
@click.option("--horizon", default=20, help="Horizon (only used to pick a model if none trained)")
def forecast_predict(horizon: int) -> None:
    """Write per-insurer forecasts from the latest model + latest signal date."""
    from digest import alpha

    db.init_db()
    console.rule("[bold cyan]return model — predict")
    r = alpha.predict(horizon=horizon)
    if not r.get("forecasts"):
        console.print(f"[yellow]{r.get('note', 'no forecasts')}[/yellow]")
        return
    console.print(f"[green]✓[/green] {r['forecasts']} forecasts (model #{r['model_id']}, "
                  f"horizon={r['horizon']}d)")
    top = db.latest_return_forecasts(horizon_days=r["horizon"], limit=5)
    for f in top:
        prob = f"{f['pred_prob']:.2f}" if f["pred_prob"] is not None else "—"
        console.print(f"  {f['ticker']:<5} excess {_fmt(f['pred_excess'])}  P(beat) {prob}")


@main.command(name="learn-loop")
@click.option("--horizon", default=20, help="Alpha-model forward horizon (days)")
def learn_loop(horizon: int) -> None:
    """Hands-off learning loop: outcomes → learn → forecast train → predict.

    Meant for the weekly launchd job (com.dr.pcdigest.learn). Each stage is
    best-effort — a stage that has too little data (the usual early-on case)
    logs a note and the loop continues, so the job never aborts. As the daily
    pipeline accumulates matured, insurer-mapped signals, this is what turns
    them into a refreshed learned scorer + return forecasts without you running
    anything by hand.
    """
    from digest.outcomes import run_outcomes
    from digest import learn as learn_mod
    from digest import alpha

    db.init_db()

    # 1. Label matured items at both horizons (feeds the learned scorer).
    console.rule("[bold cyan]1/3 outcomes")
    try:
        counts = run_outcomes(horizons=(7, 30))
        console.print("  [green]✓[/green] " +
                      ", ".join(f"{h}d checked={n}" for h, n in counts.items()))
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]outcomes skipped:[/yellow] {exc}")

    # 2. Learned relevance scorer — run_best self-starts on whichever horizon
    #    has matured labels (tries 30d, falls back to 7d).
    console.rule("[bold cyan]2/3 learned scorer")
    try:
        s = learn_mod.run_best(horizons=(30, 7))
        if s.get("model_id"):
            console.print(f"  [green]✓[/green] model #{s['model_id']} "
                          f"(h={s.get('horizon_days')}d, AUC {_fmt(s.get('auc'))}); "
                          f"scored {s.get('scored', 0)} items")
        else:
            console.print(f"  [yellow]{s.get('note', 'skipped')}[/yellow]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]learn skipped:[/yellow] {exc}")

    # 3. Alpha return model — train (honest walk-forward) then write forecasts.
    console.rule("[bold cyan]3/3 return model")
    try:
        a = alpha.run(horizon=horizon)
        if a.get("model_id"):
            edge = alpha.has_edge(a.get("ic"), a.get("baseline_ic"))
            console.print(f"  [green]✓[/green] model #{a['model_id']} ({a.get('algo')}), "
                          f"OOS IC {_fmt(a.get('ic'))} vs baseline {_fmt(a.get('baseline_ic'))} "
                          f"→ {'edge' if edge else 'no edge (noise)'}; "
                          f"{a.get('forecasts', 0)} forecasts")
        else:
            console.print(f"  [yellow]{a.get('note', 'training skipped')}[/yellow] (n={a.get('n', 0)})")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]forecast skipped:[/yellow] {exc}")


@main.command()
def canonicalize() -> None:
    """Backfill canonical_lob across loss_triangles + statutory_facts.

    Maps the messy source-specific LOB strings (SEC-XBRL member slugs, statutory
    lines) onto one comparison taxonomy so cross-insurer rollups line up. Runs at
    upsert time too; this backfills rows ingested before the column existed.
    """
    from digest.parse.lob_canonical import CANONICAL_LOBS

    db.init_db()
    console.rule("[bold cyan]LOB canonicalization")
    counts = db.backfill_canonical_lob()
    console.print(f"[green]✓[/green] loss_triangles={counts['loss_triangles']:,} rows, "
                  f"statutory_facts={counts['statutory_facts']:,} rows")
    rows = db.canonical_lob_coverage()
    if rows:
        table = Table(title="Canonical LOB coverage (loss_triangles)")
        table.add_column("Canonical LOB", no_wrap=True)
        table.add_column("Insurers", justify="right")
        table.add_column("Raw LOBs", justify="right")
        for r in rows:
            table.add_row(r["canonical_lob"] or "—", str(r["insurers"]), str(r["raw_lobs"]))
        console.print(table)
    console.print(f"[dim]taxonomy: {', '.join(CANONICAL_LOBS)}[/dim]")


@main.command(name="ingest-naic")
def ingest_naic() -> None:
    """Load NAIC InsData Schedule P exports → loss_triangles + statutory_facts.

    The only route to loss triangles for the big mutuals (State Farm, USAA,
    Liberty Mutual, …) that file nothing with the SEC. Export Schedule P from the
    InsData portal, drop the CSV(s) in $NAIC_INSDATA_DIR, map columns in
    config/naic_insdata.yaml, then run this. Feeds the same reserving chain.
    """
    from digest.ingest import naic_insdata

    db.init_db()
    console.rule("[bold cyan]NAIC InsData Schedule P ingest")
    r = naic_insdata.load_from_dir()
    if r["files"] == 0:
        console.print(f"[yellow]No export files found in {settings.naic_insdata_dir}.[/yellow]\n"
                      "Export Schedule P from InsData and drop CSV(s) there "
                      "(map columns in config/naic_insdata.yaml).")
        return
    console.print(f"[green]✓[/green] {r['files']} file(s) → {r['triangle_cells']:,} triangle cells, "
                  f"{r['premium_facts']:,} premium facts across {r['insurers']} insurers")
    if r.get("insurer_list"):
        console.print(f"  [dim]{', '.join(r['insurer_list'])}[/dim]")


@main.command(name="ingest-statutory")
def ingest_statutory() -> None:
    """Free high-level statutory data — top-writer DPW + market share from III.

    Pulls the big mutuals' direct premiums written and market share (NAIC-sourced
    III tables) into statutory_facts — so the warehouse isn't blind to State Farm
    (#1) and peers. No credentials. Triangles still come from `digest ingest-naic`.
    """
    from digest.ingest import statutory_summary

    db.init_db()
    console.rule("[bold cyan]free statutory summary ingest")
    r = statutory_summary.run_statutory_summary()
    console.print(f"[green]✓[/green] {r['sources']} source(s) → {r['facts']:,} facts "
                  f"across {r['insurers']} insurers")
    if r.get("insurer_list"):
        console.print(f"  [dim]{', '.join(r['insurer_list'][:14])}"
                      f"{' …' if len(r['insurer_list']) > 14 else ''}[/dim]")


@main.command(name="ingest-xbrl")
@click.argument("tickers", nargs=-1)
def ingest_xbrl(tickers: tuple[str, ...]) -> None:
    """Ingest component-level insurer XBRL facts (datasets 1-13) → insurer_xbrl_facts.

    One 10-K instance fetch per insurer yields every reviewed dataset (premiums,
    claim counts, IBNR, reserve development, reinsurance, investments, DAC, …)
    broken out by segment/product/accident-year/geography. Universe =
    config/xbrl_pc_insurers.yaml (top-10 SEC-filing US P&C). Needs EDGAR_USER_AGENT.
    """
    from digest import edgar_xbrl_ingest as ing

    db.init_db()
    console.rule("[bold cyan]XBRL component-fact ingest")
    results = ing.run_ingest(list(tickers) or None)

    table = Table(title="Component facts per insurer")
    table.add_column("Ticker", no_wrap=True)
    table.add_column("Filed", style="dim", no_wrap=True)
    table.add_column("Facts", justify="right")
    table.add_column("Triangle cells", justify="right")
    table.add_column("Datasets", justify="right")
    for r in results:
        if "error" in r:
            table.add_row(r["ticker"], "[red]ERR[/red]", "—", "—", str(r["error"])[:48])
        else:
            table.add_row(r["ticker"], str(r["filed"]), f"{r['facts']:,}",
                          f"{r['triangle_cells']:,}", str(len(r["datasets"])))
    console.print(table)

    cov = db.xbrl_facts_coverage()
    if cov:
        datasets = sorted({row["dataset"] for row in cov})
        console.print(f"\n[dim]Datasets captured ({len(datasets)}):[/dim] {', '.join(datasets)}")
        total = sum(row["n"] for row in cov)
        console.print(f"[green]✓[/green] {total:,} component facts stored across "
                      f"{len({row['insurer'] for row in cov})} insurers")


@main.command(name="triangles-compare")
@click.argument("tickers", nargs=-1)
def triangles_compare(tickers: tuple[str, ...]) -> None:
    """Cross-validate the two EDGAR triangle extractors (build-both-and-compare).

    For each ticker's latest 10-K, runs the XBRL-instance parser and the rendered
    R-file parser and reports whether their values agree. No DB writes — this is
    the evaluation harness for picking the extraction path. Needs EDGAR_USER_AGENT.
    """
    from digest.edgar_triangle_extract import extract_and_compare, ticker_ciks

    ciks = ticker_ciks()
    targets = [t.upper() for t in tickers] or list(ciks)
    console.rule("[bold cyan]EDGAR triangle extractor comparison")
    table = Table(title="XBRL vs R-file — value cross-validation")
    table.add_column("Ticker", no_wrap=True)
    table.add_column("Filed", style="dim", no_wrap=True)
    table.add_column("XBRL", justify="right")
    table.add_column("R-file", justify="right")
    table.add_column("Agree", justify="right")
    table.add_column("Matched tri", justify="right")
    table.add_column("Only X / R", justify="right")
    table.add_column("Note", overflow="fold")
    for tk in targets:
        try:
            res = extract_and_compare(tk, ciks[tk])
            d = res["diff"]
            agree = f"{d['value_agree_pct']:.0f}%"
            colour = "green" if d["value_agree_pct"] >= 99 and d["rfile_cells"] else (
                "yellow" if d["rfile_cells"] else "red")
            note = ""
            if not d["rfile_cells"]:
                note = "no R-file parsed (XBRL only)"
            elif d["only_xbrl_values"] or d["only_rfile_values"]:
                note = f"{len(d['unmatched_xbrl'])} X / {len(d['unmatched_rfile'])} R unmatched tri"
            table.add_row(
                tk, str(res["filing_date"]), str(d["xbrl_cells"]), str(d["rfile_cells"]),
                f"[{colour}]{agree}[/{colour}]", str(d["matched_triangles"]),
                f"{d['only_xbrl_values']}/{d['only_rfile_values']}", note or "[green]✓ full agreement[/green]",
            )
        except Exception as exc:  # noqa: BLE001
            table.add_row(tk, "—", "—", "—", "[red]ERR[/red]", "—", "—", str(exc)[:60])
    console.print(table)


@main.command(name="underwriting")
@click.argument("tickers", nargs=-1)
def underwriting(tickers: tuple[str, ...]) -> None:
    """Loss&LAE / expense / combined ratio per insurer, with earned-premium validation.

    The earned-premium denominator is cross-checked against net written premium
    (EP/WP ≈ 1.0 = sound, ✓). The loss&LAE ratio is the cross-insurer comparison;
    the combined ratio is shown only where the EP validates AND the full expense is
    tagged (else '—' — build it from per-LOB components rolled up or the reported
    EX-99.1 figure via the combined-ratio-bridge skill, never from operating profit).
    """
    from digest import fundamentals as fmod
    from digest.edgar_xbrl_ingest import insurer_universe

    db.init_db()
    targets = [t.upper() for t in tickers] or [t for t, _ in insurer_universe()]
    console.rule("[bold cyan]underwriting ratios (XBRL components)")
    table = Table(title="Underwriting — latest fiscal year")
    table.add_column("Ticker", no_wrap=True)
    table.add_column("Earned $M", justify="right")
    table.add_column("EP/WP", justify="right")
    table.add_column("Loss&LAE %", justify="right")
    table.add_column("Expense %", justify="right")
    table.add_column("Combined %", justify="right")
    table.add_column("Reported %", justify="right")

    def pct(x):
        return f"{x * 100:.1f}" if x is not None else "[dim]—[/dim]"

    def musd(x):
        return f"{x:,.0f}" if x else "[dim]—[/dim]"

    clean = 0
    for tk in targets:
        d = fmod.underwriting_ratios(tk)
        if d["loss_lae_ratio"] is not None:
            clean += 1
        ep = "[dim]—[/dim]"
        if d["ep_to_wp"] is not None:
            ep = f"[{'green' if d['ep_validated'] else 'yellow'}]{d['ep_to_wp']:.2f}" \
                 f"{'✓' if d['ep_validated'] else '?'}[/]"
        cr = d["combined_ratio"]
        crc = "green" if cr is not None and cr < 1.0 else ("red" if cr is not None else "dim")
        # Reported (from the investor-supplement table) with a ✓ when it reconciles
        # with the computed combined ratio — two independent sources agreeing.
        rep = d["reported_combined_ratio"]
        rep_cell = "[dim]—[/dim]"
        if rep is not None:
            agree = cr is not None and abs(cr - rep) <= 0.01
            rep_cell = f"[{'green' if agree else 'yellow'}]{rep * 100:.1f}{'✓' if agree else ''}[/]"
        table.add_row(tk, musd(d["earned_premium_musd"]), ep, pct(d["loss_lae_ratio"]),
                      pct(d["expense_ratio"]), f"[{crc}]{pct(d['combined_ratio'])}[/{crc}]", rep_cell)
    console.print(table)
    console.print(f"[dim]{clean}/{len(targets)} clean loss line. Combined = XBRL components (EP "
                  f"validated vs written); Reported = parsed from the investor-supplement table "
                  f"(`digest ingest investor_supp`), ✓ = the two reconcile.[/dim]")


@main.command()
def reserving() -> None:
    """Chain-ladder reserving over stored loss triangles (Option 5).

    Computes ultimate / IBNR per insurer/LOB from loss_triangles (populated by
    the naic_schedp / investor_supp ingestors once validated on the Mac mini),
    flags adverse development vs. the prior estimate, and shows the result.
    """
    from digest import reserving as reserving_mod

    db.init_db()
    console.rule("[bold cyan]reserving")
    counts = reserving_mod.run_reserving()
    if counts["triangles"] == 0:
        console.print("[yellow]No loss triangles yet.[/yellow] "
                      "Enable naic_schedp / investor_supp ingestors on the Mac mini.")
        return
    console.print(f"[green]✓[/green] computed {counts['computed']}/{counts['triangles']} estimates")
    rows = db.latest_reserving_signals(limit=20)
    if rows:
        table = Table(title="Reserving — IBNR & development")
        table.add_column("Insurer", no_wrap=True)
        table.add_column("LOB", no_wrap=True)
        table.add_column("Metric", no_wrap=True, style="dim")
        table.add_column("IBNR", justify="right")
        table.add_column("Δ vs prior", justify="right")
        for r in rows:
            det = r["deterioration_pct"]
            if det is None:
                delta = "[dim]—[/dim]"
            else:
                colour = "red" if r["direction"] == "adverse" else "green"
                delta = f"[{colour}]{det:+.1%} {r['direction']}[/{colour}]"
            table.add_row(r["insurer"], r["lob"], r["metric"],
                          f"{r['ibnr']:,.0f}" if r["ibnr"] is not None else "—", delta)
        console.print(table)


@main.command(name="cat-nowcast")
def cat_nowcast() -> None:
    """Federal-disaster velocity nowcast for the regime cat_load axis (Lead 2).

    Pulls monthly distinct disaster-declaration counts from OpenFEMA (free, no
    key), z-scores the latest month vs the trailing-12m baseline, and stores the
    reading so `digest regime` can escalate cat_load on an anomalous surge.
    """
    from digest import cat_nowcast as nowcast_mod

    db.init_db()
    console.rule("[bold cyan]cat-nowcast")
    counts = nowcast_mod.run_cat_nowcast()
    if counts["written"] == 0:
        console.print("[yellow]No nowcast data[/yellow] — OpenFEMA returned too few months.")
        return
    sig = nowcast_mod.nowcast_signal()
    z = sig.get("declaration_z")
    flag = "[red]⚠ anomalous surge[/red]" if counts["anomaly"] else "[green]normal[/green]"
    console.print(
        f"[green]✓[/green] {counts['written']} months stored · latest z="
        f"[bold]{z:+.2f}[/bold] {flag}" if z is not None
        else f"[green]✓[/green] {counts['written']} months stored"
    )


@main.command()
@click.option("--window", default=90, help="Trailing window in days")
def burden(window: int) -> None:
    """Per-state regulatory-burden barometer (Lead 9).

    Intensity-weighted count of regulatory_rate items by US state over the
    trailing window — the local analog of gold.burden_by_state. Populated by the
    triage `state` field; empty until regulatory_rate items with a state are
    triaged.
    """
    db.init_db()
    rows = db.burden_by_state(window_days=window)
    if not rows:
        console.print("[yellow]No state-tagged regulatory items yet.[/yellow]")
        return
    table = Table(title=f"Regulatory burden by state ({window}d)")
    table.add_column("State", no_wrap=True)
    table.add_column("Items", justify="right")
    table.add_column("Weighted", justify="right")
    table.add_column("Net dir", justify="right")
    for r in rows:
        nd = r["net_direction"] or 0
        arrow = "↑" if nd > 0 else "↓" if nd < 0 else "·"
        colour = "red" if nd > 0 else "green" if nd < 0 else "white"
        table.add_row(r["state"], str(r["n"]), str(r["weighted_burden"] or 0),
                      f"[{colour}]{arrow} {nd:+d}[/{colour}]")
    console.print(table)


@main.command()
def litigation() -> None:
    """National litigation-pressure index for the TPLF boost (Lead 4).

    Composes nuclear-verdict counts / median awards (Marathon), TPLF commitments
    (Westfleet) and CourtListener docket velocity into a 0-100 pressure index.
    v1 computes the live docket-velocity component; the verdict/TPLF components
    are pending scraper validation, so the index stays conservative until then.
    """
    from digest import litigation as litigation_mod

    db.init_db()
    console.rule("[bold cyan]litigation")
    counts = litigation_mod.run_litigation()
    p = litigation_mod.pressure_signal()
    console.print(
        f"[green]✓[/green] national pressure index = [bold]{p:.1f}[/bold]/100 "
        f"[dim](docket-velocity component live; verdict/TPLF pending)[/dim]"
    )


@main.command(name="severity-tape")
def severity_tape() -> None:
    """Blended loss-cost severity index for the inflation boost (Lead 3).

    Blends the FRED parts/labor/used-car/medical series already tracked into one
    severity z-score so `digest signals` can magnitude-scale the inflation-keyword
    boost when the loss-cost regime is hot. Needs FRED_API_KEY.
    """
    from digest import severity_tape as tape_mod

    db.init_db()
    console.rule("[bold cyan]severity-tape")
    counts = tape_mod.run_severity_tape()
    if counts["written"] == 0:
        console.print("[yellow]No severity data[/yellow] — check FRED_API_KEY / fred_series.yaml.")
        return
    z = tape_mod.severity_regime()
    flag = "[red]⚠ hot[/red]" if counts.get("anomaly") else "[green]normal[/green]"
    console.print(
        f"[green]✓[/green] {counts['components']} FRED components → blended z="
        f"[bold]{z:+.2f}[/bold] {flag}"
    )


@main.command()
def disclosure() -> None:
    """Reserve-tone NLP over stored EDGAR filings (EKG Lead 5).

    Scores the reserve language in each insurer's recent EDGAR filings (8-K
    earnings releases / 10-Q / 10-K) with a deterministic reserve-tone lexicon
    and feeds an adverse-tone reading into the same reserve-deterioration boost
    as Lead 6 — a language signal that leads the chain-ladder number.
    """
    from digest import disclosure as disclosure_mod

    db.init_db()
    console.rule("[bold cyan]disclosure")
    counts = disclosure_mod.run_disclosure()
    if counts["filings"] == 0:
        console.print("[yellow]No EDGAR filings with content yet.[/yellow] "
                      "Run [bold]digest ingest edgar[/bold] first.")
        return
    console.print(f"[green]✓[/green] scored {counts['scored']}/{counts['filings']} filings")
    rows = db.latest_disclosure_sentiment(limit=20)
    if rows:
        table = Table(title="Disclosure sentiment — reserve tone")
        table.add_column("Insurer", no_wrap=True)
        table.add_column("Period", no_wrap=True, style="dim")
        table.add_column("Tone", no_wrap=True)
        table.add_column("Adverse", justify="right")
        table.add_column("Filing", style="dim", no_wrap=True)
        for r in rows:
            tone = r["reserve_tone"] or "neutral"
            colour = {"strengthening": "red", "releasing": "green"}.get(tone, "white")
            score = r["adverse_language_score"]
            table.add_row(
                r["insurer"], r["period"], f"[{colour}]{tone}[/{colour}]",
                f"{score:.2f}" if score is not None else "—",
                (r["source_filing"] or "")[:32],
            )
        console.print(table)


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
    run_ingest(INGESTORS, list(INGESTORS), run_type, console, per_source_rule=False)

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

    # Alpha engine — refresh the daily price store (insurers + benchmarks) so the
    # outcomes σ label and the returns model see fresh closes. Best-effort: a
    # vendor throttle never blocks the digest.
    console.rule("[bold cyan]stage 5b: price store")
    try:
        from digest.prices import run_prices
        px = run_prices()
        console.print(f"  [green]✓[/green] {px['rows']:,} price rows "
                      f"({len(px['skipped'])} tickers skipped)")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]price store skipped:[/yellow] {exc}")

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
@click.option("--calibration/--no-calibration", default=True,
              help="Also run the outcomes backtest + learned-scorer training "
                   "(the weekly launchd job does; pass --no-calibration for a note-only run).")
def weekly(date_iso: str | None, calibration: bool) -> None:
    """Generate the weekly synthesis note, run the calibration loop, then
    refresh the dashboard snapshots.

    The weekly job is the natural cadence for the outcome backtest (items need
    the 7d/30d window to mature) and the learned-scorer training it unblocks.
    Finally it regenerates the Signal Desk + Home cockpit so the _meta/ visuals
    stay current. All three run best-effort so a failure never blocks the note.
    """
    from digest.obsidian import publish_weekly

    db.init_db()

    # Refresh the severity tape first (monthly FRED loss-cost levels) so the note
    # and calibration loop see the freshest loss-cost regime. Weekly is ample for
    # monthly CPI/PPI data. Best-effort: a FRED hiccup never blocks the note.
    console.rule("[bold cyan]severity tape")
    try:
        from digest.severity_tape import run_severity_tape
        st = run_severity_tape()
        console.print(
            f"  [green]✓[/green] {st['components']} components, {st['written']} rows"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]severity tape skipped:[/yellow] {exc}")

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

    # Calibration / learning loop — backtest matured items, then (re)train the
    # learned scorer on the labels. Best-effort: each is wrapped so a failure
    # (or a too-small label set) just logs and the job still exits cleanly.
    if calibration:
        console.rule("[bold cyan]calibration loop")
        try:
            from digest.outcomes import run_outcomes
            oc = run_outcomes()
            console.print(f"  [green]✓[/green] outcomes: {oc}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [yellow]outcomes skipped:[/yellow] {exc}")
        try:
            from digest import learn as learn_mod
            # Prefer the 30d horizon; fall back to 7d so the learned scorer
            # self-starts before 30d items have matured.
            ls = learn_mod.run_best(horizons=(30, 7))
            if ls.get("model_id"):
                console.print(f"  [green]✓[/green] learn: model {ls['model_id']} "
                              f"trained @ {ls.get('horizon_days')}d (AUC "
                              f"{ls.get('auc') if ls.get('auc') is not None else '—'})")
            else:
                console.print("  [dim]learn: not enough labeled items yet (need ≥12)[/dim]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [yellow]learn skipped:[/yellow] {exc}")

    # Refresh the Signal Desk dashboard + Home cockpit so the _meta/ snapshot
    # visuals track the freshest DB state on the weekly cadence. Runs after the
    # calibration loop (when enabled) so the calibration heatmap reflects it.
    # Best-effort: a render failure never blocks the weekly job.
    console.rule("[bold cyan]dashboard refresh")
    try:
        from digest.dashboard import write_dashboard
        for p in write_dashboard():
            console.print(f"  [green]✓[/green] {p}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [yellow]dashboard skipped:[/yellow] {exc}")


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
@click.option("--lab", is_flag=True,
              help="Generate the Viz Lab eval harness note (_meta/) instead of the claims-trend pages")
def viz(open_after: bool, lab: bool) -> None:
    """Generate claims trend SVG visualizations into the Obsidian vault.

    With --lab, instead renders every candidate Markdown viz technique against the
    same live SQLite slice into `_meta/Viz Lab (DATE).md` — a side-by-side eval
    harness to pick which graduate to the daily-note EKG header / dashboard.
    """
    db.init_db()
    console.rule("[bold cyan]viz")
    try:
        if lab:
            from digest.viz_lab import write_viz_lab
            console.print(f"  [green]✓[/green] {write_viz_lab(open_after=open_after)}")
            return
        from digest.viz import write_viz_pages
        for p in write_viz_pages(open_after=open_after):
            console.print(f"  [green]✓[/green] {p}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
@click.option("--open", "open_after", is_flag=True, help="Open the dashboard in the OS default app")
def dashboard(open_after: bool) -> None:
    """Generate the Signal Desk dashboard + Home cockpit notes into _meta/ (Phase C).

    Signal Desk = live DataviewJS regime/vitals timeline across Daily/ notes +
    reserve Sankey + catastrophe heatmap calendar + calibration heatmap. Home =
    status strip + pipeline buttons + quick links.
    """
    db.init_db()
    console.rule("[bold cyan]dashboard")
    try:
        from digest.dashboard import write_dashboard
        for p in write_dashboard(open_after=open_after):
            console.print(f"  [green]✓[/green] {p}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]✗[/red] {exc}")


@main.command()
def models() -> None:
    """Show the LLM wired to each pipeline stage + whether it's reachable.

    The plug-and-play control panel: every stage routes through the backend
    registry, so to upgrade you change a *_BACKEND / *_MODEL env var (see
    config.py) and re-run this to confirm the new model is reachable BEFORE a
    real run. ✓ present = the model is pulled (Ollama) / loaded (MLX).
    """
    from digest import health
    from digest_core.summarize.backends import BackendConfig
    s = settings

    ollama = health.check_ollama()
    mlx = health.check_mlx()
    o_models = ollama["details"].get("models_available") if ollama["status"] != "fail" else None
    m_models = mlx["details"].get("models") if mlx["status"] != "fail" else None

    def present(model: str, available) -> str:
        if available is None:
            return "[red]✗ server unreachable[/red]"
        return ("[green]✓ present[/green]" if any(model in x for x in available)
                else "[yellow]⚠ not pulled/loaded[/yellow]")

    def cell(backend: str):
        """(model, endpoint, status) for a backend name."""
        if backend == "local_qwen":
            return s.ollama_model, s.ollama_host, present(s.ollama_model, o_models)
        if backend == "mlx_local":
            return s.mlx_model, s.mlx_server_url, present(s.mlx_model, m_models)
        if backend == "claude_cli_pro":
            ok = health.check_claude_cli().get("status") == "ok"
            return s.summarizer_model, "claude CLI", (
                "[green]✓ CLI ok[/green]" if ok else "[yellow]⚠ CLI not found[/yellow]")
        if backend == "haiku_api":
            return BackendConfig().haiku_model, "Anthropic API", (
                "[green]key set[/green]" if s.anthropic_api_key else "[red]✗ key unset[/red]")
        if backend == "gemini_flash_free":
            return BackendConfig().gemini_model, "Google AI Studio", (
                "[green]key set[/green]" if s.gemini_api_key else "[red]✗ key unset[/red]")
        return "?", "?", f"[red]✗ unknown backend {backend!r}[/red]"

    rows = [
        ("triage",     s.triage_backend,     *cell(s.triage_backend)),
        ("summarize",  s.summarizer_backend, *cell(s.summarizer_backend)),
        ("regime",     s.summarizer_backend, *cell(s.summarizer_backend)),
        ("embeddings", "ollama", s.embedding_model, s.ollama_host, present(s.embedding_model, o_models)),
        ("weekly",     "claude_cli_pro",     *cell("claude_cli_pro")),
    ]
    table = Table(title="LLM models by pipeline stage")
    table.add_column("Stage", style="bold")
    table.add_column("Backend")
    table.add_column("Model")
    table.add_column("Endpoint", overflow="fold")
    table.add_column("Status")
    for r in rows:
        table.add_row(*[str(x) for x in r])
    console.print(table)
    console.print(
        "[dim]Upgrade: change the stage's *_BACKEND / *_MODEL env var, "
        "`ollama pull` (or restart MLX with) the new model, re-run `digest models`.[/dim]"
    )


@main.command("init-db")
def init_db_cmd() -> None:
    """Create the SQLite DB and schema."""
    db.init_db()
    console.print(f"[green]✓[/green] DB initialized at {settings.db_path}")


if __name__ == "__main__":
    sys.exit(main())
