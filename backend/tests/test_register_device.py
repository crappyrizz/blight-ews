"""Tests for tools/register_device.py: a new device, its key, and refusing duplicates."""
import base64
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest

import backend.db
from backend import auth
from backend.config import settings
from backend.models import Farmer, SensorNode
from tools import register_device as rd

FARMER_PHONE = "+254712000077"


@pytest.fixture
def accounts(db_session):
    admin = Farmer(name="Admin", phone_number="+254712000066", password_hash="x", role="admin")
    farmer = Farmer(name="Farmer", phone_number=FARMER_PHONE, password_hash="x", role="farmer")
    db_session.add_all([admin, farmer])
    db_session.flush()
    return admin, farmer


@pytest.fixture
def cli_session(db_session, monkeypatch):
    """main() opens its own session; hand it the test session instead."""
    monkeypatch.setattr(backend.db, "SessionLocal", lambda: nullcontext(db_session))
    return db_session


def test_a_device_is_registered_with_only_the_hash_of_its_key(db_session, accounts):
    admin, _ = accounts
    device, key = rd.register_device(db_session, "node-01", "Bench node A")

    stored = db_session.get(SensorNode, "node-01")
    assert stored is device
    assert stored.location == "Bench node A"
    assert (stored.latitude, stored.longitude) == (settings.SITE_LAT, settings.SITE_LON)
    assert stored.farmer_id == admin.farmer_id  # bench devices stay off farmers' dashboards
    assert key not in stored.api_key_hash  # never the raw key
    assert auth.verify_node_key(key, stored.api_key_hash)
    assert stored.created_at is not None


def test_the_key_is_32_random_bytes(db_session, accounts):
    _, key1 = rd.register_device(db_session, "node-01", "A")
    _, key2 = rd.register_device(db_session, "node-02", "B")
    assert len(base64.urlsafe_b64decode(key1 + "=")) == 32
    assert key1 != key2


def test_a_duplicate_device_id_is_refused_and_nothing_changes(db_session, accounts):
    _, key = rd.register_device(db_session, "node-01", "Bench node A")
    original_hash = db_session.get(SensorNode, "node-01").api_key_hash

    with pytest.raises(rd.RegistrationError, match="already exists"):
        rd.register_device(db_session, "node-01", "Someone else")

    device = db_session.get(SensorNode, "node-01")
    assert (device.location, device.api_key_hash) == ("Bench node A", original_hash)
    assert auth.verify_node_key(key, device.api_key_hash)  # the first key still works


@pytest.mark.parametrize("device_id", ["", "node 01", "node/01", "-node", "x" * 33])
def test_a_malformed_device_id_is_refused(db_session, accounts, device_id):
    with pytest.raises(rd.RegistrationError, match="device_id"):
        rd.register_device(db_session, device_id, "Bench")


def test_an_owner_can_be_named_by_phone(db_session, accounts):
    _, farmer = accounts
    device, _ = rd.register_device(db_session, "node-03", "Plot node", owner_phone="0712000077")
    assert device.farmer_id == farmer.farmer_id
    with pytest.raises(rd.RegistrationError, match="no account"):
        rd.register_device(db_session, "node-04", "Plot node", owner_phone="0712999999")


def test_the_issued_key_works_on_post_readings(client, db_session, accounts):
    _, key = rd.register_device(db_session, "node-01", "Bench node A")
    ts = datetime.now(UTC).replace(microsecond=0).isoformat()

    response = client.post("/readings", headers={"X-Device-Key": key},
                           json={"device_id": "node-01", "ts": ts, "temp_c": 18.0, "rh": 70.0})

    assert response.status_code == 200
    assert response.json()["stored"] == 1


def test_the_command_prints_the_key_once_with_a_warning(cli_session, accounts, capsys, tmp_path):
    keys = tmp_path / "device_keys.local.env"

    assert rd.main(["node-01", "Bench node A", "--key-file", str(keys)]) == 0

    out = capsys.readouterr().out
    key = keys.read_text(encoding="utf-8").split("DEVICE_KEY_NODE_01=")[1].strip()
    assert out.count(key) == 1
    assert "shown once" in out and "cannot be shown again" in out
    assert auth.verify_node_key(key, cli_session.get(SensorNode, "node-01").api_key_hash)


def test_the_command_refuses_a_duplicate(cli_session, accounts, capsys, tmp_path):
    keys = tmp_path / "device_keys.local.env"
    rd.main(["node-01", "Bench node A", "--key-file", str(keys)])
    before = keys.read_text(encoding="utf-8")

    assert rd.main(["node-01", "Bench node A", "--key-file", str(keys)]) == 1

    assert "already exists" in capsys.readouterr().out
    assert keys.read_text(encoding="utf-8") == before  # no second key written


def test_keys_file_holds_one_variable_per_device(tmp_path):
    keys = tmp_path / "k.local.env"
    rd.append_key(keys, "node-01", "Bench node A", "aaa")
    rd.append_key(keys, "node-02", "Bench node B", "bbb")
    lines = [line for line in keys.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    assert lines == ["DEVICE_KEY_NODE_01=aaa", "DEVICE_KEY_NODE_02=bbb"]
