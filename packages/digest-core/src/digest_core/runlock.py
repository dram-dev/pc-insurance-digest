"""Whole-run serialization across the digests sharing one Mac mini.

pc-insurance-digest and macro-ai-digest share one Ollama and one mlx_lm.server.
`mlx_serialize` (summarize/backends.py) stops individual MLX requests from
colliding, but two pipelines running at once still interleave triage and
summarize calls for their whole duration — both resident models under load at
once and both runs stretched out. `pipeline_serialize` is the coarser lock: a
pipeline holds it end to end, so the digests run strictly one after the other
no matter what started them (launchd, a manual run, or a catch-up after the
machine slept through the schedule).
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_PATH = "/tmp/digest-pipeline.lock"
# A healthy run is well under an hour. A holder past this is wedged — the waiter
# gives up (and its run fails loudly) rather than running alongside it.
_DEFAULT_TIMEOUT_SEC = 4 * 3600
_POLL_SEC = 5.0


class PipelineLockTimeout(RuntimeError):
    """Another digest held the pipeline lock past the wait timeout."""


def _lock_path() -> str:
    # Resolved per call (not at import) so tests and one-off runs can point
    # PIPELINE_LOCK_PATH somewhere private.
    return os.environ.get("PIPELINE_LOCK_PATH", _DEFAULT_LOCK_PATH)


def _read_holder(fd: int) -> str:
    try:
        return os.pread(fd, 512, 0).decode("utf-8", "replace").strip()
    except OSError:
        return ""


@contextlib.contextmanager
def pipeline_serialize(
    holder: str,
    *,
    timeout_sec: float | None = None,
    on_wait: Callable[[str], None] | None = None,
) -> Iterator[float]:
    """Hold the cross-digest pipeline lock for the body; yields seconds waited.

    `holder` names this run in the lock file so a waiter can say who it is
    waiting on; `on_wait(holder_text)` fires once if the lock is busy. Raises
    PipelineLockTimeout after `timeout_sec` (default PIPELINE_LOCK_TIMEOUT_SEC,
    else 4h). Like `mlx_serialize`, it degrades to running unserialized (with a
    warning) if the lock file can't be opened — a permissions problem must not
    silently cancel every digest.
    """
    if timeout_sec is None:
        timeout_sec = float(os.environ.get("PIPELINE_LOCK_TIMEOUT_SEC", _DEFAULT_TIMEOUT_SEC))
    path = _lock_path()
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
    except OSError as exc:
        logger.warning(
            "pipeline: could not open %s (%s) — running UNSERIALIZED; "
            "the other digest may run concurrently", path, exc,
        )
        yield 0.0
        return

    started = time.monotonic()
    contended = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                contended = True
                waited = time.monotonic() - started
                if waited >= timeout_sec:
                    raise PipelineLockTimeout(
                        f"gave up after {waited / 60:.0f} min waiting for the pipeline "
                        f"lock ({path}) held by: {_read_holder(fd) or 'unknown'}"
                    ) from None
                if on_wait is not None:
                    on_wait(_read_holder(fd))
                    on_wait = None  # announce once, then wait quietly
                time.sleep(_POLL_SEC)

        # Exactly 0.0 when the lock was free: callers use `if waited:` to decide
        # whether to report a wait, and a raw monotonic delta is never zero.
        waited = time.monotonic() - started if contended else 0.0
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{holder} pid={os.getpid()} since={stamp}\n".encode(), 0)
        try:
            yield waited
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
