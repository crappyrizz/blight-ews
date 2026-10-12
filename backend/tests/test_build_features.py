"""Tests for ml/build_features.py: above all, that no feature looks into the future.

Data is synthetic: hourly readings in UTC, as stored. Local days in
Africa/Nairobi start at 21:00 UTC the evening before.
"""
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from backend import hutton
from ml import build_features as bf

START = date(2026, 3, 1)


def local_midnight_utc(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=hutton.LOCAL_TZ).astimezone(UTC)


def synthetic(days=40, start=START, seed=0) -> pd.DataFrame:
    """Hourly weather with a daily cycle and random spells of warm, humid days."""
    rng = np.random.default_rng(seed)
    t0 = local_midnight_utc(start)
    hours = days * 24
    local_hour = np.arange(hours) % 24
    spell = np.repeat(rng.random(days) < 0.4, 24)  # some days warm and wet
    temperature = 11 + 4 * np.sin((local_hour - 9) / 24 * 2 * np.pi) + np.where(spell, 1.5, -2.5)
    humidity = np.clip(80 + 15 * np.cos((local_hour - 3) / 24 * 2 * np.pi) + np.where(spell, 6, -6)
                       + rng.normal(0, 2, hours), 20, 100)
    return pd.DataFrame({
        "timestamp": [t0 + timedelta(hours=h) for h in range(hours)],
        "temperature": temperature,
        "humidity": humidity,
    })


def build(readings, days=40):
    return bf.build_dataset(readings, START, START + timedelta(days=days - 1))


def rows_up_to(table, day):
    return table[[d <= day for d in table.index]]


# --- THE leakage test ---------------------------------------------------------


@pytest.mark.parametrize("cut_offset", [14, 20, 33])
def test_no_feature_for_day_d_uses_any_hour_after_d(cut_offset):
    """Change every hour after the end of day D: no feature for D or earlier may move."""
    readings = synthetic()
    cut = START + timedelta(days=cut_offset)
    after_cut = readings["timestamp"] >= local_midnight_utc(cut + timedelta(days=1))

    shifted = readings.copy()
    shifted.loc[after_cut, "temperature"] += 7.0  # a deliberately different future
    shifted.loc[after_cut, "humidity"] = 100.0 - shifted.loc[after_cut, "humidity"]
    deleted = readings[~after_cut]  # or no future at all

    original = build(readings)
    for altered in (build(shifted), build(deleted)):
        pd.testing.assert_frame_equal(
            rows_up_to(original, cut)[bf.FEATURES], rows_up_to(altered, cut)[bf.FEATURES]
        )

    # The test can see a change when there is one: day D+1's features do move.
    next_day = cut + timedelta(days=1)
    assert not original.loc[next_day, bf.FEATURES].equals(build(shifted).loc[next_day, bf.FEATURES])
    # ...and day D's TARGET does depend on D+1, as it should.
    assert not np.array_equal(original.loc[cut, ["y24", "y48"]].values,
                              build(deleted).loc[cut, ["y24", "y48"]].values)


def test_shifting_the_whole_series_one_day_shifts_the_features_one_day():
    """Same weather, one day later: day D's features become day D+1's, exactly."""
    readings = synthetic()
    later = readings.assign(timestamp=readings["timestamp"] + pd.Timedelta(days=1))

    original = build(readings)
    moved = bf.build_dataset(later, START + timedelta(days=1), START + timedelta(days=40))

    shared = [d for d in original.index if d >= START + timedelta(days=14)][:20]
    for day in shared:
        a = original.loc[day, bf.FEATURES].drop(["doy_sin", "doy_cos"])
        b = moved.loc[day + timedelta(days=1), bf.FEATURES].drop(["doy_sin", "doy_cos"])
        pd.testing.assert_series_equal(a, b, check_names=False)


def test_night_humid_hours_only_count_the_night_inside_day_d():
    day = START
    t0 = local_midnight_utc(day)
    hours = [t0 + timedelta(hours=h) for h in range(48)]  # day D and D+1, local
    humidity = [60.0] * 48
    for h in (2, 23, 24 + 1):  # 02:00 and 23:00 on D (night), 01:00 on D+1
        humidity[h] = 95.0
    humidity[12] = 95.0  # midday: humid, but not night
    readings = pd.DataFrame({"timestamp": hours, "temperature": 12.0, "humidity": humidity})

    table = bf.build_dataset(readings, day, day + timedelta(days=1))

    assert table.loc[day, "night_humid_hours_d0"] == 2  # 02:00 and 23:00; not D+1's 01:00
    assert table.loc[day, "humid_hours_d0"] == 3


def test_lags_and_rolling_windows_look_backwards():
    table = build(synthetic())
    day = START + timedelta(days=20)
    assert table.loc[day, "tmin_d1"] == table.loc[day - timedelta(days=1), "tmin_d0"]
    assert table.loc[day, "tmin_d2"] == table.loc[day - timedelta(days=2), "tmin_d0"]
    window = [day - timedelta(days=n) for n in range(7)]
    assert table.loc[day, "tmin_mean7"] == pytest.approx(np.mean([table.loc[d, "tmin_d0"] for d in window]))
    assert table.loc[day, "hutton_days_7"] == sum(table.loc[d, "hutton_day_d0"] for d in window)


# --- targets --------------------------------------------------------------------


def test_y24_is_tomorrows_hutton_day():
    table = build(synthetic())
    for day in list(table.index)[:-1]:
        assert table.loc[day, "y24"] == table.loc[day + timedelta(days=1), "hutton_day_d0"]


def test_y48_is_a_period_on_d_plus_1_or_d_plus_2():
    table = build(synthetic())
    for day in list(table.index)[:-2]:
        p1 = table.loc[day + timedelta(days=1), "hutton_period_d0"]
        p2 = table.loc[day + timedelta(days=2), "hutton_period_d0"]
        assert table.loc[day, "y48"] == float(p1 == 1 or p2 == 1)


def test_no_target_beyond_the_end_of_the_data():
    table = build(synthetic())
    last, second_last = table.index[-1], table.index[-2]
    assert np.isnan(table.loc[last, "y24"])
    assert np.isnan(table.loc[last, "y48"])
    # y48 for the second-last day is known only if D+1 already gives a period.
    if table.loc[last, "hutton_period_d0"] != 1:
        assert np.isnan(table.loc[second_last, "y48"])


def test_unjudged_target_day_is_dropped_not_counted_as_negative():
    readings = synthetic()
    gap_day = START + timedelta(days=21)
    gap = (readings["timestamp"] >= local_midnight_utc(gap_day)) & \
          (readings["timestamp"] < local_midnight_utc(gap_day) + timedelta(hours=6))
    table = build(readings[~gap])  # gap day keeps 18 of 24 hours: unjudged

    day_before = gap_day - timedelta(days=1)
    assert np.isnan(table.loc[day_before, "y24"])
    assert day_before not in bf.labelled(table, 24).index


@pytest.mark.parametrize("p1, p2, expected", [
    (True, None, True), (None, True, True), (False, False, False),
    (False, None, None), (None, None, None),
])
def test_y48_uses_three_valued_or(p1, p2, expected):
    assert bf._or_unknown(p1, p2) == expected


# --- gaps -----------------------------------------------------------------------


def test_unjudged_day_gives_no_weather_numbers_and_raises_the_gap_flag():
    readings = synthetic()
    gap_day = START + timedelta(days=21)
    gap = (readings["timestamp"] >= local_midnight_utc(gap_day)) & \
          (readings["timestamp"] < local_midnight_utc(gap_day) + timedelta(hours=10))
    table = build(readings[~gap])

    assert np.isnan(table.loc[gap_day, "tmin_d0"])  # not a biased partial-day minimum
    assert np.isnan(table.loc[gap_day, "hutton_day_d0"])  # unknown, not 0
    assert table.loc[gap_day, "valid_hours_d0"] == 14
    assert table.loc[gap_day, "gap_in_window"] == 1
    assert table.loc[gap_day + timedelta(days=13), "gap_in_window"] == 1  # still in the 14-day window
    assert table.loc[gap_day + timedelta(days=14), "gap_in_window"] == 0  # out of it


def test_feature_table_refuses_a_table_with_missing_dates():
    daily = bf.daily_weather(hutton.to_hourly(synthetic(days=5)), START, START + timedelta(days=4))
    with pytest.raises(ValueError, match="one row per calendar day"):
        bf.feature_table(daily.drop(index=START + timedelta(days=2)))


def test_feature_list_matches_the_table():
    assert list(build(synthetic(days=5), days=5)[bf.FEATURES].columns) == bf.FEATURES
    assert len(set(bf.FEATURES)) == len(bf.FEATURES)
