"""Cross-digest pipeline lock (digest_core.runlock).

PC and macro share one Ollama + one MLX server; each `digest pipeline` holds
this flock for its whole run so the two digests run strictly one after the
other. The lock must serialize, announce who it's waiting on, give up loudly
on a wedged holder, release on error, and never block a run over a bad path.
"""
from __future__ import annotations

import threading
import time

import pytest

from digest_core import runlock


@pytest.fixture(autouse=True)
def private_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCK_PATH", str(tmp_path / "pipeline.lock"))
    monkeypatch.setattr(runlock, "_POLL_SEC", 0.02)


def test_uncontended_acquire_waits_zero_and_is_reacquirable():
    for _ in range(3):
        with runlock.pipeline_serialize("pc") as waited:
            assert waited == 0.0  # exactly zero, so `if waited:` never reports a wait


def test_busy_lock_times_out_naming_the_holder():
    # flock binds to the open file description, so a second open in the same
    # process contends exactly like the other digest's process would.
    with runlock.pipeline_serialize("macro-ai-digest"):
        seen: list[str] = []
        with pytest.raises(runlock.PipelineLockTimeout, match="macro-ai-digest"):
            with runlock.pipeline_serialize("pc", timeout_sec=0.1, on_wait=seen.append):
                pytest.fail("acquired a lock another run holds")
    assert len(seen) == 1 and seen[0].startswith("macro-ai-digest pid=")


def test_waiter_runs_after_the_holder_releases():
    order: list[str] = []
    holding = threading.Event()

    def _holder():
        with runlock.pipeline_serialize("macro-ai-digest"):
            holding.set()
            time.sleep(0.2)
            order.append("macro done")

    t = threading.Thread(target=_holder)
    t.start()
    holding.wait(2)
    with runlock.pipeline_serialize("pc", timeout_sec=5) as waited:
        order.append("pc start")
    t.join()
    assert order == ["macro done", "pc start"]
    assert waited > 0.1


def test_lock_is_released_when_the_body_raises():
    with pytest.raises(RuntimeError):
        with runlock.pipeline_serialize("pc"):
            raise RuntimeError("stage blew up")
    with runlock.pipeline_serialize("pc", timeout_sec=0.1):
        pass  # would raise PipelineLockTimeout if the failed run leaked the lock


def test_unusable_lock_path_degrades_to_running(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_LOCK_PATH", str(tmp_path / "missing-dir" / "x.lock"))
    entered = False
    with runlock.pipeline_serialize("pc"):
        entered = True
    assert entered
