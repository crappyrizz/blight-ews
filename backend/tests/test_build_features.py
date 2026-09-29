"""Tests for the learned model's training rows (ml/build_features.py)."""
from datetime import date, timedelta

from ml.build_features import build_dataset, window_features

START = date(2026, 3, 1)


def day(offset, status="ok", hutton_day=False, valid_hours=24,
        min_temperature=12.0, high_humidity_hours=8):
    return {
        "for_date": START + timedelta(days=offset),
        "status": status,
        "valid_hours": valid_hours,
        "hutton_day": hutton_day if status == "ok" else None,
        "min_temperature": min_temperature if status == "ok" else None,
        "hours_at_high_humidity": high_humidity_hours if status == "ok" else None,
    }


def gap(offset, valid_hours=12):
    return day(offset, status="insufficient_data", valid_hours=valid_hours)


def test_unjudged_target_day_is_dropped_not_treated_as_negative():
    # Days 0-2 are fine; day 3 (the 24h target for day 2) is a gap.
    days = [day(0), day(1), day(2), gap(3)]

    rows = build_dataset(days, horizon_hours=24)

    assert rows == []  # dropped entirely, not stored as target 0


def test_judged_target_day_produces_a_row():
    days = [day(0), day(1), day(2), day(3, hutton_day=True)]

    rows = build_dataset(days, horizon_hours=24)

    assert len(rows) == 1
    assert rows[0]["target_date"] == START + timedelta(days=3)
    assert rows[0]["target_hutton_day"] == 1


def test_gap_in_the_feature_window_is_flagged_but_keeps_the_row():
    days = [day(0), gap(1), day(2), day(3)]

    rows = build_dataset(days, horizon_hours=24)

    assert len(rows) == 1
    assert rows[0]["gap_in_window"] == 1
    assert rows[0]["judged_days_in_window"] == 2


def test_full_window_is_not_flagged():
    days = [day(0), day(1), day(2), day(3)]

    assert build_dataset(days, horizon_hours=24)[0]["gap_in_window"] == 0


def test_window_with_nothing_judged_is_dropped():
    days = [gap(0), gap(1), gap(2), day(3)]

    assert build_dataset(days, horizon_hours=24) == []


def test_features_ignore_unjudged_days_rather_than_filling_them_in():
    window = [
        day(0, min_temperature=11.0, high_humidity_hours=6),
        gap(1),
        day(2, min_temperature=13.0, high_humidity_hours=10),
    ]

    features = window_features(window)

    # Averages are over the two judged days only; the gap contributes no
    # invented value, only the flag.
    assert features["min_temperature_mean"] == 12.0
    assert features["high_humidity_hours_mean"] == 8.0
    assert features["gap_in_window"] == 1


def test_48h_horizon_looks_two_days_ahead():
    days = [day(0), day(1), day(2), day(3), day(4, hutton_day=True)]

    rows = build_dataset(days, horizon_hours=48)

    # Day 2 is the only one with a full window and a judged day two days on;
    # day 3 and day 4 would need days 5 and 6, which do not exist.
    assert [r["for_date"] for r in rows] == [START + timedelta(days=2)]
    assert rows[0]["target_date"] == START + timedelta(days=4)
    assert rows[0]["target_hutton_day"] == 1


def test_window_must_be_consecutive_days():
    days = [day(0), day(1), day(5), day(6)]  # a hole in the calendar

    assert build_dataset(days, horizon_hours=24) == []
