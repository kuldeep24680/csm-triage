"""Training, evaluation and registration.

Pooled model across tenants (one tenant has ~100 labelled renewals: far too
few to fit alone) + per-tenant Platt calibration when the tenant has enough
validation positives, global calibration otherwise.

Time-based split on renewal date (never random: random splits leak the future):
    train      renewals before  as_of - 12 months
    validate   as_of - 12m .. as_of - 6m     (calibration, model choice, thresholds)
    test       last 6 months                  (reported numbers; untouched until the end)
"""
from __future__ import annotations

import io
import json
import logging
from datetime import date, datetime, timedelta, timezone

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..config import settings
from ..db import system_tx, tenant_tx
from ..features.build import FEATURE_COLS, FEATURE_VERSION, build_matrix, load_frames, training_snapshots
from .calibration import PlattCalibrator
from .evaluate import operational_eval

log = logging.getLogger(__name__)
MIN_TENANT_POS = 15


def rules_baseline(X: pd.DataFrame) -> np.ndarray:
    """What a hand-built red/yellow/green score typically looks like."""
    pts = (2 * (X["wau_trend"] < -0.36).astype(int)
           + 2 * (X["urgent_tickets_90d"] >= 2).astype(int)
           + 1 * (X["payment_failures_90d"] >= 1).astype(int)
           + 1 * (X["bad_csat_90d"] >= 1).astype(int)
           + 1 * (X["seat_util_4w"] < 0.3).astype(int))   # NaN usage -> no points -> "green"
    return (pts / 7.0).to_numpy()


def make_lr() -> Pipeline:
    return Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                     ("scale", StandardScaler()),
                     ("lr", LogisticRegression(C=0.3, max_iter=2000))])


# Direction a feature *should* push risk, from domain knowledge. Features not
# listed may go either way. A fitted coefficient that contradicts its expected
# direction is almost always collinearity, and it would produce explanations
# like "usage went up, so risk went up", which no CSM should be shown.
EXPECTED_SIGN = {
    "seat_util_4w": -1, "wau_trend": -1, "automation_trend": -1, "inbound_30d": -1,
    "weeks_since_usage": +1, "tickets_30d": +1, "urgent_tickets_90d": +1, "bad_csat_90d": +1,
    "export_tickets_90d": +1, "days_since_inbound": +1, "days_since_champion": +1,
    "negative_emails_90d": +1, "payment_failures_90d": +1, "seat_contraction_180d": +1,
}


def fit_sign_constrained_lr(X: pd.DataFrame, y) -> tuple[Pipeline, list[str], list[str]]:
    """Fit LR; drop features whose coefficient contradicts EXPECTED_SIGN; refit."""
    feats, dropped = list(FEATURE_COLS), []
    for _ in range(len(FEATURE_COLS)):
        pipe = make_lr().fit(X[feats], y)
        coef = dict(zip(feats, pipe.named_steps["lr"].coef_[0][: len(feats)]))
        bad = [f for f in feats if f in EXPECTED_SIGN and coef[f] * EXPECTED_SIGN[f] < 0]
        if not bad:
            return pipe, feats, dropped
        worst = max(bad, key=lambda f: abs(coef[f]))
        feats.remove(worst)
        dropped.append(worst)
    return pipe, feats, dropped


def make_gbm() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                          l2_regularization=1.0, min_samples_leaf=40, random_state=0)


def metrics(y, p) -> dict:
    y = np.asarray(y)
    if len(set(y)) < 2:
        return {"n": int(len(y)), "positives": int(y.sum())}
    return {"n": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 4),
            "roc_auc": round(float(roc_auc_score(y, p)), 4),
            "pr_auc": round(float(average_precision_score(y, p)), 4),
            "brier": round(float(brier_score_loss(y, np.clip(p, 0, 1))), 4),
            "log_loss": round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))), 4)}


def reliability(y, p, bins=10) -> list[dict]:
    df = pd.DataFrame({"y": y, "p": p})
    df["bin"] = pd.qcut(df["p"], q=bins, duplicates="drop")
    g = df.groupby("bin", observed=True).agg(n=("y", "size"), predicted=("p", "mean"), observed=("y", "mean"))
    return [{"n": int(r.n), "predicted": round(float(r.predicted), 4), "observed": round(float(r.observed), 4)}
            for r in g.itertuples()]


def load_dataset(as_of: date) -> tuple[pd.DataFrame, dict]:
    with system_tx() as conn:
        tenants = [r["tenant_id"] for r in conn.execute("SELECT tenant_id FROM tenants ORDER BY 1")]
    parts, frames = [], {}
    observe_from = as_of.replace(year=as_of.year - settings.sim_years)
    for t in tenants:
        with tenant_tx(t) as conn:
            fr = load_frames(conn, t)
        frames[t] = fr
        snaps = training_snapshots(fr, as_of, observe_from)
        m = build_matrix(fr, snaps)
        m["tenant_id"] = t
        parts.append(m)
        log.info("tenant %s: %d training rows, %d positives", t, len(m), int(m["label"].sum()))
    return pd.concat(parts, ignore_index=True), frames


class ChurnModel:
    """What gets pickled and stored in the models table."""

    def __init__(self, version, algorithm, estimator, calibrators, watch_min_p, features=None):
        self.version = version
        self.algorithm = algorithm
        self.estimator = estimator
        self.calibrators = calibrators          # {"global": PlattCalibrator, "<tenant>": ...}
        self.watch_min_p = watch_min_p
        self.features = list(features or FEATURE_COLS)

    def raw_score(self, X: pd.DataFrame) -> np.ndarray:
        return self.estimator.predict_proba(X[self.features])[:, 1]

    def predict(self, X: pd.DataFrame, tenant_id: str) -> np.ndarray:
        cal = self.calibrators.get(tenant_id, self.calibrators["global"])
        return cal.transform(self.raw_score(X))


def fit_calibrators(raw: np.ndarray, y: np.ndarray, tenants: np.ndarray) -> dict:
    cals = {"global": PlattCalibrator().fit(raw, y)}
    for t in np.unique(tenants):
        m = tenants == t
        if y[m].sum() >= MIN_TENANT_POS:
            cals[t] = PlattCalibrator().fit(raw[m], y[m])
    return cals


def train_and_register(as_of: date, activate: bool = True) -> dict:
    data, frames = load_dataset(as_of)
    data["renewal_date"] = pd.to_datetime(data["renewal_date"])
    test_start = pd.Timestamp(as_of - timedelta(days=182))
    val_start = pd.Timestamp(as_of - timedelta(days=365))
    tr = data[data["renewal_date"] < val_start]
    va = data[(data["renewal_date"] >= val_start) & (data["renewal_date"] < test_start)]
    te = data[data["renewal_date"] >= test_start]
    split = {k: {"rows": int(len(v)), "positives": int(v["label"].sum()),
                 "renewals": int(v.drop_duplicates(["tenant_id", "account_id", "renewal_date"]).shape[0])}
             for k, v in (("train", tr), ("validate", va), ("test", te))}
    log.info("split %s", split)

    results, candidates = {}, {}
    lr_pipe, lr_feats, sign_dropped = fit_sign_constrained_lr(tr, tr["label"])
    gbm = make_gbm().fit(tr[FEATURE_COLS], tr["label"])
    for name, est, feats in (("logistic_regression", lr_pipe, lr_feats), ("gradient_boosting", gbm, FEATURE_COLS)):
        cals = fit_calibrators(est.predict_proba(va[feats])[:, 1], va["label"].to_numpy(),
                               va["tenant_id"].to_numpy())
        m = ChurnModel(None, name, est, cals, None, feats)
        pv = np.concatenate([m.predict(g, t) for t, g in va.groupby("tenant_id")])
        yv = np.concatenate([g["label"].to_numpy() for _, g in va.groupby("tenant_id")])
        candidates[name] = m
        results[name] = {"validate": metrics(yv, pv)}
    rb_val = rules_baseline(va)
    results["rules_baseline"] = {"validate": metrics(va["label"], rb_val)}

    # Model choice on validation only. Prefer the interpretable model unless the
    # other one is clearly better: CSMs act on the explanation, not just the rank.
    lr_auc = results["logistic_regression"]["validate"]["pr_auc"]
    gb_auc = results["gradient_boosting"]["validate"]["pr_auc"]
    chosen = "gradient_boosting" if gb_auc - lr_auc > 0.03 else "logistic_regression"
    model = candidates[chosen]
    model.reference = tr[FEATURE_COLS].median()

    # Elevated-risk threshold: twice the validation base rate. Below it an account
    # is not "at risk" no matter how big or close to renewal it is.
    yv = va["label"].to_numpy()
    watch_min_p = float(np.round(2 * yv.mean(), 3))

    # ---- test: touched once, at the end
    for name in ("logistic_regression", "gradient_boosting"):
        m = candidates[name]
        pt = np.concatenate([m.predict(g, t) for t, g in te.groupby("tenant_id")])
        yt = np.concatenate([g["label"].to_numpy() for _, g in te.groupby("tenant_id")])
        results[name]["test"] = metrics(yt, pt)
    results["rules_baseline"]["test"] = metrics(te["label"], rules_baseline(te))
    pt = np.concatenate([model.predict(g, t) for t, g in te.groupby("tenant_id")])
    te_sorted = pd.concat([g for _, g in te.groupby("tenant_id")])
    by_tenant = {t: metrics(g["label"], pt[(te_sorted["tenant_id"] == t).to_numpy()])
                 for t, g in te_sorted.groupby("tenant_id")}
    by_lead = {int(l): metrics(g["label"], pt[(te_sorted["lead_days"] == l).to_numpy()])
               for l, g in te_sorted.groupby("lead_days")}
    by_usage = {k: metrics(g["label"], pt[(te_sorted["usage_weeks_12w"] >= 3).to_numpy() == (k == "with_usage")])
                for k, g in (("with_usage", te_sorted[te_sorted["usage_weeks_12w"] >= 3]),
                             ("without_usage", te_sorted[te_sorted["usage_weeks_12w"] < 3]))}

    version = f"churn-{chosen.split('_')[0][:3]}-{as_of.strftime('%Y%m%d')}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    model.version = version
    model.watch_min_p = watch_min_p

    ops = operational_eval(frames, model, test_start.date(), as_of)

    report = {
        "chosen": chosen, "as_of": as_of.isoformat(), "split": split,
        "split_dates": {"validate_from": val_start.date().isoformat(), "test_from": test_start.date().isoformat()},
        "comparison": results, "test_by_tenant": by_tenant, "test_by_lead_days": by_lead,
        "test_by_usage_coverage": by_usage,
        "calibration_test": reliability(te_sorted["label"].to_numpy(), pt),
        "calibrators": {k: v.params() for k, v in model.calibrators.items()},
        "watch_min_p": watch_min_p, "operational": ops,
        "coefficients": _coefficients(model) if chosen == "logistic_regression" else None,
        "features_dropped_for_sign": sign_dropped if chosen == "logistic_regression" else [],
    }
    buf = io.BytesIO()
    joblib.dump(model, buf)
    with system_tx() as conn:
        if activate:
            conn.execute("UPDATE models SET is_active = false")
        conn.execute(
            """INSERT INTO models (model_version, algorithm, feature_version, features, train_window, metrics,
                                   artifact, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (version, chosen, FEATURE_VERSION, json.dumps(FEATURE_COLS),
             json.dumps({"label_cutoff": as_of.isoformat(), **report["split_dates"], "split": split}),
             json.dumps(report, default=str), buf.getvalue(), activate))
    report["model_version"] = version
    return report


def _coefficients(model: ChurnModel) -> list[dict]:
    from .explain import lr_feature_names
    lr = model.estimator.named_steps["lr"]
    names = lr_feature_names(model.estimator)
    return sorted([{"feature": n, "coef": round(float(c), 4)} for n, c in zip(names, lr.coef_[0])],
                  key=lambda r: -abs(r["coef"]))
