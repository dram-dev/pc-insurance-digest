"""Generic CLI building blocks for digest-style projects.

Reusable mechanics a domain's Click CLI composes: dynamic ingestor loading and
the ingest loop. The Click group, command set, and logging setup stay
domain-side. A fuller group-factory / command-registration seam is intentionally
deferred until the seams settle, so we don't pre-design the wrong shape.

The source map a domain passes to `run_ingest` no longer has to be a hand-kept
dict — `discover_ingestors(package)` builds it from the registry (every
`IngestorBase` subclass self-registers). Values may be either a class or a
'package.module:ClassName' string, so the static-dict style still works.

`run_ingest` takes a `console` duck-typed (anything with rich-style `.rule()` /
`.print()`), so this module stays dependency-free.
"""
from __future__ import annotations

import importlib
from typing import Any, Iterable

from digest_core.ingest import discover, registered


def load_ingestor(target: Any) -> Any:
    """Resolve an ingestor to its class.

    Accepts a class (returned as-is), a 'package.module:ClassName' string, or
    a registry `IngestorSpec` (anything with a `.cls`).
    """
    if isinstance(target, type):
        return target
    cls_attr = getattr(target, "cls", None)
    if isinstance(cls_attr, type):
        return cls_attr
    module_path, class_name = str(target).split(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def discover_ingestors(package: str) -> dict[str, Any]:
    """Import a domain's ingest `package` and return {name: class} from the
    registry, ordered for stable display. The drop-in replacement for a
    hand-maintained INGESTORS dict.
    """
    return {name: spec.cls for name, spec in discover(package).items()}


def run_ingest(
    ingestors: dict[str, Any],
    names: Iterable[str],
    run_type: str,
    console: Any,
    *,
    per_source_rule: bool = True,
) -> tuple[int, int]:
    """Run the named ingestors (resolved from the {name: class|'mod:Class'} map),
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


# re-export so domains can `from digest_core.cli.base import registered`
__all__ = ["load_ingestor", "discover_ingestors", "run_ingest", "registered"]
