"""Tests for POST /readings: ESP32 readings with X-Device-Key, one at a time or buffered.

The device is a row in sensor_nodes (its node_id is the device_id) and its
readings land in sensor_readings, the same tables POST /nodes/{id}/readings
uses, so the risk layer sees them without any change.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from backend import auth, hutton
from backend.models import Farmer, RiskScore, SensorNode, SensorReading

DEVICE_KEY = "test-device-key-abcdefghijklmnop"
# Whole hours, 30 days ago: fixed points in the past that cannot fall in the future.
BASE = (datetime.now(UTC) - timedelta(days=30)).replace(minute=0, second=0, microsecond=0)


@pytest.fixture
def device(db_session):
    """A farmer with one ESP32 node that has a key."""
    farmer = Farmer(name="Kinangop Farmer", phone_number="+254712000009",
                    password_hash=auth.hash_password("shangi-2026"), role="farmer")
    db_session.add(farmer)
    db_session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location="Kinangop plot", latitude=-0.7, longitude=36.6,
                      api_key_hash=auth.hash_node_key(DEVICE_KEY))
    db_session.add(node)
    db_session.flush()
    return node


def reading(device, hours=0, temp_c=12.4, rh=93.1, **overrides):
    row = {"device_id": device.node_id,
           "ts": (BASE + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
           "temp_c": temp_c, "rh": rh}
    return {**row, **overrides}


def post(client, body, key=DEVICE_KEY):
    headers = {} if key is None else {"X-Device-Key": key}
    return client.post("/readings", json=body, headers=headers)


def stored(db_session, device):
    return db_session.scalars(select(SensorReading).where(SensorReading.node_id == device.node_id)
                              .order_by(SensorReading.timestamp)).all()


# --- valid readings ----------------------------------------------------------------


def test_a_single_reading_is_stored(client, db_session, device):
    response = post(client, reading(device, temp_c=11.5, rh=96.0))

    assert response.status_code == 200
    assert response.json() == {"stored": 1, "skipped": 0, "rejected": [], "risk_level": "unknown"}
    [row] = stored(db_session, device)
    assert (row.timestamp, row.temperature, row.humidity) == (BASE, 11.5, 96.0)


def test_a_buffered_batch_is_stored(client, db_session, device):
    # Sent newest first, as a node flushing its buffer might: order does not matter.
    batch = [reading(device, hours=h) for h in (2, 0, 1)]

    response = post(client, batch)

    assert response.json()["stored"] == 3
    assert [row.timestamp for row in stored(db_session, device)] == [BASE + timedelta(hours=h) for h in (0, 1, 2)]


def test_last_seen_is_updated(client, db_session, device):
    before = datetime.now(UTC)
    post(client, reading(device))
    db_session.refresh(device)
    assert device.last_sync_time >= before
    assert device.created_at is not None


def test_readings_reach_the_same_table_as_the_node_endpoint(client, db_session, device):
    post(client, reading(device))
    # The same instant through POST /nodes/{id}/readings is a duplicate of it.
    response = client.post(f"/nodes/{device.node_id}/readings", headers={"X-Node-Key": DEVICE_KEY},
                           json={"readings": [{"timestamp": reading(device)["ts"],
                                               "temperature": 12.4, "humidity": 93.1}]})
    assert response.json()["duplicates"] == 1


# --- the key -------------------------------------------------------------------------


def test_a_wrong_key_is_refused_and_nothing_is_stored(client, db_session, device):
    response = post(client, reading(device), key="wrong-key")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid device credentials"
    assert stored(db_session, device) == []


def test_a_missing_key_is_refused(client, db_session, device):
    response = post(client, reading(device), key=None)

    assert response.status_code == 401
    assert "X-Device-Key" in response.json()["detail"]
    assert stored(db_session, device) == []


def test_an_unknown_device_gets_the_same_answer_as_a_wrong_key(client, device):
    response = post(client, reading(device, device_id="NODE-DOESNOTEXIST"))

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid device credentials"


def test_a_key_works_only_for_its_own_device(client, db_session, device):
    other = SensorNode(farmer_id=device.farmer_id, location="Second plot", latitude=-0.71, longitude=36.61,
                       api_key_hash=auth.hash_node_key("another-device-key-0123456789"))
    db_session.add(other)
    db_session.flush()

    assert post(client, reading(other)).status_code == 401  # device's key, other's readings


def test_one_request_carries_one_device(client, device):
    response = post(client, [reading(device), reading(device, hours=1, device_id="NODE-SOMEONEELSE")])

    assert response.status_code == 422
    assert "one device" in response.json()["detail"]


def test_an_empty_request_is_refused(client):
    assert post(client, []).status_code == 422


# --- validation ----------------------------------------------------------------------


@pytest.mark.parametrize("field, value", [
    ("temp_c", -40.1), ("temp_c", 85.1), ("rh", -0.1), ("rh", 100.1),
])
def test_out_of_range_values_are_rejected_and_the_rest_stored(client, db_session, device, field, value):
    batch = [reading(device, hours=0), reading(device, hours=1, **{field: value}), reading(device, hours=2)]

    body = post(client, batch).json()

    assert body["stored"] == 2  # one bad reading does not cost the node its buffer
    assert [r["index"] for r in body["rejected"]] == [1]
    assert field in body["rejected"][0]["reason"]
    assert len(stored(db_session, device)) == 2


@pytest.mark.parametrize("temp_c, rh", [(-40.0, 0.0), (85.0, 100.0)])
def test_the_limits_themselves_are_allowed(client, device, temp_c, rh):
    assert post(client, reading(device, temp_c=temp_c, rh=rh)).json()["stored"] == 1


def test_more_than_5_minutes_in_the_future_is_rejected(client, device):
    future = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()

    body = post(client, reading(device, ts=future)).json()

    assert body["stored"] == 0
    assert "future" in body["rejected"][0]["reason"]


def test_a_slightly_fast_clock_is_tolerated(client, device):
    soon = (datetime.now(UTC) + timedelta(minutes=4)).isoformat()
    assert post(client, reading(device, ts=soon)).json()["stored"] == 1


def test_a_timestamp_without_a_timezone_is_rejected(client, device):
    body = post(client, reading(device, ts="2026-10-01T03:00:00")).json()
    assert "timezone" in body["rejected"][0]["reason"]


def test_a_reading_without_a_device_id_is_rejected_on_its_own(client, device):
    no_id = reading(device, hours=1)
    del no_id["device_id"]

    body = post(client, [reading(device), no_id]).json()

    assert body["stored"] == 1
    assert body["rejected"][0]["index"] == 1


def test_unknown_fields_are_rejected_not_ignored(client, device):
    body = post(client, reading(device, temperature=12.0)).json()  # old field name by mistake
    assert body["stored"] == 0 and "temperature" in body["rejected"][0]["reason"]


# --- duplicates -------------------------------------------------------------------------


def test_a_resent_batch_is_skipped_not_an_error(client, db_session, device):
    batch = [reading(device, hours=h) for h in range(3)]
    post(client, batch)

    response = post(client, batch)  # the node never saw the first 200 and sends again

    assert response.status_code == 200
    assert response.json()["stored"] == 0
    assert response.json()["skipped"] == 3
    assert response.json()["rejected"] == []
    assert len(stored(db_session, device)) == 3


def test_a_repeat_inside_one_request_is_skipped(client, db_session, device):
    body = post(client, [reading(device), reading(device), reading(device, hours=1)]).json()
    assert (body["stored"], body["skipped"]) == (2, 1)


def test_an_overlapping_batch_stores_only_the_new_readings(client, db_session, device):
    post(client, [reading(device, hours=h) for h in range(2)])
    body = post(client, [reading(device, hours=h) for h in range(1, 4)]).json()
    assert (body["stored"], body["skipped"]) == (2, 1)


# --- the risk level in the reply ----------------------------------------------------------


def test_risk_is_unknown_without_enough_history(client, device):
    assert post(client, reading(device)).json()["risk_level"] == "unknown"


def test_risk_level_comes_from_the_risk_layer(client, db_session, device):
    today = datetime.now(hutton.LOCAL_TZ).date()
    db_session.add(RiskScore(node_id=device.node_id, method="hutton", data_source="sensor",
                             horizon_hours=0, for_date=today, status="ok", score=1.0, valid_hours=24,
                             hutton_day=True, hutton_period=True))
    db_session.flush()

    assert post(client, reading(device)).json()["risk_level"] == "high"


def test_an_unjudged_day_is_unknown_not_low(client, db_session, device):
    db_session.add(RiskScore(node_id=device.node_id, method="hutton", data_source="sensor",
                             horizon_hours=0, for_date=datetime.now(hutton.LOCAL_TZ).date(),
                             status="insufficient_data",
                             score=None, valid_hours=12))
    db_session.flush()

    assert post(client, reading(device)).json()["risk_level"] == "unknown"
