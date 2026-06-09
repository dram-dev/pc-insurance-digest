"""Alpha engine Phase 5 — optional MLX head: graceful absence + blend math."""
from __future__ import annotations

import numpy as np
import pytest

from digest import alpha_mlx


def test_is_available_is_a_clean_bool():
    # On this (non-arm64 / no-MLX) host it must be False, never raise.
    assert alpha_mlx.is_available() in (True, False)


def test_constructing_regressor_without_mlx_raises_clearly():
    if alpha_mlx.is_available():
        pytest.skip("MLX present — absence path not exercised here")
    with pytest.raises(RuntimeError, match="MLX not available"):
        alpha_mlx.MLXRegressor()


def test_blend_is_rank_standardized_convex_mix():
    tree = np.array([1.0, 2.0, 3.0, 4.0])
    mlx = np.array([4.0, 3.0, 2.0, 1.0])      # opposite ordering
    # w=0 → tree ordering; w=1 → mlx ordering; w=.5 → cancels to ~flat
    assert np.argmax(alpha_mlx.blend(tree, mlx, w_mlx=0.0)) == 3
    assert np.argmax(alpha_mlx.blend(tree, mlx, w_mlx=1.0)) == 0
    assert np.allclose(alpha_mlx.blend(tree, mlx, w_mlx=0.5), 0.0)


def test_impute_fills_nan_with_column_mean():
    X = np.array([[1.0, np.nan], [3.0, 4.0]])
    out = alpha_mlx._impute(X)
    assert not np.isnan(out).any()
    assert out[0, 1] == 4.0   # only non-nan in col → mean is itself
