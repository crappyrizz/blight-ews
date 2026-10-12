"""Features and targets for the learned risk model (README component 3).

The model forecasts infection-favourable WEATHER at the plot: whether Hutton
conditions will occur in the next 24 h / 48 h. It does not forecast blight.

Targets, for each local (Africa/Nairobi) day D, computed with backend/hutton.py:
    y24 = hutton_day(D+1)
    y48 = a Hutton period flagged on D+1 or on D+2
Rows whose target is unknown (None) are dropped. A day we could not judge is
never a negative: that would teach the model that a silent sensor means safe
weather, which is exactly backwards.

THE LEAKAGE RULE. A feature for day D may use only data up to 23:59 local
time on day D, which is when the forecast would be made. In practice:

- every per-day value comes from that day's own hours (hutton.daily_summary
  and the extra columns below group hours by their LOCAL date);
- lags only look backwards (shift(+1), shift(+2));
- rolling windows end at D (pandas' rolling() is trailing by default);
- "night-time humid hours" counts the night hours INSIDE day D (00:00-05:59
  and 18:00-23:59 local), not the night that runs on into D+1.

backend/tests/test_build_features.py proves this by changing every hour after
day D and checking that no feature for day D or earlier moves.

Gaps follow the project's missing-data policy: a day with fewer than 20 of
24 hourly values contributes no weather numbers (NaN, which the gradient-
boosting model handles natively), nothing is interpolated, and the number of
judged days in the last 14 is itself a feature, so the model can learn how
far to trust a window built from partial data.
"""
import math
from datetime import date

import numpy as np
import pandas as pd

from backend import hutton

HORIZONS = (24, 48)
TARGETS = {24: "y24", 48: "y48"}

LAGS = (0, 1, 2)  # D, D-1, D-2
ROLLING_WINDOWS = (7, 14)  # days, ending at D
NIGHT_START_HOUR, NIGHT_END_HOUR = 18, 6  # local; the night hours inside day D

# Per-day values (each from that day's own hours only).
DAILY_COLUMNS = [
    "tmin", "tmax", "mean_rh", "humid_hours", "longest_humid_run",
    "night_humid_hours", "diurnal_range", "hutton_day", "hutton_period",
]
WEATHER_COLUMNS = DAILY_COLUMNS[:7]  # the measured ones (not the Hutton flags)
ROLLING_COLUMNS = ["tmin", "tmax", "mean_rh", "humid_hours", "night_humid_hours"]


def daily_weather(hourly: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """One row per local day in [start, end], every day present (gaps included).

    Built on backend/hutton.py, so the Hutton day and period here are exactly
    the ones the baseline and the targets use.
    """
    daily = hutton.hutton_periods(hutton.hutton_days(hutton.daily_summary(hourly, start, end)))
    daily = daily[[start <= d <= end for d in daily.index]].copy()

    # Extra per-day values, from the same hourly means, grouped by LOCAL date.
    valid = hourly.dropna(subset=["temperature", "humidity"])
    local_date = pd.Index(valid.index.date)
    hour = valid.index.hour
    is_night = (hour >= NIGHT_START_HOUR) | (hour < NIGHT_END_HOUR)
    humid = valid["humidity"] >= hutton.RH_THRESHOLD_PCT
    daily["mean_rh"] = valid["humidity"].groupby(local_date).mean().reindex(daily.index)
    daily["night_humid_hours"] = (
        (humid & is_night).groupby(local_date).sum().reindex(daily.index).astype(float)
    )
    daily["diurnal_range"] = daily["tmax"] - daily["tmin"]

    # An unjudged day (fewer than 20 hours) gives no weather numbers at all:
    # its partial-day minimum or humid-hour count would be biased.
    unjudged = daily["status"] != hutton.STATUS_OK
    daily.loc[unjudged, WEATHER_COLUMNS] = np.nan
    return daily


def _as_number(series: pd.Series) -> pd.Series:
    """True/False/None -> 1.0/0.0/NaN."""
    return series.map(lambda v: np.nan if v is None or pd.isna(v) else float(bool(v))).astype(float)


def feature_table(daily: pd.DataFrame) -> pd.DataFrame:
    """Features for every day in `daily`, which must have one row per calendar day.

    Because the index is continuous, shift(n) is exactly "n days earlier"
    and rolling(w) is exactly "the w days ending at D".
    """
    dates = list(daily.index)
    if dates:
        expected = list(pd.date_range(dates[0], dates[-1], freq="D").date)
        if dates != expected:
            raise ValueError("daily table must have exactly one row per calendar day")

    values = daily[WEATHER_COLUMNS].astype(float)
    values["hutton_day"] = _as_number(daily["hutton_day"])
    values["hutton_period"] = _as_number(daily["hutton_period"])

    features = {}
    for lag in LAGS:  # D, D-1, D-2: backwards only
        for column in DAILY_COLUMNS:
            features[f"{column}_d{lag}"] = values[column].shift(lag)

    for window in ROLLING_WINDOWS:  # trailing windows ending at D
        rolling = values.rolling(window, min_periods=window // 2)
        for column in ROLLING_COLUMNS:
            features[f"{column}_mean{window}"] = rolling[column].mean()
        features[f"hutton_days_{window}"] = rolling["hutton_day"].sum()

    judged = (daily["status"] == hutton.STATUS_OK).astype(float)
    features["judged_days_14"] = judged.rolling(14, min_periods=1).sum()
    features["gap_in_window"] = (features["judged_days_14"] < 14).astype(float)
    features["valid_hours_d0"] = daily["valid_hours"].astype(float)

    # Season, as a point on a circle so 31 Dec and 1 Jan are neighbours.
    day_of_year = np.array([d.timetuple().tm_yday for d in dates], dtype=float)
    features["doy_sin"] = np.sin(2 * math.pi * day_of_year / 365.25)
    features["doy_cos"] = np.cos(2 * math.pi * day_of_year / 365.25)

    table = pd.DataFrame(features, index=daily.index)
    table.index.name = "for_date"
    return table


FEATURES = (
    [f"{column}_d{lag}" for lag in LAGS for column in DAILY_COLUMNS]
    + [name for window in ROLLING_WINDOWS
       for name in [f"{c}_mean{window}" for c in ROLLING_COLUMNS] + [f"hutton_days_{window}"]]
    + ["judged_days_14", "gap_in_window", "valid_hours_d0", "doy_sin", "doy_cos"]
)


def _known(value):
    """True/False, or None for unknown (None or NaN)."""
    return None if value is None or pd.isna(value) else bool(value)


def _or_unknown(a, b):
    """Three-valued OR: True if either is True; otherwise unknown if either is unknown."""
    a, b = _known(a), _known(b)
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def targets(daily: pd.DataFrame) -> pd.DataFrame:
    """y24 and y48 for each day D as 1.0 / 0.0, or NaN when unknown.

    These look ahead on purpose: they are what is predicted. The last one or
    two days of the table have no future in it, so their targets are NaN.
    """
    day = list(daily["hutton_day"])
    period = list(daily["hutton_period"])
    n = len(daily)
    y24, y48 = [], []
    for i in range(n):
        tomorrow = _known(day[i + 1]) if i + 1 < n else None
        y24.append(np.nan if tomorrow is None else float(tomorrow))
        both = _or_unknown(period[i + 1] if i + 1 < n else None,
                           period[i + 2] if i + 2 < n else None)
        y48.append(np.nan if both is None else float(both))
    return pd.DataFrame({"y24": y24, "y48": y48}, index=daily.index)


def build_dataset(readings: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Readings (timestamp UTC, temperature, humidity) -> features and targets per day.

    One row per local day in [start, end]. A target that would need data
    after `end` is NaN, so nothing after `end` is ever used.
    """
    daily = daily_weather(hutton.to_hourly(readings), start, end)
    table = feature_table(daily).join(targets(daily))
    table["status_d0"] = daily["status"]
    return table


def labelled(dataset: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Rows with a known target for this horizon (unknown targets dropped, never 0)."""
    return dataset[dataset[TARGETS[horizon]].notna()]
