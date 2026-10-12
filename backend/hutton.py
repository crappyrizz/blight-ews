"""Hutton Criteria: the baseline blight-risk rule, and how gaps are handled.

Definition (Hutton Criteria: Dancy, 2017):

    Dancy, S. et al. (2017). "Hutton Criteria: a classification tool for
    identifying high risk periods for potato late blight disease development
    in Great Britain." Proceedings of the 16th EuroBlight Workshop.

The Hutton Criteria replaced the older Smith Period in the UK. As used here:

- A "Hutton day" is a local calendar day (Africa/Nairobi) with
  minimum temperature >= 10.0 C AND at least 6 hours with relative
  humidity >= 90%.
- A "Hutton period" is two consecutive Hutton days. It is flagged on the
  second day, because that is when the warning becomes meaningful.

Missing data is the other half of this module. A day needs at least 20 of
its 24 hourly values to be judged at all. A day with fewer is recorded as
'insufficient_data' (or 'no_data') with score NULL, not as a low-risk day,
and it is written to the database rather than skipped, so a gap is visible
as a gap in the farmer's history instead of simply being absent. Gaps are
never interpolated.

Layout of this file:

1. Pure functions on pandas DataFrames (no database access), one per step:
       to_hourly -> daily_summary -> hutton_days -> hutton_periods -> hutton_score
   Each takes the previous step's output and returns a new DataFrame with
   its own columns added, so every step can be tested on its own with a
   small hand-built table.
2. compute_hutton_for_node(): the database wrapper that loads readings,
   runs the pipeline and writes risk_scores rows.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import RiskScore, SensorNode, SensorReading, WeatherReading

# --- Hutton thresholds -------------------------------------------------
MIN_TEMPERATURE_C = 10.0  # daily minimum temperature must reach this
RH_THRESHOLD_PCT = 90.0  # "high humidity" means at or above this
MIN_HOURS_AT_RH = 6  # ... for at least this many hours in the day

# --- Data-completeness rule --------------------------------------------
HOURS_IN_DAY = 24
MIN_VALID_HOURS = 20  # fewer than this and the day cannot be judged

# --- Row status values (mirror the CHECK constraint on risk_scores) ----
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_NO_DATA = "no_data"

# --- Score attached to a judged day ------------------------------------
# The Hutton rule is a set of yes/no conditions, so these are conventions
# for expressing it on the 0-1 scale that fusion and the dashboard use.
# A judged day only ever gets one of these three values; an unjudged day
# gets None (NULL in the database), never 0.0.
SCORE_NO_HUTTON_DAY = 0.0
SCORE_HUTTON_DAY = 0.5  # conditions met today, but not yet two days running
SCORE_HUTTON_PERIOD = 1.0  # a Hutton period today or yesterday: the real warning

LOCAL_TZ = ZoneInfo(settings.TIMEZONE)

DATA_SOURCES = ("sensor", "api")


# --- 1. Pure pipeline --------------------------------------------------


def to_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """Readings (UTC) -> one row per local hour, holding the hourly means.

    Input columns: timestamp (UTC), temperature, humidity. Readings may come
    at any interval (the node, or the simulator, may sample every 10 min).

    - Timestamps are converted to Africa/Nairobi first, so that each hour,
      and later each day, is a LOCAL hour and day.
    - Each hour gets the mean of the readings inside it. Repeats of the same
      reading therefore cannot inflate the number of valid hours.
    - A reading missing temperature or humidity is dropped: half a reading
      tells us nothing about whether that hour met the Hutton conditions.
    - An hour with no readings stays NaN. Nothing is interpolated.
    """
    empty = pd.DataFrame(
        {"temperature": pd.Series(dtype=float), "humidity": pd.Series(dtype=float)},
        index=pd.DatetimeIndex([], tz=settings.TIMEZONE, name="hour"),
    )
    if df.empty:
        return empty

    data = pd.DataFrame({
        # utc=True: the stored timestamps are UTC; then shift to local time.
        "hour": pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(settings.TIMEZONE),
        "temperature": pd.to_numeric(df["temperature"], errors="coerce"),
        "humidity": pd.to_numeric(df["humidity"], errors="coerce"),
    }).dropna(subset=["temperature", "humidity"])
    if data.empty:
        return empty

    # resample("1h") makes one row per clock hour from the first reading's
    # hour to the last's. Hours with no readings come out as NaN.
    return data.set_index("hour").sort_index().resample("1h").mean()


def _longest_run(flags) -> int:
    """Longest stretch of consecutive True values (a NaN hour breaks it)."""
    longest = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def daily_summary(
    hourly: pd.DataFrame, start_date: date | None = None, end_date: date | None = None
) -> pd.DataFrame:
    """Hourly means -> one row per local calendar day.

    Columns: tmin, tmax, humid_hours (hours with RH >= 90), valid_hours
    (hours with both values present) and longest_humid_run (the longest
    unbroken stretch of humid hours; a missing hour breaks the stretch,
    because we do not know it was humid).

    If start_date and end_date are given, every day in that range gets a
    row, including days with no readings at all (valid_hours = 0). That is
    how a silent sensor shows up as a gap instead of disappearing.
    """
    rows = {}
    if not hourly.empty:
        # The index is already local time, so .date is the local calendar day.
        for for_date, day in hourly.groupby(hourly.index.date):
            valid = day.dropna(subset=["temperature", "humidity"])
            humid = valid["humidity"] >= RH_THRESHOLD_PCT
            # Over ALL hours of the day, in order, so a NaN hour (False here)
            # breaks a run rather than being skipped over.
            humid_in_order = (day["humidity"] >= RH_THRESHOLD_PCT) & day["temperature"].notna()
            rows[for_date] = {
                "tmin": valid["temperature"].min() if len(valid) else float("nan"),
                "tmax": valid["temperature"].max() if len(valid) else float("nan"),
                "humid_hours": int(humid.sum()),
                "valid_hours": len(valid),
                "longest_humid_run": _longest_run(humid_in_order),
            }

    if start_date is not None and end_date is not None:
        day = start_date
        while day <= end_date:
            rows.setdefault(day, {
                "tmin": float("nan"), "tmax": float("nan"),
                "humid_hours": 0, "valid_hours": 0, "longest_humid_run": 0,
            })
            day += timedelta(days=1)

    columns = ["tmin", "tmax", "humid_hours", "valid_hours", "longest_humid_run"]
    daily = pd.DataFrame.from_dict(rows, orient="index", columns=columns).sort_index()
    daily.index.name = "for_date"
    return daily


def _status(valid_hours: int) -> str:
    if valid_hours == 0:
        return STATUS_NO_DATA
    if valid_hours < MIN_VALID_HOURS:
        return STATUS_INSUFFICIENT
    return STATUS_OK


def hutton_days(daily: pd.DataFrame) -> pd.DataFrame:
    """Add status and hutton_day to each day.

    hutton_day = (tmin >= 10.0) AND (humid_hours >= 6), but None where
    valid_hours < 20. None means "unknown", which is deliberately different
    from False: a day we could not measure is not a day that was safe.
    """
    out = daily.copy()
    statuses, flags = [], []
    for _, day in out.iterrows():
        status = _status(int(day["valid_hours"]))
        statuses.append(status)
        if status != STATUS_OK:
            flags.append(None)  # known unknown: we say so, rather than guess
        else:
            flags.append(bool(
                day["tmin"] >= MIN_TEMPERATURE_C
                and day["humid_hours"] >= MIN_HOURS_AT_RH
            ))
    out["status"] = statuses
    out["hutton_day"] = pd.Series(flags, index=out.index, dtype=object)
    return out


def hutton_periods(daily: pd.DataFrame) -> pd.DataFrame:
    """Add hutton_period: True on day D when D and D-1 are both Hutton days.

    None if either day is unknown (or D-1 is not in the table): a period is
    a claim about both days, and one unknown day makes the claim unknown. It
    is never False just because yesterday's data is missing.
    """
    out = daily if "hutton_day" in daily else hutton_days(daily)
    out = out.copy()
    by_date = dict(zip(out.index, out["hutton_day"]))

    periods = []
    for for_date in out.index:
        today = by_date[for_date]
        yesterday = by_date.get(for_date - timedelta(days=1))
        if today is None or yesterday is None:
            periods.append(None)
        else:
            periods.append(bool(today and yesterday))
    out["hutton_period"] = pd.Series(periods, index=out.index, dtype=object)
    return out


def hutton_score(daily: pd.DataFrame) -> pd.DataFrame:
    """Add score in [0, 1] per day, for use in fusion.

    - 1.0 if a Hutton period is flagged on D or on D-1 (the warning carries
      over one day, since the risk does not vanish at midnight);
    - 0.5 if D is a Hutton day;
    - 0.0 otherwise;
    - None if D itself could not be judged. Never 0.0 for an unknown day:
      0.0 states that conditions do not favour blight, which we cannot know
      from missing data, and both the dashboard and the learned model would
      read it as a genuinely safe day.
    """
    out = daily if "hutton_period" in daily else hutton_periods(daily)
    out = out.copy()
    period_by_date = dict(zip(out.index, out["hutton_period"]))

    scores = []
    for for_date, day in out.iterrows():
        if day["status"] != STATUS_OK:
            scores.append(None)
        elif period_by_date[for_date] or period_by_date.get(for_date - timedelta(days=1)):
            scores.append(SCORE_HUTTON_PERIOD)
        elif day["hutton_day"]:
            scores.append(SCORE_HUTTON_DAY)
        else:
            scores.append(SCORE_NO_HUTTON_DAY)
    out["score"] = pd.Series(scores, index=out.index, dtype=object)
    return out


def display_label(status: str, valid_hours: int | None, score: float | None) -> str:
    """Wording for the dashboard. An unjudged day never reads as 'Low risk'."""
    if status == STATUS_NO_DATA:
        return "No reading"
    if status == STATUS_INSUFFICIENT:
        return f"Incomplete data ({valid_hours} of {HOURS_IN_DAY} hours)"

    # Imported here to keep the risk-level bands in one place (fusion.py)
    # without the two modules importing each other at module level.
    from backend.fusion import risk_level

    return {"low": "Low risk", "medium": "Medium risk", "high": "High risk"}[
        risk_level(score)
    ]


# --- 2. Database wrapper -----------------------------------------------


def local_day_bounds(for_date: date) -> tuple[datetime, datetime]:
    """UTC start and end instants of a local (Africa/Nairobi) calendar day.

    Readings are stored in UTC, but a "day" for the Hutton rule is a local
    day, so the window is shifted by the local offset (UTC+3 here).
    """
    start_local = datetime.combine(for_date, time.min, tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local


def load_readings(
    session: Session, node_id: str, start: datetime, end: datetime, data_source: str
) -> pd.DataFrame:
    """Readings in [start, end) as a DataFrame (timestamp, temperature, humidity).

    data_source='sensor': the node's own readings (sensor_readings).
    data_source='api':    the Open-Meteo archive at the node's location
                          (weather_readings, source='archive').
    """
    if data_source == "sensor":
        statement = select(
            SensorReading.timestamp, SensorReading.temperature, SensorReading.humidity
        ).where(
            SensorReading.node_id == node_id,
            SensorReading.timestamp >= start,
            SensorReading.timestamp < end,
        )
    elif data_source == "api":
        node = session.get(SensorNode, node_id)
        if node is None:
            raise ValueError(f"unknown node {node_id}")
        statement = select(
            WeatherReading.timestamp, WeatherReading.temperature, WeatherReading.humidity
        ).where(
            WeatherReading.source == "archive",
            WeatherReading.latitude == node.latitude,
            WeatherReading.longitude == node.longitude,
            WeatherReading.timestamp >= start,
            WeatherReading.timestamp < end,
        )
    else:
        raise ValueError(f"data_source must be one of {DATA_SOURCES}, not {data_source!r}")

    rows = session.execute(statement).all()
    return pd.DataFrame(rows, columns=["timestamp", "temperature", "humidity"])


def _store_day(
    session: Session, node_id: str, for_date: date, day: pd.Series, data_source: str
) -> RiskScore:
    """Write (or update) the Hutton row for one day.

    Rows are written for unjudged days too, so the history shows the gap.
    Re-running for the same day updates the existing row instead of adding a
    second one, which makes the daily job safe to re-run.
    """
    row = session.scalar(
        select(RiskScore).where(
            RiskScore.node_id == node_id,
            RiskScore.method == "hutton",
            RiskScore.data_source == data_source,
            RiskScore.horizon_hours == 0,
            RiskScore.for_date == for_date,
        )
    )
    if row is None:
        row = RiskScore(
            node_id=node_id,
            method="hutton",
            data_source=data_source,
            horizon_hours=0,
            for_date=for_date,
        )
        session.add(row)

    row.status = day["status"]
    row.valid_hours = int(day["valid_hours"])
    row.hutton_day = day["hutton_day"]
    row.hutton_period = day["hutton_period"]
    row.score = day["score"]
    row.computed_at = func.now()  # refreshed on a re-run, not only on insert
    return row


def compute_hutton_for_node(
    node_id: str,
    start_date: date,
    end_date: date,
    data_source: str = "sensor",
    session: Session | None = None,
) -> pd.DataFrame:
    """Run the Hutton pipeline for each local day in [start_date, end_date].

    Upserts one risk_scores row per day (method='hutton', horizon_hours=0),
    including days with no or too little data, and returns the daily table.

    Two extra days before start_date are loaded but not written: the period
    on start_date needs the day before it, and the score on start_date needs
    the period on the day before that.

    Pass a session to take part in the caller's transaction (it is flushed,
    not committed); with no session, one is opened and committed here.
    """
    if session is None:
        from backend.db import SessionLocal

        with SessionLocal() as own_session:
            result = compute_hutton_for_node(
                node_id, start_date, end_date, data_source, session=own_session
            )
            own_session.commit()
            return result

    if data_source not in DATA_SOURCES:
        raise ValueError(f"data_source must be one of {DATA_SOURCES}, not {data_source!r}")

    lead_in_start = start_date - timedelta(days=2)
    window_start, _ = local_day_bounds(lead_in_start)
    _, window_end = local_day_bounds(end_date)

    readings = load_readings(session, node_id, window_start, window_end, data_source)
    daily = daily_summary(to_hourly(readings), lead_in_start, end_date)
    daily = hutton_score(hutton_periods(hutton_days(daily)))

    in_range = [start_date <= d <= end_date for d in daily.index]
    daily = daily[in_range]

    for for_date, day in daily.iterrows():
        _store_day(session, node_id, for_date, day, data_source)
    session.flush()
    return daily
