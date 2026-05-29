"""Generic CLI building blocks for digest-style projects.

Reusable mechanics a domain's Click CLI composes: dynamic ingestor loading and
the ingest loop. The Click group, command set, logging setup, and the INGESTORS
registry stay domain-side. A fuller group-factory / command-registration seam
is intentionally deferred until a second domain (macro-ai-digest) is ported
onto core, so we don't pre-design the wrong shape.

`run_ingest` takes a `console` duck-typed (anything with rich-style `.rule()` /
`.print()`), so this module stays dependency-free.
"""
from __future__ import annotations

import importlib
from typing import Any, Iterable


def load_ingestor(dotted: str) -> Any:
    """Resolve a 'package.module:ClassName' string to the class object."""
    module_path, class_name = dotted.split(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def run_ingest(
    ingestors: dict[str, str],
    names: Iterable[str],
    run_type: str,
    console: Any,
    *,
    per_source_rule: bool = True,
) -> tuple[int, int]:
    """Run the named ingestors (resolved from the {name: 'mod:Class'} map),
    print per-source results, and return (total_fetched, total_new).

    Per-source failures are caught + printed so one bad source can't abort the
    batch. `per_source_rule` draws a rule before each source (off for the
    compact pipeline-stage view).
    """
    total_fetched = 0
    total_new = 0
    for name in names:
        if per_source_rule:
            console.rule(f"[bold cyan]{name}")
        try:
            cls = load_ingestor(ingestors[name])
            fetched, new = cls().run(run_type=run_type)
            total_fetched += fetched
            total_new += new
            console.print(f"[green]✓[/green] {name}: fetched={fetched} new={new}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗[/red] {name}: {exc}")
    return total_fetched, total_new
