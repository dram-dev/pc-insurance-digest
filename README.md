# P&C Insurance & Financial Services Digest

Daily + weekly curated digest covering US P&C insurance and financial services.
Sibling to [macro-ai-digest](../macro-ai-digest); shares MLX/Ollama backends and
the same Obsidian vault (lands in `81 P&C Digest` next to `80 Digest`).

## Wave 1 scope

- **Ingestors:** EDGAR (named insurer universe), trade-press RSS (Insurance
  Journal, Reinsurance News, Artemis, Carrier Management, Insurance ERM, others),
  Reddit (r/Insurance, r/Actuary, r/CFP, weather/EQ subreddits), Substack
  (Insurance Insider, etc.), Hacker News
- **Topic taxonomy:** 17 P&C topics + 1 sub-tag (see `src/digest/triage.py` for
  the canonical list and the triage prompt)
- **Triage:** Ollama Qwen2.5:14b with a P&C-specific prompt. Python-side
  hybrid auto-keep enforces EDGAR 8-K/10-K/10-Q from named insurers without
  calling the model (cannot silently fail on material disclosures)
- **Summarize:** MLX-LM local server (Qwen3.5-27B), shared with macro digest
- **Publish:** Daily + weekly notes to Obsidian vault, grouped by topic
- **Skipped for Wave 1:** dashboard, signals leaderboard, weekly essay,
  bull/bear debate, backtest, velocity, calendar ingestor, sentiment,
  entities, stock tracker, market-cycle regime detector (Wave 2 / 3)

## Schedule

Staggered with macro digest to avoid MLX contention:

| Job | When |
|---|---|
| am pipeline | daily 04:00 (3h after macro am) |
| pm pipeline | daily 16:00 (3h after macro pm) |
| weekly | Sat 06:00 (after macro Fri-night batch wraps) |

## Prerequisites

- Python 3.12+
- `uv` (`brew install uv`)
- Ollama running locally with `qwen2.5:14b` pulled
- MLX server (managed by macro digest's `com.dr.mlx.server` launchd job)
- Reddit script-type app credentials
- EDGAR user agent string (your email, per SEC policy)

## Getting started

```bash
cd ~/Projects/pc-insurance-digest
uv sync
cp .env.example .env       # then fill in REDDIT_*, EDGAR_USER_AGENT, OBSIDIAN_VAULT_PATH
uv run digest init-db
uv run digest ingest all
uv run digest stats
uv run digest pipeline --run-type manual
```

## Scheduling

```bash
bash scripts/install_launchd.sh
launchctl list | grep com.dr.pcdigest
```

## Project layout

```
pc-insurance-digest/
├── pyproject.toml
├── README.md
├── config/
│   ├── edgar_tickers.yaml     # Wave 1 insurer universe (must stay in sync with triage.py)
│   ├── rss_feeds.yaml         # trade press + Google News searches
│   ├── subreddits.yaml        # r/Insurance, r/Actuary, weather/EQ
│   └── substack_feeds.yaml    # Insurance Insider, Coverager, etc.
├── launchd/
│   ├── com.dr.pcdigest.am.plist
│   ├── com.dr.pcdigest.pm.plist
│   └── com.dr.pcdigest.weekly.plist
├── scripts/
│   └── install_launchd.sh
└── src/digest/
    ├── cli.py                 # entry point (ingest, triage, summarize, pipeline, publish, weekly, health)
    ├── config.py
    ├── db.py
    ├── triage.py              # P&C triage prompt + Python auto-keep hook
    ├── summarize.py
    ├── obsidian.py            # writes to 81 P&C Digest/{Daily,Topics,Weekly}/
    ├── health.py
    ├── security.py
    └── ingest/
        ├── base.py
        ├── edgar.py
        ├── rss.py
        ├── reddit.py
        ├── substack.py
        └── hackernews.py
```

## Wave 2 / 3 roadmap (deferred)

- **Wave 2:** NOAA/NHC/USGS catastrophe event ingestors; market-cycle
  (hard/soft) + CAT-load regime detector; signals leaderboard
- **Wave 3:** AM Best rating actions, NAIC + state DOI (SERFF) rate filings,
  Lloyd's / Bermuda reinsurance market

When divergence between this project and `macro-ai-digest` settles after Wave 2,
extract the shared core into a `digest-core` framework package and let both
projects become thin domain plug-ins.
