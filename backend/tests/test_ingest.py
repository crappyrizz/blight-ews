"""Tests for sensor ingest: the contract the ESP32 firmware will follow."""
from datetime import datetime, timedelta, timezone

import pytest

from backend import auth
from backend.models import Farmer, SensorNode, SensorReading
from backend.schemas import MAX_BATCH_SIZE

# Relative to now, so a 500-reading batch (about 21 days of hourly data)
# stays in the past however long after today the tests are run.
BASE = (datetime.now(timezone.utc) - timedelta(days=30)).replace(
    minute=0, second=0, microsecond=0
)
NODE_KEY = "test-node-key-abcdefghijklmnop"


@pytest.fixture
def node(db_session):
    """A farmer with one node that has an API key."""
    farmer = Farmer(
        name="Kinangop Farmer", phone_number="+254712000001",
        password_hash=auth.hash_password("shangi-2026"), role="farmer",
    )
    db_session.add(farmer)
    db_session.flush()
    node = SensorNode(
        farmer_id=farmer.farmer_id, location="Kinangop plot",
        latitude=-0.7, longitude=36.6,
        api_key_hash=auth.hash_node_key(NODE_KEY),
    )
    db_session.add(node)
    db_session.flush()
    return node


def key_header(key=NODE_KEY):
    return {"X-Node-Key": key}


def reading(hours_from_base=0, temperature=12.4, humidity=93.1, leaf_wetness=None):
    return {
        "timestamp": (BASE + timedelta(hours=hours_from_base)).isoformat().replace("+00:00", "Z"),
        "temperature": temperature,
        "humidity": humidity,
        "leaf_wetness": leaf_wetness,
    }


def post(client, node, readings, key=NODE_KEY):
    return client.post(
        f"/nodes/{node.node_id}/readings",
        json={"readings": readings},
        headers=key_header(key),
    )


def stored_timestamps(session, node):
    rows = session.query(SensorReading).filter_by(node_id=node.node_id).all()
    return sorted(r.timestamp for r in rows)


# --- happy path --------------------------------------------------------


def test_batch_is_accepted(client, db_session, node):
    response = post(client, node, [reading(0), reading(1), reading(2)])

    assert response.status_code == 200
    assert response.json() == {"accepted": 3, "duplicates": 0, "rejected": []}
    assert len(stored_timestamps(db_session, node)) == 3


def test_leaf_wetness_may_be_null_or_a_number(client, db_session, node):
    post(client, node, [reading(0), reading(1, leaf_wetness=0.42)])

    rows = db_session.query(SensorReading).filter_by(node_id=node.node_id).all()
    assert sorted(r.leaf_wetness is None for r in rows) == [False, True]


def test_out_of_order_timestamps_are_accepted(client, db_session, node):
    response = post(client, node, [reading(5), reading(1), reading(3)])

    assert response.json()["accepted"] == 3
    # Stored order does not depend on the order they arrived in.
    assert stored_timestamps(db_session, node) == [
        BASE + timedelta(hours=h) for h in (1, 3, 5)
    ]


def test_old_buffered_readings_are_accepted(client, node):
    """The node may be offline for days; its backlog is still valid."""
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat().replace("+00:00", "Z")

    response = post(client, node, [{"timestamp": old, "temperature": 11.0, "humidity": 91.0}])

    assert response.json()["accepted"] == 1


def test_sync_time_is_updated(client, db_session, node):
    assert node.last_sync_time is None

    post(client, node, [reading(0)])
    db_session.refresh(node)

    assert node.last_sync_time is not None
    # "Last heard from", not the newest reading's timestamp.
    assert node.last_sync_time > BASE


def test_empty_batch_is_accepted_and_changes_nothing(client, db_session, node):
    response = post(client, node, [])

    assert response.json() == {"accepted": 0, "duplicates": 0, "rejected": []}


# --- replay and duplicates --------------------------------------------


def test_replayed_batch_adds_no_rows(client, db_session, node):
    batch = [reading(0), reading(1), reading(2)]
    post(client, node, batch)

    # The node never saw our first response, so it sends the batch again.
    response = post(client, node, batch)

    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 3, "rejected": []}
    assert len(stored_timestamps(db_session, node)) == 3


def test_overlapping_batch_stores_only_the_new_readings(client, db_session, node):
    post(client, node, [reading(0), reading(1)])

    response = post(client, node, [reading(1), reading(2)])  # one old, one new

    assert response.json()["accepted"] == 1
    assert response.json()["duplicates"] == 1
    assert len(stored_timestamps(db_session, node)) == 3


def test_duplicate_inside_one_batch_is_counted_once(client, db_session, node):
    response = post(client, node, [reading(0), reading(0), reading(1)])

    assert response.json()["accepted"] == 2
    assert response.json()["duplicates"] == 1


def test_same_timestamp_from_another_node_is_not_a_duplicate(client, db_session, node):
    post(client, node, [reading(0)])

    other_key = "another-node-key-1234567890"
    other = SensorNode(
        farmer_id=node.farmer_id, location="Second plot", latitude=-0.71, longitude=36.61,
        api_key_hash=auth.hash_node_key(other_key),
    )
    db_session.add(other)
    db_session.flush()

    response = post(client, other, [reading(0)], key=other_key)

    assert response.json()["accepted"] == 1


# --- per-row validation ------------------------------------------------


def test_invalid_rows_are_rejected_individually(client, db_session, node):
    response = post(client, node, [
        reading(0),  # ok
        reading(1, temperature=60.0),  # too hot
        reading(2, humidity=150.0),  # impossible humidity
        {"timestamp": reading(3)["timestamp"], "humidity": 90.0},  # no temperature
        reading(4),  # ok
    ])

    body = response.json()
    assert response.status_code == 200  # the good rows still landed
    assert body["accepted"] == 2
    assert [r["index"] for r in body["rejected"]] == [1, 2, 3]
    assert "temperature" in body["rejected"][0]["reason"]
    assert "humidity" in body["rejected"][1]["reason"]
    assert len(stored_timestamps(db_session, node)) == 2


@pytest.mark.parametrize("temperature", [-10.1, 50.1])
def test_temperature_outside_the_allowed_range_is_rejected(client, node, temperature):
    response = post(client, node, [reading(0, temperature=temperature)])

    assert response.json()["accepted"] == 0
    assert "temperature" in response.json()["rejected"][0]["reason"]


@pytest.mark.parametrize("temperature", [-10.0, 50.0])
def test_range_limits_themselves_are_allowed(client, node, temperature):
    assert post(client, node, [reading(0, temperature=temperature)]).json()["accepted"] == 1


def test_future_timestamp_is_rejected(client, node):
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")

    response = post(client, node, [{"timestamp": future, "temperature": 12.0, "humidity": 90.0}])

    assert response.json()["accepted"] == 0
    assert "future" in response.json()["rejected"][0]["reason"]


def test_small_clock_drift_is_tolerated(client, node):
    """Up to 10 minutes ahead is allowed, so a slightly fast node still works."""
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

    response = post(client, node, [{"timestamp": soon, "temperature": 12.0, "humidity": 90.0}])

    assert response.json()["accepted"] == 1


def test_timestamp_without_a_timezone_is_rejected(client, node):
    response = post(client, node, [
        {"timestamp": "2026-09-22T03:00:00", "temperature": 12.0, "humidity": 90.0},
    ])

    assert response.json()["accepted"] == 0
    assert "timezone" in response.json()["rejected"][0]["reason"]


def test_unknown_field_is_rejected_rather_than_ignored(client, node):
    row = reading(0)
    row["temprature"] = 12.0  # typo in the firmware

    response = post(client, node, [row])

    assert response.json()["accepted"] == 0
    assert "temprature" in response.json()["rejected"][0]["reason"]


def test_non_object_row_is_rejected(client, node):
    response = post(client, node, ["not-a-reading"])

    assert response.json()["rejected"][0]["reason"] == "reading must be an object"


# --- limits ------------------------------------------------------------


def test_oversize_batch_is_refused(client, db_session, node):
    readings = [reading(h) for h in range(MAX_BATCH_SIZE + 1)]

    response = post(client, node, readings)

    assert response.status_code == 413
    assert str(MAX_BATCH_SIZE) in response.json()["detail"]
    assert stored_timestamps(db_session, node) == []  # nothing was stored


def test_a_full_size_batch_is_allowed(client, node):
    readings = [reading(h) for h in range(MAX_BATCH_SIZE)]

    response = post(client, node, readings)

    assert response.status_code == 200
    assert response.json()["accepted"] == MAX_BATCH_SIZE


# --- node authentication ----------------------------------------------


def test_wrong_node_key_is_rejected(client, db_session, node):
    response = post(client, node, [reading(0)], key="wrong-key")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid node credentials"
    assert stored_timestamps(db_session, node) == []


def test_missing_node_key_is_rejected(client, node):
    response = client.post(
        f"/nodes/{node.node_id}/readings", json={"readings": [reading(0)]}
    )

    assert response.status_code == 401
    assert "X-Node-Key" in response.json()["detail"]


def test_unknown_node_gives_the_same_error_as_a_wrong_key(client):
    response = client.post(
        "/nodes/NODE-DOESNOTEXIST/readings",
        json={"readings": [reading(0)]},
        headers=key_header(),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid node credentials"


def test_a_farmer_jwt_does_not_work_for_ingest(client, db_session, node):
    """Nodes use node keys only: a stolen phone must not be able to fake readings."""
    token = auth.create_access_token(node.farmer_id)

    response = client.post(
        f"/nodes/{node.node_id}/readings",
        json={"readings": [reading(0)]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_a_node_key_does_not_work_for_another_node(client, db_session, node):
    other = SensorNode(
        farmer_id=node.farmer_id, location="Second plot", latitude=-0.71, longitude=36.61,
        api_key_hash=auth.hash_node_key("a-different-key-000000000"),
    )
    db_session.add(other)
    db_session.flush()

    response = post(client, other, [reading(0)], key=NODE_KEY)  # first node's key

    assert response.status_code == 401


# --- reading back (farmer JWT) ----------------------------------------


def farmer_token(node):
    return {"Authorization": f"Bearer {auth.create_access_token(node.farmer_id)}"}


def test_farmer_can_read_their_nodes_readings_in_time_order(client, node):
    post(client, node, [reading(5), reading(1), reading(3)])

    response = client.get(f"/nodes/{node.node_id}/readings", headers=farmer_token(node))

    assert response.status_code == 200
    stamps = [r["timestamp"] for r in response.json()]
    assert stamps == sorted(stamps)
    assert len(stamps) == 3


def test_readings_can_be_filtered_by_time_range(client, node):
    post(client, node, [reading(h) for h in range(6)])

    response = client.get(
        f"/nodes/{node.node_id}/readings",
        params={
            "from": (BASE + timedelta(hours=2)).isoformat(),
            "to": (BASE + timedelta(hours=4)).isoformat(),
        },
        headers=farmer_token(node),
    )

    # 'from' inclusive, 'to' exclusive: hours 2 and 3.
    assert len(response.json()) == 2


def test_reading_back_requires_a_token(client, node):
    assert client.get(f"/nodes/{node.node_id}/readings").status_code == 401


def test_farmer_cannot_read_another_farmers_readings(client, db_session, node):
    other_farmer = Farmer(
        name="Farmer B", phone_number="+254712000002",
        password_hash=auth.hash_password("x"), role="farmer",
    )
    db_session.add(other_farmer)
    db_session.flush()
    token = {"Authorization": f"Bearer {auth.create_access_token(other_farmer.farmer_id)}"}

    response = client.get(f"/nodes/{node.node_id}/readings", headers=token)

    assert response.status_code == 404
