from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


class PlattCalibrator:
    """p_cal = sigmoid(a * logit(p_raw) + b), fit on held-out validation data."""

    def __init__(self):
        self.a, self.b, self.n = 1.0, 0.0, 0

    def fit(self, raw: np.ndarray, y: np.ndarray) -> "PlattCalibrator":
        lr = LogisticRegression(C=1e4)  # ~unregularised
        lr.fit(_logit(np.asarray(raw)).reshape(-1, 1), np.asarray(y))
        self.a, self.b, self.n = float(lr.coef_[0][0]), float(lr.intercept_[0]), int(len(y))
        return self

    def transform(self, raw: np.ndarray) -> np.ndarray:
        return 1 / (1 + np.exp(-(self.a * _logit(np.asarray(raw)) + self.b)))

    def params(self) -> dict:
        return {"a": round(self.a, 4), "b": round(self.b, 4), "n": self.n}
