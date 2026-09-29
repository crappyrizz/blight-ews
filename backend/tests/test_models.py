from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend.models import Farmer, SensorNode, SensorReading, WeatherReading


def make_node(session):
    farmer = Farmer(name="Test Farmer", phone_number="+254711111111", password_hash="x", role="farmer")
    session.add(farmer)
    session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location="Test plot", latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def test_ids_are_generated_with_prefixes(db_session):
    node = make_node(db_session)
    assert node.node_id.startswith("NODE-")
    assert node.farmer_id.startswith("FRM-")


def test_duplicate_sensor_reading_is_rejected(db_session):
    node = make_node(db_session)
    ts = datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)

    db_session.add(SensorReading(node_id=node.node_id, temperature=12.5, humidity=91.0, timestamp=ts))
    db_session.flush()

    # Same node, same timestamp: must violate UNIQUE(node_id, timestamp).
    db_session.add(SensorReading(node_id=node.node_id, temperature=13.0, humidity=92.0, timestamp=ts))
    with pytest.raises(IntegrityError, match="uq_sensor_readings_node_timestamp"):
        db_session.flush()
    db_session.rollback()


def test_same_timestamp_on_different_nodes_is_allowed(db_session):
    node_a = make_node(db_session)
    node_b = SensorNode(farmer_id=node_a.farmer_id, location="Second plot", latitude=-0.71, longitude=36.61)
    db_session.add(node_b)
    db_session.flush()
    ts = datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)

    db_session.add(SensorReading(node_id=node_a.node_id, temperature=12.5, humidity=91.0, timestamp=ts))
    db_session.add(SensorReading(node_id=node_b.node_id, temperature=12.7, humidity=90.0, timestamp=ts))
    db_session.flush()  # no error


def test_duplicate_archive_weather_row_is_rejected(db_session):
    # forecast_issued_at is NULL for archive data; NULLS NOT DISTINCT must
    # still treat these two rows as duplicates.
    ts = datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)
    row = dict(latitude=-0.7, longitude=36.6, source="archive", timestamp=ts, temperature=11.0, humidity=88.0)

    db_session.add(WeatherReading(**row))
    db_session.flush()
    db_session.add(WeatherReading(**row))
    with pytest.raises(IntegrityError, match="uq_weather_readings_source_point_time"):
        db_session.flush()
    db_session.rollback()


def test_invalid_role_is_rejected(db_session):
    db_session.add(Farmer(name="X", phone_number="+254722222222", password_hash="x", role="superuser"))
    with pytest.raises(IntegrityError, match="ck_farmers_role"):
        db_session.flush()
    db_session.rollback()
