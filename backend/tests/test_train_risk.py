"""Tests for ml/train_risk.py (the time split, tuning, the saved bundle) and the
baseline logic in ml/eval_risk.py. Synthetic data only; no database needed."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from test_build_features import synthetic

from ml import build_features as bf
from ml import eval_risk, train_risk

FIRST, LAST = date(2015, 1, 2), date(2026, 10, 4)


@pytest.fixture(scope="module")
def split():
    return train_risk.make_split(FIRST, LAST)


def every_day(start, end):
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


# --- the time split ---------------------------------------------------------------


def test_no_test_date_appears_in_train_or_validation(split):
    parts = {d: split.part(d) for d in every_day(FIRST, LAST)}
    train = {d for d, p in parts.items() if p == "train"}
    val = {d for d, p in parts.items() if p == "val"}
    test = {d for d, p in parts.items() if p == "test"}

    assert not test & train
    assert not test & val
    assert not val & train
    assert max(train) < min(val) < max(val) < min(test)  # time-ordered, never shuffled


def test_test_is_the_most_recent_12_months_and_val_the_12_before(split):
    assert split.test_end == LAST
    assert split.test_start == date(2025, 10, 5)  # 4 Oct 2026 minus 12 months, plus a day
    assert split.val_start == date(2024, 10, 5)
    assert (split.test_end - split.test_start).days + 1 == 365


def test_two_embargo_days_before_each_boundary_belong_to_no_split(split):
    for boundary in (split.val_start, split.test_start):
        assert split.part(boundary - timedelta(days=1)) is None
        assert split.part(boundary - timedelta(days=2)) is None
        assert split.part(boundary - timedelta(days=3)) is not None
    # So no training row's 48 h target (D+1, D+2) reaches into validation.
    assert split.train_end + timedelta(days=2) < split.val_start
    assert split.val_end + timedelta(days=2) < split.test_start


def test_rows_in_selects_by_split(split):
    days = every_day(date(2024, 9, 25), date(2025, 10, 10))
    table = pd.DataFrame({"x": range(len(days))}, index=days)
    test_rows = train_risk.rows_in(table, split, "test")
    assert min(test_rows.index) == split.test_start
    assert all(d < split.test_start for d in train_risk.rows_in(table, split, "train", "val").index)


def test_too_little_history_is_refused():
    with pytest.raises(ValueError, match="history"):
        train_risk.make_split(date(2025, 1, 1), date(2026, 10, 4))


def test_default_data_end_leaves_the_provisional_days_out():
    assert train_risk.default_data_end(date(2026, 10, 12)) == date(2026, 10, 5)


# --- tuning and the saved bundle --------------------------------------------------


def test_threshold_is_the_best_f1_cut_off():
    y = np.array([0, 0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.35, 0.4, 0.9])
    assert train_risk.best_f1_threshold(y, p) == pytest.approx(0.36)  # just above the last negative


def test_train_horizon_tunes_on_validation_and_refits(monkeypatch):
    # Three synthetic years, a one-setting grid and 3-month splits keep this fast.
    monkeypatch.setattr(train_risk, "PARAM_GRID", {
        "learning_rate": [0.1], "max_leaf_nodes": [7], "min_samples_leaf": [10],
        "l2_regularization": [0.0], "max_iter": [30],
    })
    start = date(2020, 1, 1)
    dataset = bf.build_dataset(synthetic(days=3 * 365, start=start), start, start + timedelta(days=3 * 365 - 1))
    known = dataset.index[dataset["y24"].notna()]
    split = train_risk.make_split(min(known), max(known), test_months=3, val_months=3)

    bundle = train_risk.train_horizon(dataset, split, 24, max(known))

    assert bundle["features"] == bf.FEATURES
    assert 0.05 <= bundle["threshold"] <= 0.95
    assert bundle["split"] == split.as_dict()
    assert bundle["rows"]["train_val"] == bundle["rows"]["train"] + bundle["rows"]["val"]
    assert bundle["model_version"].startswith("hgb-24h-")
    assert bundle["model"].early_stopping is False  # no hidden random split inside training
    p = bundle["model"].predict_proba(dataset[bf.FEATURES].tail(5))[:, 1]
    assert ((0 <= p) & (p <= 1)).all()


# --- the baselines ------------------------------------------------------------------


def test_hutton_on_forecast_uses_observed_today_and_forecast_tomorrow():
    days = [date(2026, 3, 1) + timedelta(days=n) for n in range(4)]
    test = pd.DataFrame({"hutton_day_d0": [1.0, 0.0, 1.0, 1.0]}, index=days)
    forecast = pd.DataFrame({"hutton_day": [None, True, True, False, True, None]},
                            index=[days[0] + timedelta(days=n) for n in range(6)])

    pred = eval_risk.forecast_predictions(test, forecast)

    # 24 h: the forecast's Hutton day for D+1.
    assert pred[24][days[0]] == 1.0  # D+1 forecast True
    assert pred[24][days[2]] == 0.0  # D+1 forecast False
    # 48 h on day 0: period D+1 = observed D (True) and forecast D+1 (True) -> yes.
    assert pred[48][days[0]] == 1.0
    # 48 h on day 1: observed D False; D+1 and D+2 forecast True, False -> no period.
    assert pred[48][days[1]] == 0.0
    # 48 h on day 3: D+1 forecast True, D+2 unknown, observed D True -> period on D+1: yes.
    assert pred[48][days[3]] == 1.0


def test_unknown_forecast_gives_no_prediction_not_a_no():
    day = date(2026, 3, 1)
    test = pd.DataFrame({"hutton_day_d0": [0.0]}, index=[day])
    forecast = pd.DataFrame({"hutton_day": [None]}, index=[day + timedelta(days=1)])
    pred = eval_risk.forecast_predictions(test, forecast)
    assert np.isnan(pred[24][day])
    # D is not a Hutton day, so no period on D+1; but D+1 and D+2 are unknown,
    # so a period on D+2 cannot be ruled out: unknown, not "no".
    assert np.isnan(pred[48][day])


def test_climatology_uses_the_month_of_d():
    days = [date(2026, 1, 15), date(2026, 4, 15), date(2026, 9, 15)]
    rows = pd.DataFrame(index=days)
    p = eval_risk.climatology(rows, {1: 0.1, 4: 0.7}, fallback=0.3)
    assert list(p) == [0.1, 0.7, 0.3]
