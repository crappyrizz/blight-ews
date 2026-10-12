"""Tests for the Hutton rule and, above all, for how gaps are handled.

Every pure-function test builds a small DataFrame by hand, in UTC, the way
readings are stored. Local days in Africa/Nairobi (UTC+3) start at 21:00 UTC
the evening before, which several tests depend on.
"""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError

from backend import hutton
from backend.hutton import (
    STATUS_INSUFFICIENT,
    STATUS_NO_DATA,
    STATUS_OK,
    compute_hutton_for_node,
    daily_summary,
    display_label,
    hutton_days,
    hutton_periods,
    hutton_score,
    to_hourly,
)
from backend.models import Farmer, RiskScore, SensorNode, SensorReading, WeatherReading

DAY = date(2026, 3, 2)


def local_midnight_utc(day: date) -> datetime:
    """00:00 local on `day`, expressed in UTC (21:00 UTC the day before)."""
    return datetime.combine(day, datetime.min.time(), tzinfo=hutton.LOCAL_TZ).astimezone(timezone.utc)


def readings(n_hours=24, temperature=12.0, humidity=95.0, day=DAY, start_hour=0) -> pd.DataFrame:
    """n_hours of hourly readings on a local day, starting at start_hour local."""
    start = local_midnight_utc(day) + timedelta(hours=start_hour)
    return pd.DataFrame({
        "timestamp": [start + timedelta(hours=h) for h in range(n_hours)],
        "temperature": [temperature] * n_hours,
        "humidity": [humidity] * n_hours,
    })


def judge(df: pd.DataFrame, start_date=None, end_date=None) -> pd.DataFrame:
    """The whole pure pipeline, as compute_hutton_for_node runs it."""
    daily = daily_summary(to_hourly(df), start_date, end_date)
    return hutton_score(hutton_periods(hutton_days(daily)))


# --- to_hourly ---------------------------------------------------------


def test_sub_hourly_readings_aggregate_to_hourly_means():
    start = local_midnight_utc(DAY)
    ten_minutes = [start + timedelta(minutes=10 * i) for i in range(6)]  # 00:00 .. 00:50
    df = pd.DataFrame({
        "timestamp": ten_minutes,
        "temperature": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        "humidity": [85.0, 87.0, 89.0, 91.0, 93.0, 95.0],
    })

    hourly = to_hourly(df)

    assert len(hourly) == 1  # six readings, one hour
    assert hourly["temperature"].iloc[0] == pytest.approx(12.5)
    assert hourly["humidity"].iloc[0] == pytest.approx(90.0)  # the mean reaches the threshold
    assert daily_summary(hourly).loc[DAY, "valid_hours"] == 1
    assert daily_summary(hourly).loc[DAY, "humid_hours"] == 1


def test_hourly_index_is_local_time():
    hourly = to_hourly(readings(1))
    assert str(hourly.index.tz) == "Africa/Nairobi"
    assert hourly.index[0].hour == 0


def test_missing_hours_stay_nan_and_are_not_interpolated():
    df = pd.concat([readings(1, start_hour=0), readings(1, start_hour=3)])
    hourly = to_hourly(df)

    assert len(hourly) == 4  # 00, 01, 02, 03
    assert hourly["temperature"].isna().tolist() == [False, True, True, False]


def test_half_readings_do_not_count_as_valid_hours():
    df = readings(20)
    df.loc[0, "humidity"] = None
    assert daily_summary(to_hourly(df)).loc[DAY, "valid_hours"] == 19


def test_repeat_readings_in_one_hour_count_once():
    df = pd.concat([readings(20), readings(20)])  # node sent everything twice
    assert daily_summary(to_hourly(df)).loc[DAY, "valid_hours"] == 20


# --- daily_summary -----------------------------------------------------


def test_humid_hours_spanning_midnight_split_into_two_local_days():
    # Two full local days at 80% RH ...
    df = pd.concat([readings(24, humidity=80.0, day=DAY - timedelta(days=1)), readings(24, humidity=80.0)])
    # ... with six humid hours 18:00-23:00 UTC on 1 March, which is
    # 21:00-02:00 local: three hours on 1 March and three on 2 March.
    humid_utc = [datetime(2026, 3, 1, h, tzinfo=timezone.utc) for h in range(18, 24)]
    df.loc[df["timestamp"].isin(humid_utc), "humidity"] = 95.0

    daily = judge(df)

    assert daily.loc[date(2026, 3, 1), "humid_hours"] == 3
    assert daily.loc[date(2026, 3, 2), "humid_hours"] == 3
    # Judged in UTC, 1 March would have had all 6 and been a Hutton day.
    assert daily.loc[date(2026, 3, 1), "hutton_day"] is False
    assert daily.loc[date(2026, 3, 2), "hutton_day"] is False


def test_summary_columns():
    df = readings(24, humidity=80.0)
    df["temperature"] = [8.0 + h * 0.5 for h in range(24)]
    df.loc[[2, 3, 4, 10, 11], "humidity"] = 92.0

    day = daily_summary(to_hourly(df)).loc[DAY]

    assert day["tmin"] == 8.0
    assert day["tmax"] == 8.0 + 23 * 0.5
    assert day["humid_hours"] == 5
    assert day["valid_hours"] == 24
    assert day["longest_humid_run"] == 3


def test_a_missing_hour_breaks_a_humid_run():
    df = readings(6, humidity=95.0)
    df = df.drop(index=3)  # hour 03 missing: we do not know it was humid
    assert daily_summary(to_hourly(df)).loc[DAY, "longest_humid_run"] == 3


def test_days_without_readings_get_a_row_when_a_range_is_given():
    daily = judge(readings(24), DAY - timedelta(days=1), DAY + timedelta(days=1))

    assert list(daily.index) == [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)]
    assert daily.loc[DAY + timedelta(days=1), "status"] == STATUS_NO_DATA
    assert daily.loc[DAY + timedelta(days=1), "valid_hours"] == 0


# --- hutton_days: the thresholds ---------------------------------------


def test_tmin_exactly_10_counts():
    assert judge(readings(24, temperature=10.0)).loc[DAY, "hutton_day"] is True


def test_tmin_9_99_does_not_count():
    df = readings(24, temperature=12.0)
    df.loc[5, "temperature"] = 9.99  # one cold hour drags the minimum down
    assert judge(df).loc[DAY, "hutton_day"] is False


def test_five_humid_hours_is_not_a_hutton_day():
    df = readings(24, humidity=80.0)
    df.loc[:4, "humidity"] = 92.0  # rows 0-4: five hours
    day = judge(df).loc[DAY]
    assert day["humid_hours"] == 5
    assert day["hutton_day"] is False


def test_six_humid_hours_is_a_hutton_day():
    df = readings(24, humidity=80.0)
    df.loc[:5, "humidity"] = 90.0  # rows 0-5: six hours, threshold inclusive
    day = judge(df).loc[DAY]
    assert day["humid_hours"] == 6
    assert day["hutton_day"] is True


# --- the 20-of-24 completeness rule ------------------------------------


def test_19_valid_hours_is_unknown_with_no_score():
    day = judge(readings(19)).loc[DAY]

    assert day["status"] == STATUS_INSUFFICIENT
    assert day["valid_hours"] == 19
    assert day["hutton_day"] is None  # unknown, not False
    assert day["score"] is None  # never 0.0


def test_20_valid_hours_is_evaluated():
    day = judge(readings(20)).loc[DAY]

    assert day["status"] == STATUS_OK
    assert day["hutton_day"] is True  # 12 C and 20 hours at 95% RH
    assert day["score"] == hutton.SCORE_HUTTON_DAY


# --- hutton_periods ----------------------------------------------------


def test_two_consecutive_hutton_days_flag_a_period_on_the_second_day_only():
    df = pd.concat([readings(24, day=DAY - timedelta(days=1)), readings(24)])
    daily = judge(df)

    assert daily.loc[DAY - timedelta(days=1), "hutton_period"] is None  # its D-1 is not known
    assert daily.loc[DAY, "hutton_period"] is True


def test_hutton_day_then_unknown_day_then_hutton_day_is_no_period():
    df = pd.concat([
        readings(24, day=DAY - timedelta(days=1)),
        readings(19),  # unknown day in the middle
        readings(24, day=DAY + timedelta(days=1)),
    ])
    daily = judge(df)

    assert daily.loc[DAY, "hutton_day"] is None
    # Unknown, NOT True and NOT False: we cannot say the two-day condition held.
    assert daily.loc[DAY, "hutton_period"] is None
    assert daily.loc[DAY + timedelta(days=1), "hutton_period"] is None


def test_calm_day_after_hutton_day_is_a_known_non_period():
    df = pd.concat([readings(24, day=DAY - timedelta(days=1)), readings(24, temperature=8.0)])
    assert judge(df).loc[DAY, "hutton_period"] is False


# --- hutton_score ------------------------------------------------------


def test_score_is_1_on_the_period_day_and_the_day_after():
    df = pd.concat([
        readings(24, day=DAY - timedelta(days=1)),
        readings(24),
        readings(24, temperature=8.0, day=DAY + timedelta(days=1)),  # calm day
        readings(24, temperature=8.0, day=DAY + timedelta(days=2)),
    ])
    scores = judge(df)["score"]

    assert scores[DAY - timedelta(days=1)] == hutton.SCORE_HUTTON_DAY  # 0.5
    assert scores[DAY] == hutton.SCORE_HUTTON_PERIOD  # 1.0, period flagged today
    assert scores[DAY + timedelta(days=1)] == hutton.SCORE_HUTTON_PERIOD  # period yesterday
    assert scores[DAY + timedelta(days=2)] == hutton.SCORE_NO_HUTTON_DAY  # 0.0


def test_score_constants():
    assert (hutton.SCORE_NO_HUTTON_DAY, hutton.SCORE_HUTTON_DAY, hutton.SCORE_HUTTON_PERIOD) == (0.0, 0.5, 1.0)


# --- dashboard wording -------------------------------------------------


def test_dashboard_never_calls_an_unjudged_day_low_risk():
    assert display_label(STATUS_NO_DATA, 0, None) == "No reading"
    assert display_label(STATUS_INSUFFICIENT, 19, None) == "Incomplete data (19 of 24 hours)"


def test_dashboard_wording_for_judged_days():
    assert display_label(STATUS_OK, 24, 0.0) == "Low risk"
    assert display_label(STATUS_OK, 24, hutton.SCORE_HUTTON_DAY) == "Medium risk"
    assert display_label(STATUS_OK, 24, hutton.SCORE_HUTTON_PERIOD) == "High risk"


# --- compute_hutton_for_node (database) --------------------------------


def make_node(session):
    farmer = Farmer(name="T", phone_number="+254799000001", password_hash="x", role="farmer")
    session.add(farmer)
    session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location="p", latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def add_sensor_readings(session, node, df):
    for row in df.itertuples():
        session.add(SensorReading(
            node_id=node.node_id, timestamp=row.timestamp,
            temperature=row.temperature, humidity=row.humidity,
        ))
    session.flush()


def stored_rows(session, node, data_source="sensor"):
    return {
        row.for_date: row
        for row in session.query(RiskScore).filter_by(
            node_id=node.node_id, method="hutton", data_source=data_source
        )
    }


def test_compute_writes_one_row_per_day_including_gaps(db_session):
    node = make_node(db_session)
    add_sensor_readings(db_session, node, pd.concat([
        readings(24, day=DAY - timedelta(days=1)),
        readings(24),
        readings(19, day=DAY + timedelta(days=1)),
    ]))

    compute_hutton_for_node(node.node_id, DAY - timedelta(days=1), DAY + timedelta(days=2),
                            "sensor", session=db_session)
    rows = stored_rows(db_session, node)

    assert len(rows) == 4
    assert rows[DAY].status == STATUS_OK
    assert rows[DAY].hutton_period is True
    assert rows[DAY].score == hutton.SCORE_HUTTON_PERIOD
    assert rows[DAY].horizon_hours == 0
    # The gaps are visible in the history rather than absent from it.
    assert (rows[DAY + timedelta(days=1)].status, rows[DAY + timedelta(days=1)].valid_hours) == (STATUS_INSUFFICIENT, 19)
    assert rows[DAY + timedelta(days=1)].score is None
    assert rows[DAY + timedelta(days=2)].status == STATUS_NO_DATA
    assert rows[DAY + timedelta(days=2)].hutton_day is None


def test_compute_uses_lead_in_days_but_does_not_write_them(db_session):
    node = make_node(db_session)
    add_sensor_readings(db_session, node, pd.concat([
        readings(24, day=DAY - timedelta(days=1)), readings(24),
    ]))

    compute_hutton_for_node(node.node_id, DAY, DAY, "sensor", session=db_session)
    rows = stored_rows(db_session, node)

    assert list(rows) == [DAY]
    assert rows[DAY].hutton_period is True  # yesterday was read, not written


def test_recomputing_updates_the_same_rows(db_session):
    node = make_node(db_session)
    compute_hutton_for_node(node.node_id, DAY, DAY, "sensor", session=db_session)  # no readings yet
    add_sensor_readings(db_session, node, readings(24))
    compute_hutton_for_node(node.node_id, DAY, DAY, "sensor", session=db_session)

    rows = db_session.query(RiskScore).filter_by(node_id=node.node_id, for_date=DAY).all()
    assert len(rows) == 1
    assert rows[0].status == STATUS_OK


def test_compute_from_api_reads_the_weather_archive_at_the_node(db_session):
    node = make_node(db_session)
    for row in readings(24).itertuples():
        db_session.add(WeatherReading(
            latitude=node.latitude, longitude=node.longitude, source="archive",
            timestamp=row.timestamp, temperature=row.temperature, humidity=row.humidity,
        ))
    db_session.flush()

    compute_hutton_for_node(node.node_id, DAY, DAY, "api", session=db_session)

    assert stored_rows(db_session, node, "api")[DAY].hutton_day is True
    assert stored_rows(db_session, node, "sensor") == {}


def test_compute_rejects_an_unknown_data_source(db_session):
    node = make_node(db_session)
    with pytest.raises(ValueError, match="data_source"):
        compute_hutton_for_node(node.node_id, DAY, DAY, "satellite", session=db_session)


def test_local_day_boundaries_follow_nairobi_time(db_session):
    """A reading at 23:00 UTC belongs to the next local day (UTC+3)."""
    node = make_node(db_session)
    db_session.add(SensorReading(
        node_id=node.node_id, temperature=12.0, humidity=95.0,
        timestamp=datetime.fromisoformat("2026-03-01T23:00:00+00:00"),  # 02:00 local, 2 March
    ))
    db_session.flush()

    daily = compute_hutton_for_node(node.node_id, date(2026, 3, 1), date(2026, 3, 2),
                                    "sensor", session=db_session)

    assert daily.loc[date(2026, 3, 1), "valid_hours"] == 0
    assert daily.loc[date(2026, 3, 2), "valid_hours"] == 1


def test_database_rejects_a_score_on_an_unjudged_day(db_session):
    node = make_node(db_session)
    db_session.add(RiskScore(
        node_id=node.node_id, method="hutton", data_source="sensor", horizon_hours=0,
        status=STATUS_INSUFFICIENT, score=0.0, valid_hours=19, for_date=DAY,
    ))
    with pytest.raises(IntegrityError, match="ck_risk_scores_score_matches_status"):
        db_session.flush()
    db_session.rollback()


def test_database_rejects_an_unknown_status(db_session):
    node = make_node(db_session)
    db_session.add(RiskScore(
        node_id=node.node_id, method="hutton", data_source="sensor", horizon_hours=0,
        status="maybe", score=None, for_date=DAY,
    ))
    with pytest.raises(IntegrityError, match="ck_risk_scores_status"):
        db_session.flush()
    db_session.rollback()
