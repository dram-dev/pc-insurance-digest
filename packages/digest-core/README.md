# digest-core (scaffold)

Shared framework for digest-style projects:

- [pc-insurance-digest](https://github.com/dram-dev/pc-insurance-digest) — US P&C
- [macro-ai-digest](https://github.com/dram-dev/macro-ai-digest) — Macro & AI

## Status

**Scaffold only.** No implementation yet. This package is intentionally empty
until the Wave 2 dogfooding window for pc-insurance-digest closes (≤ 1 week
of daily runs). See [EXTRACTION_PLAN.md](EXTRACTION_PLAN.md) for the concrete
seam analysis: what moves here, what stays domain, and which abstractions
need design work before extraction.

## When to extract

Trigger (from pc-insurance-digest's CLAUDE.md):

> Wave 2 lands → ≤ 1 week dogfooding → extraction begins.

At extraction time, both pc-insurance-digest and macro-ai-digest become thin
domain plug-ins that depend on `digest-core` and register:

- Topic taxonomy + display labels
- Triage system prompt
- Ingestor list
- Regime axes (PC has 2, macro has 1)
- Signal scoring weights + topic priority boosts
- Obsidian display structure

## Layout

```
packages/digest-core/
├── pyproject.toml
├── README.md
├── EXTRACTION_PLAN.md
└── src/digest_core/
    ├── __init__.py
    ├── ingest/       # IngestorBase, common ingestors (rss, reddit, edgar, …)
    ├── db/           # base schema, conn helpers, migrations
    ├── triage/       # TriageEngine + auto-keep preprocessor pattern
    ├── summarize/    # backend abstraction (claude_cli_pro, mlx, …) + runner
    ├── regime/       # RegimeDetectorBase, hysteresis, override loader
    ├── signals/      # leaderboard scoring scaffold
    ├── obsidian/     # paths, callouts, frontmatter, archive upsert
    └── cli/          # Click group factory; domains register commands
```
