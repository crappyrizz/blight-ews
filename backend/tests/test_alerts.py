"""Tests for SMS alerting (backend/alerts.py, backend/sms.py) with the Africa's
Talking SDK mocked: no message ever leaves the machine."""
from datetime import UTC, date, datetime, timedelta

import pytest

from backend import alerts, hutton, risk_job, sms
from backend.config import settings
from backend.models import Alert, DiagnosisResult, Farmer, LeafImage, RiskScore, SensorNode

PASSWORD = "correct-horse-battery"
NOW = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
TODAY = date(2026, 3, 10)


class FakeATSMS:
    """Stands in for africastalking.SMS: records calls, answers like the real API."""

    def __init__(self, status_code=101, status="Success", fail=None):
        self.calls = []
        self.status_code, self.status, self.fail = status_code, status, fail

    def send(self, message, recipients, sender_id=None, enqueue=False):
        self.calls.append({"message": message, "recipients": recipients, "sender_id": sender_id})
        if self.fail:
            raise self.fail
        return {"SMSMessageData": {"Message": "Sent to 1/1 Total Cost: KES 0.8000", "Recipients": [{
            "statusCode": self.status_code, "number": recipients[0], "status": self.status,
            "cost": "KES 0.8000", "messageId": f"ATXid_test{len(self.calls)}"}]}}


@pytest.fixture
def at(monkeypatch):
    """Real sending path, with the SDK replaced by FakeATSMS."""
    fake = FakeATSMS()
    monkeypatch.setattr(settings, "SMS_DRY_RUN", False)
    monkeypatch.setattr(sms, "_service", fake)
    return fake


def make_node(session, phone="+254722000111", farm_id="FARM-7", location="Kinangop plot"):
    farmer = Farmer(name="F", phone_number=phone, password_hash="x", role="farmer", farm_id=farm_id)
    session.add(farmer)
    session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location=location, latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def set_risk(session, node, score, day=TODAY):
    """A judged Hutton score for the day (what fusion.resolve_risk falls back to)."""
    row = session.query(RiskScore).filter_by(node_id=node.node_id, method="hutton", data_source="sensor",
                                             horizon_hours=0, for_date=day).one_or_none()
    if row is None:
        row = RiskScore(node_id=node.node_id, method="hutton", data_source="sensor", horizon_hours=0,
                        for_date=day, valid_hours=24, status="ok")
        session.add(row)
    row.score = score
    session.flush()


def alerts_of(session, node, alert_type=None):
    query = session.query(Alert).filter_by(node_id=node.node_id)
    if alert_type:
        query = query.filter_by(alert_type=alert_type)
    return query.order_by(Alert.sent_at).all()


# --- templates -----------------------------------------------------------------------


LONGEST_PLOT = alerts.plot_name("A very long plot name near the Kinangop forest edge, block C", "FARM-ABCDEFGHIJ")


@pytest.mark.parametrize("message", [
    alerts.high_risk_message(LONGEST_PLOT),
    alerts.low_risk_message(LONGEST_PLOT),
    alerts.diagnosis_message("early_blight", 1.0, "medium"),
    alerts.diagnosis_message("late_blight", 0.999, "high"),
    alerts.diagnosis_message("healthy", 0.5, "low"),
])
def test_templates_fit_one_gsm_sms(message):
    assert len(message) <= alerts.SMS_MAX_CHARS
    assert alerts.gsm_safe(message) == message  # no character would force Unicode (70-char SMS)


def test_templates_are_decision_support():
    texts = [alerts.high_risk_message("Kinangop plot (FARM-7)"),
             alerts.diagnosis_message("late_blight", 0.82, "high"),
             alerts.low_risk_message("Kinangop plot (FARM-7)")]
    assert "consider checking your crop" in texts[0].lower()
    assert "Kinangop plot (FARM-7)" in texts[0]
    assert "late blight (82%)" in texts[1] and "HIGH" in texts[1]
    assert "No action suggested" in texts[2]
    for text in texts:
        assert "spray" not in text.lower()


def test_plot_names_are_cleaned_and_cut():
    assert alerts.plot_name("Kinangop – plot ’A’", "FARM-1") == "Kinangop - plot 'A' (FARM-1)"
    assert len(LONGEST_PLOT) <= alerts.PLOT_MAX_CHARS
    assert LONGEST_PLOT.endswith("(FARM-ABCDEFGHIJ)")  # the farm id survives the cut
    assert "  " not in LONGEST_PLOT


# --- the high-risk policy (pure) ---------------------------------------------------------


@pytest.mark.parametrize("previous, current, send", [
    ("medium", "high", True), ("low", "high", True), (None, "high", True),
    ("high", "high", False), ("high", "medium", False), ("medium", "low", False),
])
def test_high_risk_only_on_a_change_to_high(previous, current, send):
    assert alerts.should_send_high_risk(previous, current, None, NOW)[0] is send


def test_cooldown_blocks_a_second_high_risk_within_24h():
    assert alerts.should_send_high_risk("medium", "high", NOW - timedelta(hours=23, minutes=59), NOW)[0] is False
    assert alerts.should_send_high_risk("medium", "high", NOW - timedelta(hours=24), NOW)[0] is True


# --- the high-risk policy against the database --------------------------------------------


def test_medium_to_high_sends_and_records_the_alert(db_session, at):
    node = make_node(db_session)
    node.last_risk_level = "medium"
    set_risk(db_session, node, 1.0)

    alert = alerts.check_high_risk(db_session, node, TODAY, NOW)

    assert alert.alert_type == "high_risk"
    assert alert.provider_message_id == "ATXid_test1"
    assert alert.delivery_status == sms.STATUS_SUBMITTED
    assert alert.risk_score_id is not None  # the score that triggered it
    assert at.calls[0]["recipients"] == ["+254722000111"]
    assert node.last_risk_level == "high"


def test_high_to_high_sends_nothing(db_session, at):
    node = make_node(db_session)
    set_risk(db_session, node, 1.0)
    alerts.check_high_risk(db_session, node, TODAY, NOW)  # first: none -> high, sends
    assert alerts.check_high_risk(db_session, node, TODAY, NOW + timedelta(hours=1)) is None
    assert len(at.calls) == 1


def test_flicker_back_to_high_within_24h_is_blocked_by_the_cooldown(db_session, at):
    node = make_node(db_session)
    set_risk(db_session, node, 1.0)
    first = alerts.check_high_risk(db_session, node, TODAY, NOW)
    first.sent_at = NOW  # the database default is the real clock; pin it for the test
    set_risk(db_session, node, 0.5)
    alerts.check_high_risk(db_session, node, TODAY, NOW + timedelta(hours=2))  # -> medium
    set_risk(db_session, node, 1.0)

    assert alerts.check_high_risk(db_session, node, TODAY, NOW + timedelta(hours=5)) is None
    assert len(alerts_of(db_session, node, "high_risk")) == 1
    assert node.last_risk_level == "high"  # the level is still tracked


def test_unknown_risk_changes_nothing(db_session, at):
    node = make_node(db_session)
    node.last_risk_level = "medium"  # no risk rows at all: neutral prior

    assert alerts.check_high_risk(db_session, node, TODAY, NOW) is None
    assert node.last_risk_level == "medium"
    assert at.calls == []


def test_the_risk_job_runs_the_high_risk_policy(db_session, at, tmp_path, monkeypatch):
    from backend import risk_model

    monkeypatch.setattr(risk_model, "MODEL_PATHS", {24: tmp_path / "none.joblib"})  # Hutton only
    monkeypatch.setattr(risk_model, "_bundles", {})
    node = make_node(db_session)
    node.last_risk_level = "low"
    # A judged Hutton period the day before, so today's risk falls back to it.
    set_risk(db_session, node, 1.0, day=TODAY - timedelta(days=1))
    monkeypatch.setattr(hutton, "compute_hutton_for_node", lambda *a, **k: _empty_daily())

    summary = risk_job.run_once(db_session, TODAY)

    assert summary[-1]["high_risk_sms"] == sms.STATUS_SUBMITTED
    assert len(alerts_of(db_session, node, "high_risk")) == 1


def _empty_daily():
    import pandas as pd

    return pd.DataFrame({"status": []})


# --- diagnosis alerts ---------------------------------------------------------------------


def make_diagnosis(session, node, flagged=True):
    image = LeafImage(farmer_id=node.farmer_id, file_path="x.jpg", capture_date=NOW)
    session.add(image)
    session.flush()
    diagnosis = DiagnosisResult(image_id=image.image_id, disease_label="late_blight", confidence=0.82,
                                probabilities={"late_blight": 0.82}, risk_score_used=0.9, threshold_used=0.44,
                                risk_level="high", late_blight_flagged=flagged, risk_basis="learned_24h")
    session.add(diagnosis)
    session.flush()
    return diagnosis


def test_flagged_diagnosis_sends_one_sms(db_session, at):
    node = make_node(db_session)
    diagnosis = make_diagnosis(db_session, node)

    alert = alerts.send_diagnosis_alert(db_session, node, diagnosis)

    assert alert.alert_type == "diagnosis" and alert.result_id == diagnosis.result_id
    assert "late blight (82%)" in alert.message
    assert alerts.send_diagnosis_alert(db_session, node, diagnosis) is None  # never a duplicate
    assert len(at.calls) == 1


def test_unflagged_diagnosis_sends_nothing(db_session, at):
    node = make_node(db_session)
    assert alerts.send_diagnosis_alert(db_session, node, make_diagnosis(db_session, node, flagged=False)) is None
    assert at.calls == []


# --- sms.send ---------------------------------------------------------------------------


def test_dry_run_logs_instead_of_sending(monkeypatch):
    fake = FakeATSMS()
    monkeypatch.setattr(settings, "SMS_DRY_RUN", True)
    monkeypatch.setattr(sms, "_service", fake)

    result = sms.send("+254722000111", "hello")

    assert result.status == sms.STATUS_DRY_RUN and result.message_id is None
    assert fake.calls == []


def test_a_rejected_number_is_recorded_as_rejected(monkeypatch):
    monkeypatch.setattr(settings, "SMS_DRY_RUN", False)
    monkeypatch.setattr(sms, "_service", FakeATSMS(status_code=403, status="InvalidPhoneNumber"))
    assert sms.send("+254722000111", "hello").status == "InvalidPhoneNumber"


def test_a_network_failure_is_recorded_not_raised(monkeypatch):
    monkeypatch.setattr(settings, "SMS_DRY_RUN", False)
    monkeypatch.setattr(sms, "_service", FakeATSMS(fail=ConnectionError("no route")))
    assert sms.send("+254722000111", "hello").status == sms.STATUS_ERROR


# --- endpoints ---------------------------------------------------------------------------


def login(client, phone="+254722000999"):
    client.post("/auth/register", json={"name": "SMS Farmer", "phone_number": phone,
                                        "password": PASSWORD, "farm_id": "FARM-9"})
    token = client.post("/auth/login", json={"phone_number": phone, "password": PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/me", headers=headers).json()["farmer_id"]


def node_for(session, farmer_id):
    node = SensorNode(farmer_id=farmer_id, location="Kinangop plot", latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def local_today():
    return datetime.now(hutton.LOCAL_TZ).date()


def test_status_sms_when_risk_is_low(client, db_session, at):
    headers, farmer_id = login(client)
    node = node_for(db_session, farmer_id)
    set_risk(db_session, node, 0.0, day=local_today())

    response = client.post(f"/nodes/{node.node_id}/status-sms", headers=headers)

    assert response.status_code == 201
    body = response.json()
    assert body["alert_type"] == "low_risk"
    assert "No action suggested" in body["message"] and "(FARM-9)" in body["message"]
    assert body["provider_message_id"] == "ATXid_test1"
    # Asking again within the hour is refused: no duplicates.
    again = client.post(f"/nodes/{node.node_id}/status-sms", headers=headers)
    assert again.status_code == 409 and again.json()["detail"]["reason"] == "recently_sent"


@pytest.mark.parametrize("score, reason", [(1.0, "risk_high"), (0.5, "risk_medium"), (None, "unknown")])
def test_status_sms_refused_unless_risk_is_judged_low(client, db_session, at, score, reason):
    headers, farmer_id = login(client)
    node = node_for(db_session, farmer_id)
    if score is not None:
        set_risk(db_session, node, score, day=local_today())

    response = client.post(f"/nodes/{node.node_id}/status-sms", headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == reason
    assert at.calls == []


def test_status_sms_for_another_farmers_node_is_404(client, db_session, at):
    _, owner = login(client)
    node = node_for(db_session, owner)
    stranger, _ = login(client, "+254722000888")
    assert client.post(f"/nodes/{node.node_id}/status-sms", headers=stranger).status_code == 404


def test_delivery_report_updates_the_status(client, db_session, at):
    node = make_node(db_session)
    set_risk(db_session, node, 1.0)
    alert = alerts.check_high_risk(db_session, node, TODAY, NOW)

    response = client.post("/sms/delivery-report", data={
        "id": alert.provider_message_id, "status": "Success", "phoneNumber": "+254722000111",
        "networkCode": "63902", "retryCount": "0"})

    assert response.status_code == 200 and response.json() == {"updated": True}
    db_session.refresh(alert)
    assert alert.delivery_status == "Success"


def test_delivery_report_keeps_the_failure_reason(client, db_session, at):
    node = make_node(db_session)
    set_risk(db_session, node, 1.0)
    alert = alerts.check_high_risk(db_session, node, TODAY, NOW)

    client.post("/sms/delivery-report", data={"id": alert.provider_message_id, "status": "Failed",
                                              "failureReason": "UserInBlackList"})

    db_session.refresh(alert)
    assert alert.delivery_status == "Failed: UserInBlackList"


def test_delivery_report_for_an_unknown_id_is_accepted_but_changes_nothing(client):
    response = client.post("/sms/delivery-report", data={"id": "ATXid_nope", "status": "Success"})
    assert response.status_code == 200 and response.json() == {"updated": False}


def test_delivery_report_token_is_checked_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "SMS_CALLBACK_TOKEN", "s3cret")
    form = {"id": "ATXid_x", "status": "Success"}
    assert client.post("/sms/delivery-report", data=form).status_code == 403
    assert client.post("/sms/delivery-report?token=wrong", data=form).status_code == 403
    assert client.post("/sms/delivery-report?token=s3cret", data=form).status_code == 200


def test_tests_never_send_by_default():
    assert settings.SMS_DRY_RUN is True or settings.AT_USERNAME == "sandbox"
