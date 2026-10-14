"""Smoke tests for backend/risk_model.py on seeded data in the test database.

A tiny model is trained here on synthetic weather (the real joblib files are
gitignored and not needed), saved to a temporary folder, and used through
predict_for_node() exactly as the app will.
"""
from datetime import timedelta

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier
from test_build_features import START, local_midnight_utc, synthetic

from backend import risk_model
from backend.models import Farmer, SensorNode, SensorReading, WeatherReading
from ml import build_features as bf

DAYS = 40
DAY = START + timedelta(days=30)  # well past the 15 days of history a prediction needs


@pytest.fixture
def node(db_session):
    farmer = Farmer(name="T", phone_number="+254799000777", password_hash="x", role="farmer")
    db_session.add(farmer)
    db_session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location="p", latitude=-0.7, longitude=36.6)
    db_session.add(node)
    db_session.flush()
    return node


@pytest.fixture
def weather():
    return synthetic(days=DAYS)


@pytest.fixture
def seeded_archive(db_session, node, weather):
    for row in weather.itertuples():
        db_session.add(WeatherReading(latitude=node.latitude, longitude=node.longitude, source="archive",
                                      timestamp=row.timestamp, temperature=row.temperature,
                                      humidity=row.humidity))
    db_session.flush()


def save_tiny_models(directory) -> dict:
    """Two small models with the real feature list, saved like ml/train_risk.py saves them."""
    dataset = bf.build_dataset(synthetic(days=400, start=START - timedelta(days=400), seed=3),
                               START - timedelta(days=400), START - timedelta(days=1))
    paths = {}
    for horizon in bf.HORIZONS:
        rows = bf.labelled(dataset, horizon)
        model = HistGradientBoostingClassifier(max_iter=20, random_state=0, early_stopping=False)
        model.fit(rows[bf.FEATURES], rows[bf.TARGETS[horizon]])
        paths[horizon] = directory / f"risk_model_{horizon}h.joblib"
        joblib.dump({"model": model, "features": list(bf.FEATURES),
                     "model_version": f"test-{horizon}h"}, paths[horizon])
    return paths


@pytest.fixture
def tiny_models(tmp_path, monkeypatch):
    paths = save_tiny_models(tmp_path)
    monkeypatch.setattr(risk_model, "MODEL_PATHS", paths)
    monkeypatch.setattr(risk_model, "_bundles", {})
    return paths


def test_predict_for_node_returns_both_probabilities(db_session, node, seeded_archive, tiny_models):
    result = risk_model.predict_for_node(node.node_id, DAY, "api", session=db_session)

    assert result["status"] == "ok"
    assert result["valid_hours"] == 24
    assert 0.0 <= result["p24"] <= 1.0
    assert 0.0 <= result["p48"] <= 1.0
    assert result["model_version"] == {24: "test-24h", 48: "test-48h"}
    assert result["gap_in_window"] is False


def test_serving_features_equal_training_features(db_session, node, seeded_archive, weather):
    """The short DB window must give exactly the features the training table has for that day."""
    served, _ = risk_model.features_for_day(db_session, node.node_id, DAY, "api")
    trained = bf.build_dataset(weather, START, START + timedelta(days=DAYS - 1)).loc[DAY, bf.FEATURES]

    pd.testing.assert_series_equal(served[bf.FEATURES].astype(float), trained.astype(float),
                                   check_names=False)


def test_sensor_readings_can_be_the_source(db_session, node, weather, tiny_models):
    for row in weather.itertuples():
        db_session.add(SensorReading(node_id=node.node_id, timestamp=row.timestamp,
                                     temperature=row.temperature, humidity=row.humidity))
    db_session.flush()

    result = risk_model.predict_for_node(node.node_id, DAY, "sensor", session=db_session)
    assert result["status"] == "ok"
    assert result["p24"] is not None


def test_a_day_without_enough_data_gets_no_probability(db_session, node, weather, tiny_models):
    # Seed everything except most of DAY itself.
    gap_start = local_midnight_utc(DAY)
    keep = ~((weather["timestamp"] >= gap_start) & (weather["timestamp"] < gap_start + timedelta(hours=10)))
    for row in weather[keep].itertuples():
        db_session.add(WeatherReading(latitude=node.latitude, longitude=node.longitude, source="archive",
                                      timestamp=row.timestamp, temperature=row.temperature,
                                      humidity=row.humidity))
    db_session.flush()

    result = risk_model.predict_for_node(node.node_id, DAY, "api", session=db_session)

    assert result["status"] == "insufficient_data"
    assert result["valid_hours"] == 14
    assert result["p24"] is None and result["p48"] is None  # never a guess


def test_missing_model_files_say_how_to_make_them(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_model, "MODEL_PATHS", {24: tmp_path / "nope.joblib"})
    monkeypatch.setattr(risk_model, "_bundles", {})
    with pytest.raises(FileNotFoundError, match="ml.train_risk"):
        risk_model.load_models()


def test_seeded_window_is_long_enough_for_every_feature():
    assert risk_model.HISTORY_DAYS >= max(bf.ROLLING_WINDOWS) + 1
    assert np.isfinite(risk_model.HISTORY_DAYS)
