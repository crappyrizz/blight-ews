"""Tests for tools/seed_demo_event.py and the admin re-score endpoint.

The learned model is switched off here (CI has no model files), so the risk
comes from the Hutton chain; with the real model the seeded history also
gives a high 24 h forecast (checked by hand: p24 0.92).
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from backend import alerts, auth, fusion, hutton, risk_job, risk_model, sms
from backend.config import settings
from backend.models import Alert, Farmer, SensorNode, SensorReading
from tools import seed_demo_event as seed

PASSWORD = "correct-horse-battery"


def no_learned_model():
    raise FileNotFoundError("learned model switched off for this test")


@pytest.fixture(autouse=True)
def hutton_only(monkeypatch):
    monkeypatch.setattr(risk_model, "load_models", no_learned_model)
    monkeypatch.setattr(settings, "SMS_DRY_RUN", True)


@pytest.fixture
def farmer(db_session):
    farmer = Farmer(name="Farmer", phone_number="+254712000088", password_hash=auth.hash_password(PASSWORD),
                    role="farmer")
    db_session.add(farmer)
    db_session.flush()
    return farmer


def device(db_session, farmer, node_id):
    node = SensorNode(node_id=node_id, farmer_id=farmer.farmer_id, location=node_id,
                      latitude=-0.7, longitude=36.6, api_key_hash="x")
    db_session.add(node)
    db_session.flush()
    return node


def high_risk_alerts(db_session, node_id):
    return db_session.scalars(select(Alert).where(Alert.node_id == node_id,
                                                  Alert.alert_type == alerts.ALERT_HIGH_RISK)).all()


# --- the history ---------------------------------------------------------------------


def test_history_is_hourly_and_ends_in_hutton_days():
    end = risk_job.local_today() - timedelta(days=1)
    readings = seed.hourly_history(end, days=16, wet_days=2)
    assert len(readings) == 16 * 24
    hourly = hutton.to_hourly(pd.DataFrame([r.model_dump() for r in readings]))
    daily = hutton.hutton_periods(hutton.hutton_days(hutton.daily_summary(hourly)))
    assert list(daily["hutton_day"].iloc[-3:]) == [False, True, True]
    assert daily["hutton_period"].iloc[-1] is True  # the last day completes a Hutton period
    assert set(daily["status"]) == {hutton.STATUS_OK}  # every day fully judged


def test_history_is_longer_than_the_learned_models_look_back():
    assert seed.DEFAULT_DAYS > risk_model.HISTORY_DAYS


def test_more_wet_days_than_days_is_refused():
    with pytest.raises(seed.DemoSeedError):
        seed.hourly_history(risk_job.local_today(), days=2, wet_days=3)


# --- seeding a device -----------------------------------------------------------------


def test_only_demo_devices_can_be_seeded(db_session, farmer):
    device(db_session, farmer, "node-01")
    with pytest.raises(seed.DemoSeedError, match="only demo devices"):
        seed.seed_demo_event(db_session, "node-01")
    assert db_session.scalars(select(SensorReading)).all() == []


def test_a_missing_device_is_reported(db_session):
    with pytest.raises(seed.DemoSeedError, match="does not exist"):
        seed.seed_demo_event(db_session, "demo-99")


def test_seeding_makes_the_risk_high_and_the_sms_fire(db_session, farmer):
    node = device(db_session, farmer, "demo-01")
    result = seed.seed_demo_event(db_session, "demo-01")
    assert result["stored"] == seed.DEFAULT_DAYS * 24

    summary = risk_job.score_node(db_session, node, risk_job.local_today(), learned_available=False)

    risk = fusion.resolve_risk(db_session, "demo-01", risk_job.local_today())
    assert fusion.risk_level(risk.score) == "high"
    assert summary[-1]["high_risk_sms"] == sms.STATUS_DRY_RUN
    assert len(high_risk_alerts(db_session, "demo-01")) == 1


def test_reseeding_after_a_rehearsal_lets_the_alert_fire_again(db_session, farmer):
    node = device(db_session, farmer, "demo-01")
    seed.seed_demo_event(db_session, "demo-01")
    risk_job.score_node(db_session, node, risk_job.local_today(), learned_available=False)  # rehearsal
    assert node.last_risk_level == "high"

    result = seed.seed_demo_event(db_session, "demo-01")  # the night before the demo
    assert result["removed"]["alerts"] == 1 and result["removed"]["readings"] == seed.DEFAULT_DAYS * 24
    assert node.last_risk_level is None

    summary = risk_job.score_node(db_session, node, risk_job.local_today(), learned_available=False)
    assert summary[-1]["high_risk_sms"] == sms.STATUS_DRY_RUN  # not blocked by the cooldown


def test_other_devices_are_untouched(db_session, farmer):
    device(db_session, farmer, "demo-01")
    other = device(db_session, farmer, "node-02")
    db_session.add(SensorReading(node_id=other.node_id, timestamp=datetime.now(hutton.LOCAL_TZ),
                                 temperature=15.0, humidity=70.0))
    db_session.flush()
    seed.seed_demo_event(db_session, "demo-01")
    seed.seed_demo_event(db_session, "demo-01")
    assert len(db_session.scalars(select(SensorReading).where(SensorReading.node_id == "node-02")).all()) == 1


# --- POST /admin/nodes/{node_id}/rescore ------------------------------------------------


def token(client, phone):
    response = client.post("/auth/login", json={"phone_number": phone, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def admin(db_session):
    admin = Farmer(name="Admin", phone_number="+254712000099", password_hash=auth.hash_password(PASSWORD),
                   role="admin")
    db_session.add(admin)
    db_session.flush()
    return admin


def test_rescore_scores_the_node_now_and_fires_the_alert(client, db_session, farmer, admin):
    device(db_session, farmer, "demo-01")
    seed.seed_demo_event(db_session, "demo-01")

    response = client.post("/admin/nodes/demo-01/rescore", headers=token(client, admin.phone_number))

    assert response.status_code == 200
    body = response.json()
    assert body["risk_level"] == "high"
    assert body["risk_basis"] == fusion.BASIS_FALLBACK
    assert body["high_risk_sms"] == sms.STATUS_DRY_RUN
    assert len(high_risk_alerts(db_session, "demo-01")) == 1


def test_rescore_is_admin_only(client, db_session, farmer):
    device(db_session, farmer, "demo-01")
    assert client.post("/admin/nodes/demo-01/rescore", headers=token(client, farmer.phone_number)).status_code == 403
    assert client.post("/admin/nodes/demo-01/rescore").status_code == 401


def test_rescore_of_an_unknown_node_is_404(client, admin):
    assert client.post("/admin/nodes/demo-77/rescore", headers=token(client, admin.phone_number)).status_code == 404
