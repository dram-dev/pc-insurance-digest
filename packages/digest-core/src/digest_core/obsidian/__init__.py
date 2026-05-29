"""digest_core.obsidian — shared Obsidian rendering primitives.

Domain projects build their own note/section layout + topic taxonomy on top
of these helpers.
"""
from digest_core.obsidian.archive import INDEX_BEGIN, INDEX_END, build_index_block
from digest_core.obsidian.paths import Paths, append_run_log
from digest_core.obsidian.render import (
    chat_link,
    confidence_badge,
    parse_see_also,
    row_get,
    safe,
    wikilink,
)

__all__ = [
    "chat_link", "confidence_badge", "parse_see_also", "row_get", "safe", "wikilink",
    "Paths", "append_run_log", "build_index_block", "INDEX_BEGIN", "INDEX_END",
]
