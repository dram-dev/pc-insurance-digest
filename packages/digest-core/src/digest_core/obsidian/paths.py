"""Vault path layout + run-log helper for digest Obsidian writers.

`Paths` is the Daily/Topics/Weekly/_meta folder layout under a vault's digest
root. `for_vault` builds + validates it; a domain typically wraps that in a
`resolve()` that reads its own settings (env var name, digest dir).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RUN_LOG_HEADER = "# Digest Run Log\n\n_Append-only operations log._\n\n"


@dataclass
class Paths:
    vault: Path
    digest_root: Path
    daily_dir: Path
    topics_dir: Path
    weekly_dir: Path
    meta_dir: Path

    @classmethod
    def for_vault(cls, vault_path: str, digest_dir: str) -> "Paths":
        """Build + validate the Daily/Topics/Weekly/_meta layout under
        vault/digest_dir. Raises if the vault is missing or digest_dir
        escapes it. Returns an instance of the calling class (subclass-safe).
        """
        vault = Path(vault_path).expanduser()
        if not vault.exists():
            raise RuntimeError(f"Obsidian vault not found at: {vault}")
        digest_root = vault / digest_dir
        if not digest_root.resolve().is_relative_to(vault.resolve()):
            raise RuntimeError(f"digest dir {digest_dir!r} must be within the vault.")
        return cls(
            vault=vault,
            digest_root=digest_root,
            daily_dir=digest_root / "Daily",
            topics_dir=digest_root / "Topics",
            weekly_dir=digest_root / "Weekly",
            meta_dir=digest_root / "_meta",
        )

    def ensure(self) -> None:
        for p in (self.digest_root, self.daily_dir, self.topics_dir,
                  self.weekly_dir, self.meta_dir):
            p.mkdir(parents=True, exist_ok=True)


def append_run_log(paths: Paths, message: str) -> None:
    """Append a timestamped line to the vault's _meta/Run Log.md (created on first use)."""
    target = paths.meta_dir / "Run Log.md"
    if not target.exists():
        target.write_text(RUN_LOG_HEADER, encoding="utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with target.open("a", encoding="utf-8") as fp:
        fp.write(f"- `{ts}` — {message}\n")
