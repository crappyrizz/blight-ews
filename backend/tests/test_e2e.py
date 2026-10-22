"""End to end: from a wet week at the plot to the farmer's SMS, through the real API.

One test, read top to bottom. Every step uses the same code the system runs:

1. seed               tools/seed.py: an admin, the farmer and the sensor node (with its key)
2. wet week           tools/simulate_node.py replays 7 warm, humid days through POST /nodes/{id}/readings
3. risk job           backend/risk_job.py: Hutton scores the week, the plot's risk turns HIGH,
                      and the high-risk SMS goes out (dry run)
4. leaf photo         POST /images as the farmer: the CNN says late blight at 0.55
5. fusion             high risk lowers the bar to 0.40, so the leaf is flagged
                      (at neutral risk the bar is 0.60 and the same leaf would not be)
6. diagnosis SMS      queued for the flagged diagnosis (SMS_DRY_RUN: logged, not sent)
7. cooldown           the risk dips and turns high again within 24 h: no second
                      high-risk SMS; 25 h later, it is sent

Two stand-ins, because the files they replace are not in the repository (CI has
neither): the TFLite CNN is replaced by a fixed classifier with the same interface,
and the learned risk model is switched off, so fusion uses the sensor's Hutton score.
Both are tested with their real files elsewhere (test_cnn_service.py, test_risk_job.py).
"""
import csv
import io
import random
import re
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from backend import alerts, cnn_service, fusion, risk_job, risk_model, sms
from backend import app as app_module
from backend.app import app
from backend.config import settings
from backend.models import Alert, DiagnosisResult, SensorNode
from tools import seed, simulate_node
from tools.simulate_node import SimConfig, SimulatedNode

LOCAL = simulate_node.LOCAL_TZ
LABELS = ["early_blight", "healthy", "late_blight"]
LATE_BLIGHT_LEAF = (0.30, 0.15, 0.55)  # what the stand-in CNN says about the photo


# --- helpers ----------------------------------------------------------------------------


def write_wet_week(folder, first_day: date, last_day: date) -> None:
    """An archive CSV (UTC hours, as ml/fetch_weather.py writes them) of Hutton weather.

    Every night (18:00-06:00 local) is 12-13 °C at 96 % RH: 12 humid hours and a
    minimum above 10 °C, so every day is a Hutton day and from day 2 a Hutton period.
    """
    start = datetime.combine(first_day, time.min, tzinfo=LOCAL)
    end = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=LOCAL)
    with (folder / "archive_e2e.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "temperature_2m", "relative_humidity_2m"])
        moment = start
        while moment < end:
            night = moment.hour >= 18 or moment.hour < 6
            writer.writerow([moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"),
                             12.5 if night else 17.0, 96 if night else 78])
            moment += timedelta(hours=1)


class StandInCNN:
    """Same interface as cnn_service.CNNClassifier, with a fixed answer."""

    labels = LABELS

    def predict(self, model_input):
        probabilities = dict(zip(LABELS, LATE_BLIGHT_LEAF, strict=True))
        label = max(probabilities, key=probabilities.get)
        return cnn_service.Prediction(label=label, confidence=probabilities[label], probabilities=probabilities)


def no_learned_model():
    raise FileNotFoundError("learned model switched off for this test")


def leaf_photo() -> bytes:
    """A sharp, mid-brightness 320x320 JPEG that passes the server's quality check."""
    pixels = np.random.default_rng(0).integers(40, 220, (320, 320, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, format="JPEG")
    return buffer.getvalue()


def high_risk_sms(db_session, node_id):
    return db_session.scalars(select(Alert).where(Alert.node_id == node_id,
                                                  Alert.alert_type == alerts.ALERT_HIGH_RISK)).all()


# --- the test ------------------------------------------------------------------------------


def test_a_wet_week_ends_in_a_flagged_leaf_and_one_high_risk_sms(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SMS_DRY_RUN", True)  # never send a real SMS from a test
    monkeypatch.setattr(risk_model, "load_models", no_learned_model)
    app.dependency_overrides[cnn_service.get_classifier] = lambda: StandInCNN()
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "uploads")
    today = datetime.now(LOCAL).date()

    # 1. Seed: the farmer's password and the node key are printed once, as for a person.
    log = "\n".join(seed.seed(db_session))
    password = re.search(rf"phone={re.escape(seed.FARMER_PHONE)} password=(\S+)", log).group(1)
    node_id, node_key = re.search(r"X-Node-Key for (\S+): (\S+)", log).groups()

    # 2. The node replays a wet week (the 7 days before today) through the ingest API.
    first_day, last_day = today - timedelta(days=7), today - timedelta(days=1)
    write_wet_week(tmp_path, first_day, last_day)
    config = SimConfig(interval_minutes=60, flush_every_minutes=24 * 60, offline_probability=0.0)
    rng = random.Random(42)
    readings = simulate_node.build_readings(first_day, last_day, config, rng, weather_dir=tmp_path)
    node = SimulatedNode(client, node_id, node_key, config, rng)
    sent = simulate_node.run_replay(node, readings)
    assert len(readings) == 7 * 24
    assert (sent.accepted, sent.rejected) == (len(readings), 0)

    # 3. The hourly risk job scores the plot. Today has no readings yet, so the risk
    #    is the latest judged day (yesterday, a Hutton period): HIGH, and the farmer
    #    gets the high-risk SMS.
    summary = risk_job.run_once(db_session, today)
    sensor = next(s for s in summary if s["data_source"] == "sensor")
    assert sensor["hutton"][str(last_day)] == "ok"
    risk = fusion.resolve_risk(db_session, node_id, today)
    assert (risk.basis, risk.score, fusion.risk_level(risk.score)) == (fusion.BASIS_FALLBACK, 1.0, "high")
    assert summary[-1]["high_risk_sms"] == sms.STATUS_DRY_RUN  # reported once per node, on its last row
    assert len(high_risk_sms(db_session, node_id)) == 1

    # 4. The farmer logs in and uploads a leaf photo.
    login = client.post("/auth/login", json={"phone_number": seed.FARMER_PHONE, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.post("/images", headers=headers, files={"file": ("leaf.jpg", leaf_photo(), "image/jpeg")})
    assert response.status_code == 201
    body = response.json()
    assert body["prediction"]["label"] == "late_blight"
    assert body["prediction"]["confidence"] == pytest.approx(0.55)

    # 5. Fusion: r = 1.0 lowers the late-blight bar to 0.40, so 0.55 is flagged.
    #    At neutral risk (r = 0.5) the bar is 0.60 and this leaf would NOT be flagged.
    diagnosis = body["diagnosis"]
    assert diagnosis["risk_basis"] == fusion.BASIS_FALLBACK
    assert diagnosis["threshold_used"] == pytest.approx(0.40)
    assert diagnosis["late_blight_flagged"] is True
    assert fusion.threshold_for(0.5) > 0.55
    assert diagnosis["contributing_readings"] > 0  # the 48 h of readings behind it are linked
    stored = db_session.get(DiagnosisResult, diagnosis["result_id"])
    assert stored.late_blight_flagged and stored.risk_level == "high"

    # 6. The diagnosis SMS is queued: in dry run it is logged and recorded, not sent.
    assert diagnosis["sms_alert"] == sms.STATUS_DRY_RUN
    diagnosis_sms = db_session.scalars(select(Alert).where(Alert.result_id == diagnosis["result_id"])).one()
    assert diagnosis_sms.alert_type == alerts.ALERT_DIAGNOSIS
    assert "late blight" in diagnosis_sms.message.lower()

    # 7. Cooldown. The risk job runs again: still high, so no new SMS (not a change).
    risk_job.run_once(db_session, today)
    assert len(high_risk_sms(db_session, node_id)) == 1
    #    Then the risk dips (a dry spell) and turns high again within 24 h:
    #    a change to high, but inside the cooldown, so still no second SMS.
    plot = db_session.get(SensorNode, node_id)
    plot.last_risk_level = "medium"
    risk_job.run_once(db_session, today)
    assert len(high_risk_sms(db_session, node_id)) == 1
    assert plot.last_risk_level == "high"  # the level is tracked even when no SMS goes
    #    25 hours after the first one, the same change sends again.
    plot.last_risk_level = "medium"
    later = datetime.now(UTC) + timedelta(hours=25)
    assert alerts.check_high_risk(db_session, plot, today, now=later) is not None
    assert len(high_risk_sms(db_session, node_id)) == 2
