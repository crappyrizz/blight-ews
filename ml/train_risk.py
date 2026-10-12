"""Train the learned risk model: will Hutton conditions occur in the next 24 h / 48 h?

One HistGradientBoostingClassifier per horizon, trained on the ERA5-Land
reanalysis (Open-Meteo archive, pinned) at the farm (2015 onwards), with features and
targets from ml/build_features.py.

How the evaluation is kept honest:

1. TIME-ORDERED SPLIT, NEVER SHUFFLED. Weather is autocorrelated: a random
   split puts near-identical neighbouring days on both sides and flatters
   the model. So:
       test       = the most recent 12 months
       validation = the 12 months before that
       train      = everything earlier

2. AN EMBARGO AT EACH BOUNDARY. Day D's target looks at D+1 and D+2, so the
   last two days before a boundary have targets that fall in the next
   split. Those rows are dropped from the earlier split; otherwise
   validation or test weather would leak into the training labels.

3. TUNING ON VALIDATION ONLY. A small grid is scored on validation (PR-AUC,
   which suits a rare positive class), the decision threshold is chosen
   there too (best F1), and only then is the model refit on train +
   validation and evaluated ONCE on test (ml/eval_risk.py). The test year
   plays no part in any choice.

4. NO HIDDEN SHUFFLING. HistGradientBoosting's built-in early stopping
   carves a random validation set out of the training data, so it is
   switched off; the number of boosting rounds is a grid parameter instead.

5. NO PROVISIONAL DATA. The archive returns values right up to today, but
   the last few days are not settled reanalysis yet. Data stops
   ARCHIVE_SETTLE_DAYS before today (or at --data-end).

Run from the repo root:

    .\\.venv\\Scripts\\python.exe -m ml.train_risk
    .\\.venv\\Scripts\\python.exe -m ml.train_risk --data-end 2026-10-05

Writes models/risk_model_24h.joblib and models/risk_model_48h.joblib.
"""
import argparse
import itertools
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss, f1_score
from sqlalchemy import select

from backend import hutton
from backend.config import REPO_ROOT, settings
from backend.models import WeatherReading
from ml import build_features as bf

log = logging.getLogger("train_risk")

MODEL_DIR = REPO_ROOT / "models"
FIRST_DAY = date(2015, 1, 2)  # first whole local day of the archive (2015-01-01 starts at 03:00 local)
ARCHIVE_SETTLE_DAYS = 7  # the archive's latest days are provisional
TEST_MONTHS = 12
VALIDATION_MONTHS = 12
EMBARGO_DAYS = 2  # y48 looks two days ahead
RANDOM_STATE = 42

# A deliberately small grid: 2 x 3 x 2 x 2 x 2 = 48 settings per horizon.
PARAM_GRID = {
    "learning_rate": [0.05, 0.1],
    "max_leaf_nodes": [7, 15, 31],
    "min_samples_leaf": [20, 60],
    "l2_regularization": [0.0, 1.0],
    "max_iter": [100, 300],
}


def model_path(horizon: int):
    return MODEL_DIR / f"risk_model_{horizon}h.joblib"


# --- data -----------------------------------------------------------------------


def default_data_end(today: date | None = None) -> date:
    today = today or datetime.now(hutton.LOCAL_TZ).date()
    return today - timedelta(days=ARCHIVE_SETTLE_DAYS)


def load_weather(session, source: str, latitude: float = None, longitude: float = None) -> pd.DataFrame:
    """Hourly weather_readings for one source at the farm, as a DataFrame."""
    latitude = settings.SITE_LAT if latitude is None else latitude
    longitude = settings.SITE_LON if longitude is None else longitude
    rows = session.execute(
        select(WeatherReading.timestamp, WeatherReading.temperature, WeatherReading.humidity)
        .where(WeatherReading.source == source,
               WeatherReading.latitude == latitude,
               WeatherReading.longitude == longitude)
        .order_by(WeatherReading.timestamp)
    ).all()
    return pd.DataFrame(rows, columns=["timestamp", "temperature", "humidity"])


def load_dataset(data_end: date, session=None) -> pd.DataFrame:
    """Features and targets for every local day from FIRST_DAY to data_end (archive)."""
    if session is None:
        from backend.db import SessionLocal

        with SessionLocal() as own:
            return load_dataset(data_end, own)
    return bf.build_dataset(load_weather(session, "archive"), FIRST_DAY, data_end)


# --- the time split --------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """Boundaries of the time-ordered split. Each split is [start, end] inclusive."""

    train_start: date
    train_end: date
    val_start: date
    val_end: date
    test_start: date
    test_end: date

    def part(self, d: date) -> str | None:
        """Which split day d belongs to, or None (embargoed or out of range)."""
        if self.train_start <= d <= self.train_end:
            return "train"
        if self.val_start <= d <= self.val_end:
            return "val"
        if self.test_start <= d <= self.test_end:
            return "test"
        return None

    def as_dict(self) -> dict:
        return {name: [str(getattr(self, f"{name}_start")), str(getattr(self, f"{name}_end"))]
                for name in ("train", "val", "test")}


def make_split(first_day: date, last_day: date,
               test_months: int = TEST_MONTHS, val_months: int = VALIDATION_MONTHS,
               embargo_days: int = EMBARGO_DAYS) -> Split:
    """Test = last `test_months`, val = the `val_months` before, train = the rest.

    The `embargo_days` before each boundary belong to no split.
    """
    test_start = (pd.Timestamp(last_day) - pd.DateOffset(months=test_months) + pd.Timedelta(days=1)).date()
    val_start = (pd.Timestamp(test_start) - pd.DateOffset(months=val_months)).date()
    embargo = timedelta(days=embargo_days)
    if val_start - embargo <= first_day:
        raise ValueError("not enough history for a train split")
    return Split(
        train_start=first_day, train_end=val_start - embargo - timedelta(days=1),
        val_start=val_start, val_end=test_start - embargo - timedelta(days=1),
        test_start=test_start, test_end=last_day,
    )


def split_for(dataset: pd.DataFrame) -> Split:
    """The split used by both training and evaluation: last day with a known y24."""
    known = dataset.index[dataset["y24"].notna()]
    return make_split(min(known), max(known))


def rows_in(dataset: pd.DataFrame, split: Split, *parts: str) -> pd.DataFrame:
    return dataset[[split.part(d) in parts for d in dataset.index]]


# --- fitting ---------------------------------------------------------------------


def make_model(params: dict) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        **params, early_stopping=False, random_state=RANDOM_STATE
    )


def best_f1_threshold(y_true, probabilities) -> float:
    """The probability cut-off that maximises F1 (chosen on validation only)."""
    candidates = np.round(np.arange(0.05, 0.951, 0.01), 2)
    scores = [f1_score(y_true, probabilities >= t, zero_division=0) for t in candidates]
    return float(candidates[int(np.argmax(scores))])


def tune(train: pd.DataFrame, val: pd.DataFrame, target: str) -> tuple[dict, list[dict]]:
    """Score every grid setting on validation; best by PR-AUC, then Brier."""
    results = []
    keys = list(PARAM_GRID)
    for values in itertools.product(*(PARAM_GRID[k] for k in keys)):
        params = dict(zip(keys, values, strict=True))
        model = make_model(params).fit(train[bf.FEATURES], train[target])
        p = model.predict_proba(val[bf.FEATURES])[:, 1]
        results.append({
            "params": params,
            "val_pr_auc": float(average_precision_score(val[target], p)),
            "val_brier": float(brier_score_loss(val[target], p)),
        })
    results.sort(key=lambda r: (-r["val_pr_auc"], r["val_brier"]))
    return results[0]["params"], results


def train_horizon(dataset: pd.DataFrame, split: Split, horizon: int, data_end: date) -> dict:
    """Tune on validation, choose the threshold there, refit on train+val. Returns the bundle."""
    target = bf.TARGETS[horizon]
    data = bf.labelled(dataset, horizon)
    train = rows_in(data, split, "train")
    val = rows_in(data, split, "val")

    params, grid = tune(train, val, target)
    tuned = make_model(params).fit(train[bf.FEATURES], train[target])
    val_p = tuned.predict_proba(val[bf.FEATURES])[:, 1]
    threshold = best_f1_threshold(val[target], val_p)

    # Refit on everything before the test year (the val->test embargo still applies).
    train_val = rows_in(data, split, "train", "val")
    model = make_model(params).fit(train_val[bf.FEATURES], train_val[target])

    trained_at = datetime.now(UTC)
    return {
        "model": model,
        "model_version": f"hgb-{horizon}h-{trained_at:%Y%m%d}-data{FIRST_DAY:%Y%m%d}-{data_end:%Y%m%d}",
        "horizon": horizon,
        "target": target,
        "features": list(bf.FEATURES),
        "threshold": threshold,
        "params": params,
        "split": split.as_dict(),
        "data": {"source": "archive", "latitude": settings.SITE_LAT,
                 "longitude": settings.SITE_LON, "first_day": str(FIRST_DAY), "data_end": str(data_end)},
        "rows": {"train": len(train), "val": len(val), "train_val": len(train_val)},
        "validation": {
            "pr_auc": float(average_precision_score(val[target], val_p)),
            "brier": float(brier_score_loss(val[target], val_p)),
            "f1_at_threshold": float(f1_score(val[target], val_p >= threshold, zero_division=0)),
            "base_rate": float(val[target].mean()),
        },
        "grid_top5": grid[:5],
        "sklearn_version": sklearn.__version__,
        "trained_at": trained_at.isoformat(timespec="seconds"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Train the 24h/48h Hutton risk models.")
    parser.add_argument("--data-end", type=date.fromisoformat, default=None,
                        help=f"last local day of data (default: today - {ARCHIVE_SETTLE_DAYS} days)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    data_end = args.data_end or default_data_end()
    dataset = load_dataset(data_end)
    split = split_for(dataset)
    log.info("Data %s..%s: %d days. Split %s", FIRST_DAY, data_end, len(dataset), split.as_dict())

    MODEL_DIR.mkdir(exist_ok=True)
    for horizon in bf.HORIZONS:
        bundle = train_horizon(dataset, split, horizon, data_end)
        joblib.dump(bundle, model_path(horizon))
        v = bundle["validation"]
        log.info("%dh: %s | rows %s | val PR-AUC %.3f, Brier %.3f, F1 %.3f at threshold %.2f "
                 "(base rate %.3f) | params %s",
                 horizon, bundle["model_version"], bundle["rows"], v["pr_auc"], v["brier"],
                 v["f1_at_threshold"], bundle["threshold"], v["base_rate"], bundle["params"])
        log.info("Saved %s", model_path(horizon).relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
