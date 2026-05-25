"""Shared types and base class for ingestors.

`IngestedItem` is re-exported from `digest_core.types` after the digest-core
extraction (2026-05-25) — same dataclass, lifted to the shared package so
macro-ai-digest can import the same shape. `IngestorBase` stays here for
now because its `run()` method calls into PC-specific `db.upsert_items` /
`db.log_run` — lifting `IngestorBase` properly needs a settings-injection
design (deferred to the next extraction pass).
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from digest import db
from digest_core.types import IngestedItem

logger = logging.getLogger(__name__)


class IngestorBase(ABC):
    """Base class — subclasses implement fetch(). run() handles DB + logging."""

    name: str = "base"

    @abstractmethod
    def fetch(self) -> list[IngestedItem]:
        """Pull fresh items from this source. Do not write to DB."""

    def run(self, run_type: str = "manual") -> tuple[int, int]:
        """Fetch, persist, log. Returns (fetched, new)."""
        start = time.perf_counter()
        fetched = 0
        new = 0
        status = "ok"
        error_msg: str | None = None

        try:
            items = self.fetch()
            fetched = len(items)
            new = db.upsert_items(items)
            logger.info("[%s] fetched=%d new=%d", self.name, fetched, new)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("[%s] failed: %s", self.name, error_msg)
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            db.log_run(
                run_type=run_type,
                source=self.name,
                items_fetched=fetched,
                items_new=new,
                duration_ms=duration_ms,
                status=status,
                error=error_msg,
            )

        return fetched, new
