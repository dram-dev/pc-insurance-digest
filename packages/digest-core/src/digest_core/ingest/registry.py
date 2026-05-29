"""Ingestor registry — the 'grow organically' seam.

Adding a new source should be: drop a file, subclass `IngestorBase`, give it a
`name`. That's it — no editing a central dict in the CLI. Every concrete
`IngestorBase` subclass auto-registers here via `IngestorBase.__init_subclass__`
(see base.py), keyed by its `name`.

The registry only knows about ingestors whose module has been imported, so a
domain calls `discover(package)` once at startup to import every submodule of
its `digest.ingest` package. Discovery is import-isolated: a submodule that
fails to import (e.g. a missing optional dependency) is recorded in
`import_failures()` and skipped rather than aborting the whole catalog. That is
strictly friendlier than the old static map, where a broken source surfaced only
when you tried to run it.

A domain may set optional class attributes for richer cataloging:

    class FooIngestor(IngestorBase):
        name = "foo"
        tags = ("wave2", "hazard")   # free-form grouping labels
        order = 20                   # sort hint (default 100)
        doc  = "One-line description of what this source pulls."
"""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestorSpec:
    """A registered ingestor and its catalog metadata."""

    name: str
    cls: type
    tags: tuple[str, ...] = ()
    order: int = 100
    doc: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.cls.__module__}:{self.cls.__qualname__}"


_REGISTRY: dict[str, IngestorSpec] = {}
_IMPORT_FAILURES: dict[str, str] = {}


def register(cls: type) -> None:
    """Register a concrete ingestor class by its `name`.

    Called automatically from `IngestorBase.__init_subclass__`. Abstract bases
    and the un-named framework base are skipped. Re-registering a name (e.g. a
    module re-imported under autoreload) overwrites the prior entry.
    """
    name = getattr(cls, "name", None)
    if not name or name == "base" or inspect.isabstract(cls):
        return
    _REGISTRY[name] = IngestorSpec(
        name=name,
        cls=cls,
        tags=tuple(getattr(cls, "tags", ()) or ()),
        order=int(getattr(cls, "order", 100)),
        doc=(inspect.getdoc(cls) or "").strip().split("\n")[0],
    )


def registered() -> dict[str, IngestorSpec]:
    """All registered ingestors, ordered by (order, name) for stable display."""
    return dict(
        sorted(_REGISTRY.items(), key=lambda kv: (kv[1].order, kv[1].name))
    )


def get(name: str) -> IngestorSpec | None:
    return _REGISTRY.get(name)


def import_failures() -> dict[str, str]:
    """{module_path: error_repr} for submodules that failed to import."""
    return dict(_IMPORT_FAILURES)


def discover(package: str) -> dict[str, IngestorSpec]:
    """Import every submodule of `package` so subclass registration fires.

    Import errors per submodule are isolated, logged, and recorded in
    `import_failures()`. Returns the full registry afterwards. Safe to call
    repeatedly (imports are cached by the import system).
    """
    try:
        pkg = importlib.import_module(package)
    except ImportError as exc:
        logger.warning("ingestor discovery: package %s not importable (%s)", package, exc)
        return registered()

    paths = getattr(pkg, "__path__", None)
    if paths is None:  # not a package
        return registered()

    for mod in pkgutil.iter_modules(paths, pkg.__name__ + "."):
        if mod.name.rsplit(".", 1)[-1].startswith("_"):
            continue  # skip private / dunder modules
        try:
            importlib.import_module(mod.name)
            _IMPORT_FAILURES.pop(mod.name, None)
        except Exception as exc:  # noqa: BLE001 — one bad source can't break the catalog
            _IMPORT_FAILURES[mod.name] = f"{type(exc).__name__}: {exc}"
            logger.warning("ingestor discovery: %s skipped (%s)", mod.name, exc)
    return registered()
