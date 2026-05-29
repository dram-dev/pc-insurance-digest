"""Tests for the ingestor registry — the 'grow organically' seam.

Covers auto-registration via __init_subclass__, the register=False opt-out,
abstract/un-named skipping, ordering, and import-isolated discovery (a broken
submodule is reported, not fatal).
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from digest_core.ingest import IngestorBase, registered
from digest_core.ingest import registry


def test_concrete_subclass_auto_registers():
    class _RegAutoIngestor(IngestorBase):
        name = "_reg_auto"
        tags = ("test",)
        order = 7

        def fetch(self):
            return []

    spec = registry.get("_reg_auto")
    assert spec is not None
    assert spec.cls is _RegAutoIngestor
    assert spec.tags == ("test",)
    assert spec.order == 7


def test_register_false_opts_out():
    class _RegHiddenBase(IngestorBase, register=False):
        name = "_reg_hidden"

        def fetch(self):
            return []

    assert registry.get("_reg_hidden") is None


def test_unnamed_and_abstract_are_skipped():
    # No `name` → inherits "base" → skipped.
    class _RegNoName(IngestorBase):
        def fetch(self):
            return []

    assert registry.get("base") is None
    # Abstract (no fetch impl) → skipped even with a name.
    class _RegAbstract(IngestorBase):
        name = "_reg_abstract"

    assert registry.get("_reg_abstract") is None


def test_registered_is_ordered_by_order_then_name():
    class _RegOrderB(IngestorBase):
        name = "_reg_zzz"
        order = 1

        def fetch(self):
            return []

    class _RegOrderA(IngestorBase):
        name = "_reg_aaa"
        order = 50

        def fetch(self):
            return []

    names = list(registered())
    # order=1 sorts before order=50 regardless of alphabetical name.
    assert names.index("_reg_zzz") < names.index("_reg_aaa")


def test_doc_falls_back_to_module_docstring(monkeypatch):
    # A subclass with no own docstring borrows its module's first line, not the
    # framework base class docstring.
    class _RegDocless(IngestorBase):
        name = "_reg_docless"

        def fetch(self):
            return []

    spec = registry.get("_reg_docless")
    # this module's docstring starts with "Tests for the ingestor registry"
    assert spec.doc.startswith("Tests for the ingestor registry")


def test_discover_is_import_isolated(tmp_path: Path, monkeypatch):
    """A submodule that fails to import is recorded, not fatal; good ones register."""
    pkg = tmp_path / "_tmp_discover_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good_src.py").write_text(textwrap.dedent("""
        from digest_core.ingest import IngestorBase

        class GoodIngestor(IngestorBase):
            name = "_tmp_good"
            def fetch(self):
                return []
    """))
    (pkg / "bad_src.py").write_text("raise ImportError('boom: missing optional dep')")

    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        specs = registry.discover("_tmp_discover_pkg")
        assert "_tmp_good" in specs
        failures = registry.import_failures()
        bad_key = next(k for k in failures if k.endswith("bad_src"))
        assert "boom" in failures[bad_key]
    finally:
        for mod in list(sys.modules):
            if mod.startswith("_tmp_discover_pkg"):
                del sys.modules[mod]
