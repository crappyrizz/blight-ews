"""Tests for the Hutton rule and, above all, for how gaps are handled."""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from backend import hutton
from backend.hutton import (
    STATUS_INSUFFICIENT,
    STATUS_NO_DATA,
    STATUS_OK,
    DaySummary,
    display_label,
    hutton_period,
    score_for,
    summarise_day,
)
from backend.models import Farmer, RiskScore, SensorNode, SensorReading

DAY = date(2026, 3, 2)


class FakeReading:
    """Minimal stand-in for a SensorReading row."""

    def __init__(self, timestamp, temperature, humidity):
        self.timestamp = timestamp
        self.temperature = temperature
        self.humidity = humidity


def hourly(n_hours, temperature=12.0, humidity=95.0, day=DAY):
    """n_hours of readings on the given local day, one per hour from 00:00."""
    # 00:00 local (Africa/Nairobi, UTC+3) is 21:00 UTC the day before.
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=hutton.LOCAL_TZ)
    return [
        FakeReading(start_local + timedelta(hours=h), temperature, humidity)
        for h in range(n_hours)
    ]


# --- the 20-of-24 completeness rule ------------------------------------


def test_19_hours_is_insufficient_and_has_no_score():
    summary = summarise_day(DAY, hourly(19))

    assert summary.status == STATUS_INSUFFICIENT
    assert summary.valid_hours == 19
    assert summary.hutton_day is None  # unknown, not False
    assert score_for(summary, None) is None  # never 0.0


def test_20_hours_is_ok():
    summary = summarise_day(DAY, hourly(20))

    assert summary.status == STATUS_OK
    assert summary.valid_hours == 20
    assert summary.hutton_day is True  # 12 C and 20 hours at 95% RH
    assert score_for(summary, None) == hutton.SCORE_HUTTON_DAY


def test_no_readings_at_all_is_no_data():
    summary = summarise_day(DAY, [])

    assert summary.status == STATUS_NO_DATA
    assert summary.valid_hours == 0
    assert summary.hutton_day is None
    assert score_for(summary, None) is None


def test_repeat_readings_in_one_hour_count_once():
    readings = hourly(20) + hourly(20)  # node reported everything twice
    assert summarise_day(DAY, readings).valid_hours == 20


def test_half_readings_do_not_count_as_valid_hours():
    readings = hourly(20)
    readings[0].humidity = None
    assert summarise_day(DAY, readings).valid_hours == 19


# --- the Hutton day test itself ----------------------------------------


def test_cold_night_is_not_a_hutton_day():
    readings = hourly(24, temperature=12.0, humidity=95.0)
    readings[3].temperature = 9.5  # one hour below 10 C drags the minimum down

    summary = summarise_day(DAY, readings)
    assert summary.status == STATUS_OK
    assert summary.hutton_day is False
    assert score_for(summary, None) == 0.0


def test_too_few_humid_hours_is_not_a_hutton_day():
    readings = hourly(24, temperature=12.0, humidity=80.0)
    for reading in readings[:5]:  # only 5 hours at/above 90%
        reading.humidity = 92.0

    summary = summarise_day(DAY, readings)
    assert summary.hours_at_high_humidity == 5
    assert summary.hutton_day is False


def test_exactly_six_humid_hours_is_a_hutton_day():
    readings = hourly(24, temperature=12.0, humidity=80.0)
    for reading in readings[:6]:
        reading.humidity = 90.0  # the threshold is inclusive

    assert summarise_day(DAY, readings).hutton_day is True


# --- Hutton periods ----------------------------------------------------


def test_two_consecutive_hutton_days_make_a_period():
    yesterday = summarise_day(DAY - timedelta(days=1), hourly(24, day=DAY - timedelta(days=1)))
    today = summarise_day(DAY, hourly(24))

    assert hutton_period(yesterday, today) is True
    assert score_for(today, True) == hutton.SCORE_HUTTON_PERIOD


def test_hutton_day_after_insufficient_day_gives_unknown_period():
    yesterday = summarise_day(DAY - timedelta(days=1), hourly(19, day=DAY - timedelta(days=1)))
    today = summarise_day(DAY, hourly(24))

    # Unknown, NOT False: we cannot say the two-day condition failed.
    assert hutton_period(yesterday, today) is None


def test_insufficient_day_after_hutton_day_gives_no_period():
    yesterday = summarise_day(DAY - timedelta(days=1), hourly(24, day=DAY - timedelta(days=1)))
    today = summarise_day(DAY, hourly(19))

    assert hutton_period(yesterday, today) is None
    assert score_for(today, None) is None


def test_missing_yesterday_gives_unknown_period():
    today = summarise_day(DAY, hourly(24))
    assert hutton_period(None, today) is None


def test_calm_day_after_hutton_day_is_a_known_non_period():
    yesterday = summarise_day(DAY - timedelta(days=1), hourly(24, day=DAY - timedelta(days=1)))
    today = summarise_day(DAY, hourly(24, temperature=8.0))

    assert hutton_period(yesterday, today) is False


# --- dashboard wording -------------------------------------------------


def test_dashboard_never_calls_an_unjudged_day_low_risk():
    assert display_label(STATUS_NO_DATA, 0, None) == "No reading"
    assert display_label(STATUS_INSUFFICIENT, 19, None) == "Incomplete data (19 of 24 hours)"


def test_dashboard_wording_for_judged_days():
    assert display_label(STATUS_OK, 24, 0.0) == "Low risk"
    assert display_label(STATUS_OK, 24, hutton.SCORE_HUTTON_DAY) == "Medium risk"
    assert display_label(STATUS_OK, 24, hutton.SCORE_HUTTON_PERIOD) == "High risk"


# --- writing rows to the database --------------------------------------


def make_node(session):
    farmer = Farmer(name="T", phone_number="+254799000001", password_hash="x", role="farmer")
    session.add(farmer)
    session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location="p", latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def test_insufficient_day_is_written_as_a_row_not_skipped(db_session):
    node = make_node(db_session)
    for reading in hourly(19):
        db_session.add(SensorReading(
            node_id=node.node_id,
            temperature=reading.temperature,
            humidity=reading.humidity,
            timestamp=reading.timestamp,
        ))
    db_session.flush()

    row = hutton.compute_and_store(db_session, node.node_id, DAY)

    # The gap is visible in the history rather than absent from it.
    assert row.status == STATUS_INSUFFICIENT
    assert row.valid_hours == 19
    assert row.score is None
    assert row.hutton_day is None
    assert row.hutton_period is None


def test_recomputing_a_day_updates_the_same_row(db_session):
    node = make_node(db_session)
    hutton.compute_and_store(db_session, node.node_id, DAY)  # no readings yet
    hutton.compute_and_store(db_session, node.node_id, DAY)

    rows = db_session.query(RiskScore).filter_by(node_id=node.node_id, for_date=DAY).all()
    assert len(rows) == 1
    assert rows[0].status == STATUS_NO_DATA


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


def test_local_day_boundaries_follow_nairobi_time(db_session):
    """A reading at 23:00 UTC belongs to the next local day (UTC+3)."""
    node = make_node(db_session)
    db_session.add(SensorReading(
        node_id=node.node_id, temperature=12.0, humidity=95.0,
        timestamp=datetime.fromisoformat("2026-03-01T23:00:00+00:00"),  # 02:00 local, 2 March
    ))
    db_session.flush()

    assert hutton.compute_day(db_session, node.node_id, date(2026, 3, 1)).valid_hours == 0
    assert hutton.compute_day(db_session, node.node_id, date(2026, 3, 2)).valid_hours == 1
