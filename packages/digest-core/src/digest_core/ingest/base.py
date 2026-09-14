"""Framework base class for ingestors.

`IngestorBase.run()` is the fetch → persist → log skeleton with timing and
exception capture. It stays domain-agnostic by taking persistence through an
injected `ItemStore` instead of importing a domain `db` module: a domain binds
its store on a subclass —

    class IngestorBase(CoreIngestorBase):
        store = db          # a module/object satisfying ItemStore

PC Digest and macro-ai-digest run as separate processes, so a single
class-level store per process is sufficient (no per-instance injection needed).
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import ClassVar, Protocol, runtime_checkable

from digest_core.types import IngestedItem

logger = logging.getLogger(__name__)


@runtime_checkable
class ItemStore(Protocol):
    """Persistence contract `IngestorBase.run()` depends on.

    A domain `db` module exposing module-level `upsert_items` / `log_run`
    functions of these shapes satisfies it structurally.
    """

    def upsert_items(self, items: list[IngestedItem]) -> int: ...

    def log_run(
        self,
        *,
        run_type: str,
        source: str,
        items_fetched: int,
        items_new: int,
        duration_ms: int,
        status: str,
        error: str | None = None,
    ) -> None: ...


class IngestorBase(ABC):
    """Base class — subclasses implement fetch(); run() handles persist + log.

    Bind persistence by setting `store` on a domain subclass. Calling run()
    on a subclass with no store configured raises RuntimeError rather than
    silently dropping fetched items.
    """

    name: str = "base"
    store: ClassVar[ItemStore | None] = None

    #: Status of the most recent `run()` — "ok" or "error". `run()` swallows
    #: fetch/persist failures so one bad source can't kill a pipeline, so a
    #: subclass doing post-run work (e.g. marking source files consumed) must
    #: check this before assuming anything was actually written.
    last_status: str = "ok"

    #: Opt in to domain-level full-text enrichment (see `enrich_items`). Set it
    #: on sources whose feeds carry excerpts rather than whole articles.
    enrich_fulltext: bool = False

    def __init_subclass__(cls, register: bool = True, **kwargs: object) -> None:
        """Auto-register concrete ingestors so the catalog grows by itself.

        Defining `class FooIngestor(IngestorBase): name = "foo"` is enough to
        make `foo` a known source — no central dict to edit. The un-named
        framework/domain base classes and abstract subclasses are skipped (see
        `registry.register`). Pass `register=False` for a base you don't want
        catalogued (e.g. a shared mixin).
        """
        super().__init_subclass__(**kwargs)
        if register:
            from digest_core.ingest.registry import register as _register
            _register(cls)

    @abstractmethod
    def fetch(self) -> list[IngestedItem]:
        """Pull fresh items from this source. Do not write to the store."""

    def enrich_items(self, items: list[IngestedItem]) -> list[IngestedItem]:
        """Post-process fetched items before they are persisted. Default no-op.

        A domain binds real behaviour here (full-text extraction) so every
        ingestor picks it up, current and future, instead of each one having to
        remember to call it.
        """
        return items

    def enrich_url(self, item: IngestedItem) -> str | None:
        """URL to pull full text from, or None to skip this item.

        Defaults to the item's own link. Override where that link isn't the
        article — an HN self-post's url is its discussion thread, and extracting
        that would file the comment page as the item's body.
        """
        return item.url

    def run(self, run_type: str = "manual") -> tuple[int, int]:
        """Fetch, persist, log. Returns (fetched, new)."""
        store = self.store
        if store is None:
            raise RuntimeError(
                f"{type(self).__name__}.store is not configured — a domain must "
                "bind an ItemStore (e.g. `class IngestorBase(CoreBase): store = db`)."
            )

        start = time.perf_counter()
        fetched = 0
        new = 0
        status = "ok"
        error_msg: str | None = None

        try:
            items = self.enrich_items(self.fetch())
            fetched = len(items)
            new = store.upsert_items(items)
            logger.info("[%s] fetched=%d new=%d", self.name, fetched, new)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("[%s] failed: %s", self.name, error_msg)
        finally:
            self.last_status = status
            duration_ms = int((time.perf_counter() - start) * 1000)
            store.log_run(
                run_type=run_type,
                source=self.name,
                items_fetched=fetched,
                items_new=new,
                duration_ms=duration_ms,
                status=status,
                error=error_msg,
            )

        return fetched, new
