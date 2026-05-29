"""Core sink schema_prefix — shared-catalog, domain-prefixed schemas.

One catalog, schemas prefixed per domain (pc_*, macro_*) so both digests live in
one lakehouse. Verifies _qualify + that emitted MERGE targets the prefixed schema
while PK lookups stay unprefixed.
"""
from __future__ import annotations

from digest_core.sinks.databricks import DatabricksSink


def _sink(prefix: str) -> DatabricksSink:
    return DatabricksSink(enabled=True, host="h", http_path="p", token="t",
                          catalog="digest", schema_prefix=prefix)


def test_qualify_default_is_unprefixed():
    s = DatabricksSink(enabled=False, host="", http_path="", token="", catalog="c")
    assert s._qualify("bronze.ingested_items") == "bronze.ingested_items"


def test_qualify_pc_and_macro():
    assert _sink("pc_")._qualify("bronze.ingested_items") == "pc_bronze.ingested_items"
    assert _sink("macro_")._qualify("silver.summaries") == "macro_silver.summaries"


def test_merge_targets_prefixed_schema(monkeypatch):
    s = _sink("pc_")
    captured = {}

    def fake_merge(cur, table, cols, pk_cols, rows):
        captured["table"] = table
        captured["pk_cols"] = pk_cols

    # _insert opens a connection; stub it out and capture the merge target.
    monkeypatch.setattr(s, "_connection", lambda: _FakeConn())
    monkeypatch.setattr(DatabricksSink, "_merge_batch", staticmethod(fake_merge))

    s.write_triage("rss", "r1", {"decision": "keep", "topic": "cyber"})
    assert captured["table"] == "pc_silver.triage_verdicts"   # prefixed
    assert captured["pk_cols"] == ("item_hash", "triaged_at")  # PK lookup unprefixed


class _FakeConn:
    def cursor(self):
        return _FakeCur()


class _FakeCur:
    def execute(self, *a, **k):
        pass

    def close(self):
        pass
