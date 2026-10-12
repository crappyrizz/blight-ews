"""The learned risk model at serving time: 24 h / 48 h probabilities for a node.

predict_for_node() loads a short window of hourly readings for the node
from the database, builds day D's features with EXACTLY the functions used
in training (ml/build_features.py), and runs the two saved models
(models/risk_model_24h.joblib, risk_model_48h.joblib, from ml/train_risk.py).

Using the same code path is the point: a feature computed one way in training
and another way here would silently change what the model sees.
backend/tests/test_risk_model.py checks that the features from the short
window equal the features from the full training table for the same day.

Two honest limits, returned to the caller rather than hidden:

- If day D itself could not be judged (fewer than 20 of 24 hours), no
  probability is returned (status 'insufficient_data' / 'no_data'), the same
  rule as the Hutton baseline.
- The models were trained on ERA5-Land reanalysis. data_source='api' reads
  that same archive at the node's location (which lags ~6 days behind
  today); data_source='sensor' reads the node's own under-canopy readings,
  which are more humid than the reanalysis grid: a domain shift until enough
  sensor history exists to check it.
"""
from datetime import date, timedelta

import joblib
import pandas as pd
from sqlalchemy.orm import Session

from backend import hutton
from backend.config import REPO_ROOT

MODEL_PATHS = {
    24: REPO_ROOT / "models" / "risk_model_24h.joblib",
    48: REPO_ROOT / "models" / "risk_model_48h.joblib",
}
# Enough history for every feature of day D: 14-day rolling windows end at D
# (back to D-13), and the Hutton period two days back needs D-3. One spare day.
HISTORY_DAYS = 15

_bundles: dict[int, dict] = {}


def load_models(paths: dict | None = None) -> dict[int, dict]:
    """Load (once) the saved model bundles. Raises if they have not been trained."""
    paths = paths or MODEL_PATHS
    for horizon, path in paths.items():
        if horizon not in _bundles:
            if not path.exists():
                raise FileNotFoundError(f"{path} not found: run  python -m ml.train_risk  first")
            _bundles[horizon] = joblib.load(path)
    return _bundles


def features_for_day(session: Session, node_id: str, for_date: date, data_source: str):
    """(feature row for day D, D's daily summary row), built from the database."""
    # Imported here: ml/ is a development package, needed only once a model is used.
    from ml import build_features as bf

    start = for_date - timedelta(days=HISTORY_DAYS)
    window_start, _ = hutton.local_day_bounds(start)
    _, window_end = hutton.local_day_bounds(for_date)
    readings = hutton.load_readings(session, node_id, window_start, window_end, data_source)
    daily = bf.daily_weather(hutton.to_hourly(readings), start, for_date)
    return bf.feature_table(daily).loc[for_date], daily.loc[for_date]


def predict_for_node(
    node_id: str,
    for_date: date,
    data_source: str = "api",
    session: Session | None = None,
) -> dict:
    """24 h and 48 h probabilities of Hutton conditions after local day `for_date`.

    Returns a dict with p24 / p48 (None if day D could not be judged), the
    status of day D, and the model versions used.
    """
    if session is None:
        from backend.db import SessionLocal

        with SessionLocal() as own_session:
            return predict_for_node(node_id, for_date, data_source, own_session)

    bundles = load_models()
    features, day = features_for_day(session, node_id, for_date, data_source)
    result = {
        "node_id": node_id,
        "for_date": for_date,
        "data_source": data_source,
        "status": day["status"],
        "valid_hours": int(day["valid_hours"]),
        "gap_in_window": bool(features["gap_in_window"]),
        "model_version": {h: b["model_version"] for h, b in bundles.items()},
        "p24": None,
        "p48": None,
    }
    if day["status"] != hutton.STATUS_OK:
        return result  # an unjudged day gets no probability, never a guess

    for horizon, bundle in bundles.items():
        row = pd.DataFrame([features[bundle["features"]].astype(float)])
        result[f"p{horizon}"] = float(bundle["model"].predict_proba(row)[0, 1])
    return result
