"""Per-account drivers: which features pushed this account's risk up or down.

Logistic regression: exact additive contributions in log-odds space,
coef * standardised value, relative to the average training account.
Missing-value indicators are folded back into their base feature, so "no
product usage data" shows up as a driver of its own.
Other models: occlusion: replace one feature with its training median and
measure the change in log-odds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..features.build import FEATURES


def lr_feature_names(pipe) -> list[str]:
    cols = list(pipe.feature_names_in_)
    ind = pipe.named_steps["impute"].indicator_
    extra = [f"missing:{cols[i]}" for i in ind.features_] if ind is not None else []
    return cols + extra


def _base(name: str) -> str:
    return name.split(":", 1)[1] if name.startswith("missing:") else name


def contributions(model, X: pd.DataFrame) -> pd.DataFrame:
    """Rows = accounts, columns = base features, values = log-odds contribution."""
    est = model.estimator
    if model.algorithm == "logistic_regression":
        Z = est[:-1].transform(X[model.features])
        coef = est.named_steps["lr"].coef_[0]
        contrib = pd.DataFrame(Z * coef, index=X.index, columns=lr_feature_names(est))
        return contrib.T.groupby(_base).sum().T
    ref = getattr(model, "reference", X[model.features].median())
    base = _logit(est.predict_proba(X[model.features])[:, 1])
    out = {}
    for f in model.features:
        Xo = X[model.features].copy()
        Xo[f] = ref[f]
        out[f] = base - _logit(est.predict_proba(Xo)[:, 1])
    return pd.DataFrame(out, index=X.index)


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def describe_value(feature: str, value: float) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "no data"
    if feature in ("wau_trend", "automation_trend", "inbound_trend"):
        pct = (np.exp(value) - 1) * 100
        return f"{pct:+.0f}%"
    if feature == "seat_util_4w":
        return f"{value * 100:.0f}%"
    if feature == "seat_contraction_180d":
        return f"-{value * 100:.0f}% seats" if value > 0 else "none"
    if feature == "log_arr":
        return f"${np.expm1(value):,.0f}"
    if feature in ("seg_smb", "seg_ent"):
        return "yes" if value >= 0.5 else "no"
    if feature == "tenure_years":
        return f"{value:.1f} years"
    if feature in ("days_since_inbound", "days_since_champion"):
        return "never / >1 year" if value >= 365 else f"{value:.0f} days"
    if feature == "weeks_since_usage":
        return "no usage seen" if value >= 52 else f"{value:.0f} weeks"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def top_drivers(model, X: pd.DataFrame, k: int = 4) -> dict[str, list[dict]]:
    """account_id -> [{feature, label, value, display, impact}], risk-increasing first."""
    C = contributions(model, X)
    out = {}
    for acct in X.index:
        row = C.loc[acct].sort_values(ascending=False)
        items = []
        for f, v in row.items():
            if len(items) >= k or v <= 0.05:
                break
            items.append(_item(f, v, X.at[acct, f]))
        prot = [_item(f, v, X.at[acct, f]) for f, v in row.sort_values().items() if v < -0.05][:2]
        out[acct] = {"risk": items, "protective": prot}
    return out


def _item(f, v, value):
    missing = value is None or (isinstance(value, float) and np.isnan(value))
    label = FEATURES.get(f, f)
    if missing and f in ("wau_4w", "seat_util_4w", "wau_trend", "automation_trend"):
        label = f"no product usage data ({label})"
    return {"feature": f, "label": label, "value": None if missing else round(float(value), 4),
            "display": describe_value(f, value if not missing else np.nan), "impact": round(float(v), 3)}
