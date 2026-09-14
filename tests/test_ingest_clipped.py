"""Clipped ingestor — frontmatter parsing, idempotent stamping, auto-keep."""
from __future__ import annotations

import pytest

from digest import db
from digest.ingest import clipped
from digest.ingest.clipped import ClippedIngestor

_CLIP = """---
title: Florida rate filing approved
source: https://x.com/doi/status/42
author: "@fldoi"
created: 2026-06-24T09:00:00
tags: [telegram, clipping]
---
The Florida OIR approved a 12% homeowners rate increase.
"""


def test_split_frontmatter():
    fm, body = clipped._split_frontmatter(_CLIP)
    assert fm["title"] == "Florida rate filing approved"
    assert fm["author"] == "@fldoi"
    assert body.strip().startswith("The Florida OIR approved")


def test_split_frontmatter_none():
    fm, body = clipped._split_frontmatter("no frontmatter here")
    assert fm == {}
    assert body == "no frontmatter here"


def test_read_one_parses(tmp_path):
    (tmp_path / "clip.md").write_text(_CLIP, encoding="utf-8")
    items = ClippedIngestor(clip_dir=tmp_path).fetch()
    assert len(items) == 1
    it = items[0]
    assert it.source == "clipped"
    assert it.title == "Florida rate filing approved"
    assert it.url == "https://x.com/doi/status/42"
    assert it.author == "@fldoi"
    assert "12% homeowners" in it.content


def test_read_one_skips_already_processed(tmp_path):
    processed = _CLIP.replace("tags: [telegram, clipping]",
                              "tags: [telegram, clipping]\ndigest_processed_at: 2026-06-24T10:00:00")
    (tmp_path / "done.md").write_text(processed, encoding="utf-8")
    assert ClippedIngestor(clip_dir=tmp_path).fetch() == []


def test_run_autokeeps_and_stamps(fresh_db, tmp_path):
    clip = tmp_path / "clip.md"
    clip.write_text(_CLIP, encoding="utf-8")

    fetched, new = ClippedIngestor(clip_dir=tmp_path).run()
    assert fetched == 1 and new == 1

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT source, triage_decision, triage_score FROM items WHERE source='clipped'"
        ).fetchone()
    assert row["triage_decision"] == "keep"      # auto-kept by user intent
    assert row["triage_score"] == 1.0

    # File is stamped → a second run is a no-op (idempotent, no re-ingest).
    assert "digest_processed_at" in clip.read_text(encoding="utf-8")
    fetched2, new2 = ClippedIngestor(clip_dir=tmp_path).run()
    assert fetched2 == 0 and new2 == 0


def test_resolve_clip_dir_requires_config(monkeypatch):
    monkeypatch.setattr(clipped.settings, "obsidian_clip_dir", "")
    with pytest.raises(RuntimeError):
        clipped._resolve_clip_dir()


def test_stamp_preserves_unparseable_frontmatter(tmp_path):
    """A clip whose YAML doesn't parse must keep every key it had.

    Re-dumping a failed parse (which yields {}) used to erase title/source/
    author/tags from the user's vault file in place.
    """
    clip = tmp_path / "bad.md"
    clip.write_text(
        "---\n"
        "title: Florida DOI: homeowners rates rise 12%\n"   # unquoted colon
        "source: https://example.com/a\n"
        "author: Jane Doe\n"
        "tags: [clipping]\n"
        "---\n"
        "Body stays put.\n",
        encoding="utf-8",
    )
    from datetime import datetime, timezone

    clipped._stamp_processed(clip, datetime.now(timezone.utc))
    out = clip.read_text(encoding="utf-8")

    assert "title: Florida DOI: homeowners rates rise 12%" in out
    assert "source: https://example.com/a" in out
    assert "author: Jane Doe" in out
    assert "tags: [clipping]" in out
    assert "digest_processed_at" in out
    assert "Body stays put." in out


def test_stamp_is_idempotent(tmp_path):
    """Re-stamping replaces the timestamp instead of appending another."""
    from datetime import datetime, timezone

    clip = tmp_path / "ok.md"
    clip.write_text(_CLIP, encoding="utf-8")
    clipped._stamp_processed(clip, datetime.now(timezone.utc))
    clipped._stamp_processed(clip, datetime.now(timezone.utc))
    assert clip.read_text(encoding="utf-8").count("digest_processed_at") == 1
