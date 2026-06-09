"""Optional Apple-MLX neural head for the alpha engine.

A small MLP regressor that trains on the GPU/ANE via MLX's unified memory — an
alternative learner whose prediction can be *blended* with the gradient-boosted
tree from `alpha.py`. Deliberately optional and fully isolated:

* **Lazy + gated** — MLX is imported only inside the functions that need it, and
  `is_available()` reports cleanly when it's absent (MLX wheels exist only on
  macOS arm64). Nothing here is on the default `digest forecast train` path, so
  cloud / non-Mac / no-MLX environments are unaffected.
* **Same contract as the tree head** — `fit(X, y)` / `predict(X)` over the
  `features.FEATURE_COLUMNS` matrix, standardized internally (NaNs → column mean,
  since an MLP can't take NaN the way HistGB can).

Wire-in is opt-in: `alpha.train(..., mlx_blend=True)` would fit this alongside
the tree and persist `blend()` of the two standardized predictions. Kept out of
the default path until it's validated on real data on the Mac mini.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """True iff MLX can be imported (macOS arm64 with the `ml-mlx` extra)."""
    try:
        import mlx.core  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — ImportError on most hosts, but be defensive
        return False


def blend(pred_tree: np.ndarray, pred_mlx: np.ndarray, w_mlx: float = 0.5) -> np.ndarray:
    """Convex blend of two prediction vectors. Pure — testable without MLX.

    Blends on RANK-standardized scale (z-score each) so the two heads' differing
    output magnitudes don't let one dominate; the result is an ordering signal,
    which is what IC / long-short consume."""
    a, b = np.asarray(pred_tree, dtype=float), np.asarray(pred_mlx, dtype=float)
    w = float(min(max(w_mlx, 0.0), 1.0))
    return (1.0 - w) * _zscore(a) + w * _zscore(b)


def _zscore(x: np.ndarray) -> np.ndarray:
    sd = np.std(x)
    return (x - np.mean(x)) / sd if sd > 0 else np.zeros_like(x)


def _impute(X: np.ndarray) -> np.ndarray:
    """Column-mean impute NaNs (an MLP can't consume them; trees can)."""
    X = np.asarray(X, dtype=float).copy()
    col_mean = np.nanmean(X, axis=0)
    col_mean = np.where(np.isnan(col_mean), 0.0, col_mean)
    idx = np.where(np.isnan(X))
    X[idx] = np.take(col_mean, idx[1])
    return X


class MLXRegressor:
    """Two-layer MLP (ReLU) trained with Adam on the MLX device. Constructed only
    when `is_available()`; raises a clear error otherwise."""

    def __init__(self, hidden: int = 32, epochs: int = 300, lr: float = 1e-3,
                 l2: float = 1e-4, seed: int = 0):
        if not is_available():
            raise RuntimeError("MLX not available — install the `ml-mlx` extra on macOS arm64")
        self.hidden, self.epochs, self.lr, self.l2, self.seed = hidden, epochs, lr, l2, seed
        self._params = None
        self._mean = self._std = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "MLXRegressor":
        import mlx.core as mx
        import mlx.nn as nn
        import mlx.optimizers as optim

        Xi = _impute(X)
        self._mean, self._std = Xi.mean(axis=0), Xi.std(axis=0)
        self._std[self._std == 0] = 1.0
        Xs = (Xi - self._mean) / self._std

        d = Xs.shape[1]
        mx.random.seed(self.seed)
        model = nn.Sequential(nn.Linear(d, self.hidden), nn.ReLU(), nn.Linear(self.hidden, 1))
        mx_X, mx_y = mx.array(Xs.astype("float32")), mx.array(y.astype("float32").reshape(-1, 1))

        def loss_fn(m):
            pred = m(mx_X)
            mse = mx.mean((pred - mx_y) ** 2)
            l2 = sum(mx.sum(p ** 2) for p in m.parameters().values() if hasattr(p, "shape"))
            return mse + self.l2 * l2

        opt = optim.Adam(learning_rate=self.lr)
        loss_and_grad = nn.value_and_grad(model, loss_fn)
        for _ in range(self.epochs):
            _, grads = loss_and_grad(model)
            opt.update(model, grads)
            mx.eval(model.parameters(), opt.state)
        self._params = model
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        import mlx.core as mx

        if self._params is None:
            raise RuntimeError("MLXRegressor.predict called before fit")
        Xs = (_impute(X) - self._mean) / self._std
        out = self._params(mx.array(Xs.astype("float32")))
        return np.asarray(out).reshape(-1)
