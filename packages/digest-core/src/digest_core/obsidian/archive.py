"""Topic-archive index block — the machine-readable entry index embedded in a
domain's topic-archive note. Generic over rows exposing id/ingested_at/title/
source; the surrounding archive layout (front-matter, headings, item rendering)
stays domain-side.
"""
from __future__ import annotations

from typing import Any, Iterable

import yaml

from digest_core.obsidian.render import safe

INDEX_BEGIN = "<!-- digest:index:begin -->"
INDEX_END = "<!-- digest:index:end -->"


def build_index_block(rows: Iterable[Any]) -> str:
    """YAML index of every dated entry, wrapped in stable begin/end markers."""
    entries = []
    for row in rows:
        entries.append({
            "id": row["id"],
            "date": safe(row["ingested_at"])[:10],
            "title": (safe(row["title"]) or "(untitled)")[:120],
            "source": safe(row["source"]),
        })
    payload = {"entries": entries}
    yaml_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).strip()
    return f"{INDEX_BEGIN}\n```yaml\n{yaml_text}\n```\n{INDEX_END}"
