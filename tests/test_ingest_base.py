"""Tests for the lifted IngestorBase (digest_core) + PC store binding.

The framework run() skeleton moved to digest_core.ingest.base and takes
persistence through an injected ItemStore; src/digest/ingest/base.py binds it
to digest.db. These tests exercise the real wired path (domain base over the
temp DB) plus the unbound-store guard on the bare core base.
"""
from __future__ import annotations

import pytest

from digest import db
from digest.ingest.base import IngestedItem, IngestorBase


class _Ok(IngestorBase):
    name = "test_ok"

    def fetch(self):
        return [
            IngestedItem(source="test_ok", source_id="a", title="Item A"),
            IngestedItem(source="test_ok", source_id="b", title="Item B"),
        ]


class _Boom(IngestorBase):
    name = "test_boom"

    def fetch(self):
        raise RuntimeError("fetch exploded")


def test_run_persists_and_logs_ok(fresh_db):
    fetched, new = _Ok().run(run_type="manual")
    assert (fetched, new) == (2, 2)
    assert db.item_stats() == {"test_ok": 2}
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT source, items_fetched, items_new, status FROM run_log"
        ).fetchone()
    assert (row["source"], row["items_fetched"], row["items_new"], row["status"]) == (
        "test_ok", 2, 2, "ok",
    )


def test_run_swallows_fetch_error_and_logs_it(fresh_db):
    fetched, new = _Boom().run()
    assert (fetched, new) == (0, 0)
    assert db.item_stats() == {}  # nothing persisted on failure
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, error FROM run_log").fetchone()
    assert row["status"] == "error"
    assert "RuntimeError" in row["error"]


def test_pc_base_binds_db_as_store():
    assert IngestorBase.store is db


def test_core_base_unbound_store_raises():
    from digest_core.ingest.base import IngestorBase as CoreBase

    class _Unbound(CoreBase):
        name = "unbound"

        def fetch(self):
            return []

    with pytest.raises(RuntimeError, match="store is not configured"):
        _Unbound().run()
