"""Cross-process MLX request serialization in digest_core backends.

The shared mlx_lm.server crashes when PC + macro hit it concurrently (mismatched
batch shapes / Metal OOM); call_mlx_local takes a flock around each request so
the two digests take turns. The lock must be robust: re-acquirable, held only
for the request, and a no-op (never blocking) if it can't be taken.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

from digest_core.summarize import backends


def test_serialize_acquires_and_releases_repeatedly():
    # Re-acquiring after release in the same process must not deadlock or raise.
    for _ in range(3):
        with backends.mlx_serialize():
            pass


def test_serialize_degrades_to_noop_on_unusable_lock_path(monkeypatch, tmp_path):
    # If the lock file can't be opened (dir missing), it still yields — a summary
    # can never be blocked by the lock itself.
    monkeypatch.setattr(backends, "_MLX_LOCK_PATH", str(tmp_path / "missing-dir" / "x.lock"))
    entered = False
    with backends.mlx_serialize():
        entered = True
    assert entered


def test_call_mlx_local_holds_the_lock_around_the_post(monkeypatch):
    state: dict = {}

    @contextlib.contextmanager
    def _spy():
        state["entered"] = True
        yield
        state["exited"] = True

    monkeypatch.setattr(backends, "mlx_serialize", _spy)

    def _fake_post(*args, **kwargs):
        state["locked_during_post"] = state.get("entered") and not state.get("exited")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": "ok"}}]},
        )

    monkeypatch.setattr(backends.requests, "post", _fake_post)
    cfg = backends.BackendConfig(mlx_server_url="http://localhost:8080", mlx_model="m")
    out = backends.call_mlx_local("sys", "user", cfg)

    assert out == "ok"
    assert state["locked_during_post"] is True   # POST ran while the lock was held
    assert state.get("exited") is True           # and the lock was released after
