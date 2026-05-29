"""Tests for the lifted Obsidian Paths layout, run-log, and index block."""
from __future__ import annotations

import pytest

from digest_core.obsidian import archive
from digest_core.obsidian.paths import Paths, append_run_log


def test_for_vault_builds_layout_and_ensure_creates_dirs(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    p = Paths.for_vault(str(vault), "81 P&C Digest")
    assert p.digest_root == vault / "81 P&C Digest"
    assert p.daily_dir.name == "Daily" and p.meta_dir.name == "_meta"
    p.ensure()
    for d in (p.daily_dir, p.topics_dir, p.weekly_dir, p.meta_dir):
        assert d.is_dir()


def test_for_vault_missing_vault_raises(tmp_path):
    with pytest.raises(RuntimeError, match="vault not found"):
        Paths.for_vault(str(tmp_path / "nope"), "X")


def test_for_vault_rejects_escaping_digest_dir(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(RuntimeError, match="within the vault"):
        Paths.for_vault(str(vault), "../escape")


def test_append_run_log_creates_then_appends(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    p = Paths.for_vault(str(vault), "D")
    p.ensure()
    append_run_log(p, "first message")
    append_run_log(p, "second message")
    text = (p.meta_dir / "Run Log.md").read_text()
    assert "Digest Run Log" in text
    assert "first message" in text and "second message" in text


def test_build_index_block_marked_yaml():
    rows = [{"id": 5, "ingested_at": "2026-05-20T12:00:00", "title": "T", "source": "edgar"}]
    block = archive.build_index_block(rows)
    assert block.startswith(archive.INDEX_BEGIN)
    assert block.rstrip().endswith(archive.INDEX_END)
    assert "id: 5" in block and "source: edgar" in block and "2026-05-20" in block


def test_pc_paths_subclass_resolve(tmp_path, monkeypatch):
    from digest import obsidian
    vault = tmp_path / "vault"
    (vault / "81 P&C Digest").mkdir(parents=True)
    monkeypatch.setattr(obsidian.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(obsidian.settings, "obsidian_digest_dir", "81 P&C Digest")
    p = obsidian.Paths.resolve()
    assert isinstance(p, obsidian.Paths)   # resolve() returns a PC Paths, not bare core Paths
    assert p.topics_dir == vault / "81 P&C Digest" / "Topics"
