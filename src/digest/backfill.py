"""Historical EDGAR backfill — open the learning gates with already-matured labels.

The calibrator (≥100 labels), log-linear gate (≥300) and Bühlmann credibility
table wait on outcome labels that normally accrue ~daily. But the expensive half
of each label already exists historically: EDGAR archives go back years and the
price store holds ~2y of closes, so the 7d/30d outcomes for a historical filing
are ALREADY matured. This module ingests historical insurer filings (8-K/10-Q/
10-K for the named ticker universe), auto-keeps them through the same
deterministic hook as live triage, summarizes them through the normal MLX path,
scores them AS-OF their historical ingestion time, and runs the outcome backtest
immediately — hundreds of labels in one overnight run.

Disciplines (per the captured design — CLAUDE.md "Next ideas" #1):

  * **As-of correctness.** A backfilled item's `ingested_at` is its FILING date
    (+16h, ~market close), never now — so outcome windows, label maturity and
    the chronological train/test split all see the true event time. Its score
    row is computed with `as_of` = ingested_at + one scoring lag: recency decays
    from the filing date, and `regime_mult` is the regime in force AT that time
    (neutral 1.0 before regime history begins — today's multiplier must not
    leak backward).
  * **Provenance.** Every backfilled item carries metadata `"backfill": true`.
    Backfilled rows are EXCLUDED from live signal scoring (db.items_for_signals)
    so the next `digest signals` run cannot clobber the as-of score with a
    recency-floored, current-regime rescore — they are training fodder, not
    leaderboard content (windowed leaderboard queries also never see them,
    since both ingested_at and computed_at are historical).
  * **Live-mix gating.** The EDGAR-heavy label mix is fine for per-source
    Bühlmann credibility, but the pooled log-linear gate additionally requires
    `loglinear.MIN_LIVE_LABELED` live (non-backfill) labels before it can pass,
    and `digest learn` reports the backfill/live split.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from digest import db
from digest.ingest.base import IngestedItem
from digest.ingest.edgar import EdgarIngestor, FILING_URL, SUBMISSIONS_URL
from digest.regime import (
    CAT_LOAD_MULT,
    DEFAULT_CAT_LOAD,
    DEFAULT_MARKET_CYCLE,
    MARKET_CYCLE_MULT,
    RegimeSignal,
)
from digest.triage import INSURER_TICKERS_WAVE1

logger = logging.getLogger(__name__)

# Historical scope: forms with body content + a deterministic auto-keep path.
# 13F-HR is excluded — holdings snapshots carry no loss-cost signal and the
# outcome detectors have nothing to corroborate against them.
BACKFILL_FORMS = {"8-K", "10-Q", "10-K"}

# Filing date (midnight UTC from EDGAR) + this offset = synthetic ingested_at.
# ~16:00 UTC ≈ noon ET: filings cluster around the US market day, and the
# offset keeps the synthetic timestamp unambiguous next to live ones.
INGESTED_AT_OFFSET_HOURS = 16.0

# As-of scoring lag: live items are scored by the next am/pm signals run, so
# emulate "first scored about a day after ingestion". Recency at the default
# 7d half-life ≈ 0.9 — a fresh item, exactly like live first-scoring.
SCORING_LAG_HOURS = 24.0

DEFAULT_LOOKBACK_DAYS = 540   # ~18 months of filings when --since is omitted

# The stock_move detector needs ~20 trailing trading days of closes before an
# item to estimate vol; clamp the window so backfilled items aren't structurally
# unlabelable by the strongest detector.
_PRICE_WARMUP_DAYS = 45


def default_since() -> str:
    """Earliest backfill filing date: ~18 months back, clamped to the price
    store's coverage (+ vol warm-up) when a benchmark history exists."""
    base = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    bench = db.price_closes("SPY") or db.price_closes("IAK")
    if not bench:
        return base
    floor = (
        datetime.fromisoformat(min(bench)) + timedelta(days=_PRICE_WARMUP_DAYS)
    ).strftime("%Y-%m-%d")
    return max(base, floor)


# ── Historical filing discovery ────────────────────────────────────────


def select_historical_filings(pages: list[dict], since: str) -> list[dict]:
    """Flatten EDGAR submissions pages (parallel arrays) into filing dicts,
    keeping BACKFILL_FORMS filed on/after `since`. Pure — unit-testable."""
    out: list[dict] = []
    for page in pages:
        forms = page.get("form", [])
        dates = page.get("filingDate", [])
        accessions = page.get("accessionNumber", [])
        primary_docs = page.get("primaryDocument", [])
        for i, form in enumerate(forms):
            if form not in BACKFILL_FORMS:
                continue
            if i >= len(dates) or dates[i] < since:
                continue
            out.append({
                "form": form,
                "filing_date": dates[i],
                "accession": accessions[i],
                "primary_doc": primary_docs[i] if i < len(primary_docs) else "",
            })
    out.sort(key=lambda d: d["filing_date"])
    return out


def _submission_pages(cik: str, headers: dict, since: str) -> list[dict]:
    """The `filings.recent` page plus any older archive pages overlapping
    `since`. Older pages live at data.sec.gov/submissions/<name> and hold the
    same parallel arrays at top level."""
    r = requests.get(SUBMISSIONS_URL.format(cik=cik), headers=headers, timeout=20)
    r.raise_for_status()
    filings = r.json().get("filings", {})
    pages = [filings.get("recent", {})]
    for f in filings.get("files", []):
        if f.get("filingTo", "") < since:
            continue
        try:
            older = requests.get(
                f"https://data.sec.gov/submissions/{f['name']}", headers=headers, timeout=20
            )
            older.raise_for_status()
            pages.append(older.json())
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill: older submissions page %s failed: %s", f.get("name"), exc)
    return pages


def fetch_backfill_items(
    since: str,
    tickers: tuple[str, ...] = (),
    fetch_content: bool = True,
) -> list[IngestedItem]:
    """Historical filings for the insurer universe as IngestedItems, skipping
    filings already stored. Content policy mirrors live ingest (8-K → EX-99.1;
    10-Q/10-K → head + financial/reserve excerpts) with no age cap — the whole
    point is that these are old."""
    ingestor = EdgarIngestor()
    seen = db.existing_source_ids("edgar")
    wanted = {t.upper() for t in tickers} if tickers else None
    items: list[IngestedItem] = []

    for entry in ingestor.config["companies"]:
        ticker = entry["ticker"]
        if entry.get("fund", False):
            continue
        if wanted is not None and ticker.upper() not in wanted:
            continue
        cik = str(entry["cik"]).zfill(10)
        cik_int = str(int(cik))
        entity_name = entry.get("name", ticker)
        try:
            pages = _submission_pages(cik, ingestor.headers, since)
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill: submissions fetch failed for %s: %s", ticker, exc)
            continue

        n_ticker = 0
        for f in select_historical_filings(pages, since):
            source_id = f"{ticker}:{f['accession']}"
            if source_id in seen:
                continue
            url = FILING_URL.format(
                cik_int=cik_int,
                accession_no_dashes=f["accession"].replace("-", ""),
                primary_doc=f["primary_doc"],
            )
            content = None
            if fetch_content:
                try:
                    content = ingestor._fetch_content(f["form"], cik_int, f["accession"], url)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("backfill: content fetch failed for %s: %s", source_id, exc)
            published = datetime.strptime(f["filing_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            items.append(IngestedItem(
                source="edgar",
                source_id=source_id,
                title=f"{ticker} {f['form']} filed {f['filing_date']}",
                url=url,
                author=entity_name,
                content=content,
                published_at=published,
                metadata={
                    "ticker": ticker,
                    "cik": cik,
                    "form": f["form"],
                    "accession": f["accession"],
                    "primary_document": f["primary_doc"],
                    "topic_hint": "underwriting_results",
                    "backfill": True,
                },
            ))
            n_ticker += 1
        logger.info("backfill: %s — %d historical filings since %s", ticker, n_ticker, since)
    return items


# ── As-of scoring ──────────────────────────────────────────────────────


def _neutral_regime(as_of_iso: str) -> RegimeSignal:
    return RegimeSignal(
        as_of=as_of_iso,
        market_cycle=DEFAULT_MARKET_CYCLE,
        cat_load=DEFAULT_CAT_LOAD,
        market_cycle_mult=MARKET_CYCLE_MULT[DEFAULT_MARKET_CYCLE],
        cat_load_mult=CAT_LOAD_MULT[DEFAULT_CAT_LOAD],
        multiplier=MARKET_CYCLE_MULT[DEFAULT_MARKET_CYCLE] * CAT_LOAD_MULT[DEFAULT_CAT_LOAD],
        evidence={"note": "backfill: no regime history at as-of"},
        source="backfill_neutral",
    )


def regime_at(as_of_iso: str) -> RegimeSignal:
    """The regime in force at `as_of_iso`; neutral (1.0×) before regime history
    begins — today's multiplier must never leak backward onto a 2024 filing."""
    row = db.regime_signal_at(as_of_iso)
    return RegimeSignal.from_row(row) if row is not None else _neutral_regime(as_of_iso)


def score_backfilled(lag_hours: float = SCORING_LAG_HOURS) -> dict:
    """Score every kept+summarized backfill item that has no score row yet,
    AS-OF its historical first-scoring time. The as-of timestamp is persisted
    as computed_at, so recency, regime and the row's place in score history all
    reflect the filing date. Calibrator / severity-tape / litigation-pressure /
    learned-exponent inputs are deliberately omitted — they are fitted on data
    that didn't exist at the as-of time."""
    from digest import signals

    rows = db.backfill_items_for_signals()
    if not rows:
        return {"scored": 0}
    weights = signals._load_scoring_weights()
    regime_cache: dict[str, RegimeSignal] = {}
    scored = []
    for row in rows:
        ingested = datetime.fromisoformat(str(row["ingested_at"]).replace(" ", "T"))
        if ingested.tzinfo is None:
            ingested = ingested.replace(tzinfo=timezone.utc)
        as_of = ingested + timedelta(hours=lag_hours)
        as_of_iso = as_of.isoformat()
        cache_key = as_of_iso[:10]
        regime = regime_cache.get(cache_key)
        if regime is None:
            regime = regime_cache[cache_key] = regime_at(as_of_iso)
        s = signals.score_item(row, regime, weights=weights, as_of=as_of)
        scored.append(s.as_row(as_of_iso))
    inserted = db.upsert_signal_scores(scored)
    logger.info("backfill: scored %d items as-of their filing dates (inserted=%d)",
                len(scored), inserted)
    return {"scored": len(scored), "inserted": inserted}


# ── Orchestration ──────────────────────────────────────────────────────


def run_backfill(
    since: str | None = None,
    tickers: tuple[str, ...] = (),
    fetch_content: bool = True,
    do_summarize: bool = True,
    do_outcomes: bool = True,
    outcome_limit: int = 5000,
) -> dict:
    """ingest → auto-keep → summarize → as-of score → outcomes, end to end.

    Each stage is keyed off the backfill provenance tag (or, for summarize, the
    normal kept+unsummarized queue restricted to source=edgar), so a re-run
    after a partial failure picks up where it left off."""
    since = since or default_since()
    summary: dict = {"since": since}
    t0 = datetime.now(timezone.utc)

    items = fetch_backfill_items(since, tickers, fetch_content=fetch_content)
    summary["fetched"] = len(items)
    summary["new"] = db.insert_backfill_items(items)
    db.log_run(
        run_type="backfill", source="edgar",
        items_fetched=len(items), items_new=summary["new"],
        duration_ms=int((datetime.now(timezone.utc) - t0).total_seconds() * 1000),
        status="ok",
    )

    summary["auto_kept"] = db.auto_keep_insurer_filings(
        tickers=INSURER_TICKERS_WAVE1, form_types=BACKFILL_FORMS
    )

    if do_summarize:
        from digest.summarize import run_summarize
        summary["summarize"] = run_summarize(source="edgar", uncapped=True)

    summary["score"] = score_backfilled()

    if do_outcomes:
        from digest.outcomes import run_outcomes
        summary["outcomes"] = run_outcomes(horizons=(7, 30), limit=outcome_limit)

    return summary
