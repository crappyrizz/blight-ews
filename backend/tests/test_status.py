"""Tests for readable error reporting: the problem log (backend/events.py), what writes
to it (risk job, SMS, unexpected errors), the farmer's warnings and the admin's status
(backend/status.py), and the one error shape every endpoint answers with."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_alerts import TODAY, FakeATSMS, make_node

from backend import alerts, auth, cnn_service, events, risk_job, risk_model, sms
from backend import status as system_status
from backend.app import app
from backend.config import settings
from backend.db import get_db
from backend.models import Alert, Farmer, SystemEvent

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


def stored(db_session, **where):
    return db_session.query(SystemEvent).filter_by(**where).order_by(SystemEvent.occurred_at).all()


def bearer(farmer):
    return {"Authorization": f"Bearer {auth.create_access_token(farmer.farmer_id)}"}


def make_admin(db_session):
    admin = Farmer(name="Admin", phone_number="+254700111999", password_hash="x", role=auth.ROLE_ADMIN)
    db_session.add(admin)
    db_session.flush()
    return admin


# --- the problem log -------------------------------------------------------------------------


def test_an_event_is_stored_in_plain_words(db_session):
    assert events.record("sms", "sms_not_sent", "The SMS was not sent.", events.WARNING, node_id="NODE-1")
    [event] = stored(db_session, code="sms_not_sent")
    assert (event.source, event.severity, event.node_id, event.message) == (
        "sms", "warning", "NODE-1", "The SMS was not sent.")
    assert event.event_id.startswith("EVT-")


def test_a_problem_that_persists_is_reported_once_per_quiet_period(db_session):
    for _ in range(3):
        events.record("risk_job", "learned_model_missing", "missing", events.WARNING, quiet_hours=24)
    events.record("risk_job", "node_scoring_failed", "x", node_id="NODE-A", quiet_hours=6)
    events.record("risk_job", "node_scoring_failed", "x", node_id="NODE-B", quiet_hours=6)  # another node
    assert len(stored(db_session, code="learned_model_missing")) == 1
    assert len(stored(db_session, code="node_scoring_failed")) == 2


def test_recording_never_breaks_the_caller(monkeypatch):
    def broken():
        raise RuntimeError("database gone")
    monkeypatch.setattr(events, "session_factory", broken)
    assert events.record("api", "server_error", "x") is False  # logged, not raised


def test_old_routine_rows_are_pruned_but_problems_kept(db_session):
    old = NOW - timedelta(days=events.KEEP_INFO_DAYS + 1)
    db_session.add_all([
        SystemEvent(source="risk_job", severity="info", code="risk_job_ran", message="old run", occurred_at=old),
        SystemEvent(source="sms", severity="warning", code="sms_not_sent", message="old problem", occurred_at=old),
    ])
    db_session.flush()
    events.prune_info(NOW)
    db_session.expire_all()
    assert [e.code for e in stored(db_session)] == ["sms_not_sent"]


# --- the risk job ------------------------------------------------------------------------------


def test_one_failing_sensor_does_not_stop_the_others(db_session, monkeypatch):
    first, second = sorted([make_node(db_session, phone="+254722000201"),
                            make_node(db_session, phone="+254722000202")], key=lambda n: n.node_id)
    db_session.commit()  # stored, as real sensors are: the failure's rollback must not erase them
    bad, good = first, second  # the job scores in node_id order: the failure comes first, the hard case
    real = risk_job.score_node

    def score(session, node, today, learned):
        if node.node_id == bad.node_id:
            raise ValueError("readings table locked")
        return real(session, node, today, learned)
    monkeypatch.setattr(risk_job, "score_node", score)

    summary = risk_job.run_once(db_session, TODAY)

    assert {"node_id": bad.node_id, "error": "ValueError: readings table locked"} in summary
    assert any(entry["node_id"] == good.node_id and "error" not in entry for entry in summary)
    [event] = stored(db_session, code="node_scoring_failed")
    assert event.node_id == bad.node_id
    assert "could not be worked out" in event.message and "readings table locked" in event.message


def test_each_run_leaves_a_record_for_the_status_page(db_session, monkeypatch):
    make_node(db_session, phone="+254722000203")
    import backend.db
    monkeypatch.setattr(backend.db, "SessionLocal",
                        lambda: Session(bind=db_session.bind, join_transaction_mode="create_savepoint"))
    risk_job.run_job()
    [event] = stored(db_session, code="risk_job_ran")
    assert event.severity == "info" and event.message.startswith("Scored 1 of 1 sensors")


# --- SMS -------------------------------------------------------------------------------------


@pytest.mark.parametrize("delivery_status, failed", [
    ("dry_run", False), ("submitted", False), ("Success", False), ("Sent", False), ("Buffered", False),
    ("error", True), ("Failed: DeliveryFailure", True), ("Rejected: UserInBlacklist", True),
    ("InvalidPhoneNumber", True), (None, False),
])
def test_which_delivery_statuses_mean_the_farmer_did_not_get_it(delivery_status, failed):
    assert alerts.sms_failed(delivery_status) is failed


def test_an_sms_that_could_not_be_sent_is_recorded(db_session, monkeypatch):
    monkeypatch.setattr(settings, "SMS_DRY_RUN", False)
    monkeypatch.setattr(sms, "_service", FakeATSMS(status_code=403, status="InvalidPhoneNumber"))
    node = make_node(db_session, phone="+254722000204")
    alerts.send_and_record(db_session, node, alerts.ALERT_HIGH_RISK, "Blight risk is HIGH.")
    [event] = stored(db_session, code="sms_not_sent")
    assert event.node_id == node.node_id and event.farmer_id == node.farmer_id
    assert "high risk SMS" in event.message and "InvalidPhoneNumber" in event.message


def test_an_sms_reported_undelivered_is_recorded(db_session):
    node = make_node(db_session, phone="+254722000205")
    db_session.add(Alert(node_id=node.node_id, alert_type="high_risk", message="m",
                         delivery_status="submitted", provider_message_id="ATXid_9"))
    db_session.flush()
    alerts.apply_delivery_report(db_session, "ATXid_9", "Success")
    assert stored(db_session, code="sms_not_delivered") == []
    alerts.apply_delivery_report(db_session, "ATXid_9", "Failed", "DeliveryFailure")
    [event] = stored(db_session, code="sms_not_delivered")
    assert "Failed: DeliveryFailure" in event.message


# --- the farmer's warnings ---------------------------------------------------------------------


@pytest.fixture
def cnn_loaded(monkeypatch):
    monkeypatch.setattr(cnn_service, "_classifier", object())


def codes(warnings):
    return [w["code"] for w in warnings]


def test_no_warnings_when_all_is_well(db_session, cnn_loaded):
    node = make_node(db_session, phone="+254722000206")
    node.last_sync_time = NOW - timedelta(minutes=20)
    assert system_status.farmer_warnings(db_session, node, NOW) == []


def test_a_quiet_sensor_says_for_how_long_and_what_to_check(db_session, cnn_loaded):
    node = make_node(db_session, phone="+254722000207")
    assert codes(system_status.farmer_warnings(db_session, node, NOW)) == ["sensor_never_seen"]
    node.last_sync_time = NOW - timedelta(hours=5)
    [warning] = system_status.farmer_warnings(db_session, node, NOW)
    assert warning["code"] == "sensor_quiet"
    assert "5 hours" in warning["message"] and "power" in warning["message"]


def test_an_undelivered_sms_and_a_missing_leaf_checker_are_shown(db_session, monkeypatch):
    monkeypatch.setattr(cnn_service, "_classifier", None)
    node = make_node(db_session, phone="+254722000208")
    node.last_sync_time = NOW
    db_session.add(Alert(node_id=node.node_id, alert_type="high_risk", message="m",
                         delivery_status="Failed: DeliveryFailure", sent_at=NOW - timedelta(hours=2)))
    db_session.flush()
    assert codes(system_status.farmer_warnings(db_session, node, NOW)) == ["sms_not_delivered",
                                                                           "leaf_check_unavailable"]


def test_the_dashboard_carries_the_warnings(client, db_session, cnn_loaded):
    node = make_node(db_session, phone="+254722000209")
    farmer = db_session.get(Farmer, node.farmer_id)
    body = client.get(f"/nodes/{node.node_id}/dashboard", headers=bearer(farmer)).json()
    assert codes(body["warnings"]) == ["sensor_never_seen"]


# --- the admin's status ------------------------------------------------------------------------


def test_status_is_for_admins_only(client, db_session):
    farmer = db_session.get(Farmer, make_node(db_session, phone="+254722000210").farmer_id)
    assert client.get("/admin/status", headers=bearer(farmer)).status_code == 403
    assert client.get("/admin/status").status_code == 401


def test_status_names_each_part_and_the_recent_problems(client, db_session, cnn_loaded):
    admin = make_admin(db_session)
    quiet = make_node(db_session, phone="+254722000211")
    events.record("risk_job", "risk_job_ran", "Scored 1 of 1 sensors.", events.INFO)
    events.record("sms", "sms_not_sent", "The high risk SMS was not sent.", events.WARNING, node_id=quiet.node_id)

    body = client.get("/admin/status", headers=bearer(admin)).json()

    checks = {c["name"]: c for c in body["checks"]}
    assert list(checks) == ["database", "leaf_checker", "risk_job", "learned_model", "sms", "sensors", "weather"]
    assert checks["leaf_checker"]["status"] == "ok"
    assert checks["risk_job"]["status"] == "ok" and "Scored 1 of 1" in checks["risk_job"]["message"]
    assert checks["sensors"]["status"] == "warning" and quiet.node_id in checks["sensors"]["message"]
    assert body["overall"] in {"warning", "error"}
    assert [p["code"] for p in body["recent_problems"]] == ["sms_not_sent"]  # routine runs are not problems
    [sensor] = [s for s in body["sensors"] if s["node_id"] == quiet.node_id]
    assert sensor["quiet"] is True and sensor["last_seen"] is None


def test_reading_the_status_never_writes_anything(db_session, monkeypatch, cnn_loaded):
    # As on GitHub's test machine: the learned model files are not there.
    def missing(*args, **kwargs):
        raise FileNotFoundError("models/risk_24h.joblib not found")
    monkeypatch.setattr(risk_model, "load_models", missing)
    body = system_status.admin_status(db_session)
    learned = next(c for c in body["checks"] if c["name"] == "learned_model")
    assert learned["status"] == "warning" and "missing" in learned["message"]
    assert stored(db_session) == []  # a status page that writes would report its own reading


def test_a_failed_run_and_a_missing_leaf_checker_are_errors(db_session, monkeypatch):
    monkeypatch.setattr(cnn_service, "_classifier", None)
    events.record("risk_job", "risk_job_ran", "Scored 2 of 2 sensors.", events.INFO)
    events.record("risk_job", "risk_job_failed", "The hourly risk job failed (OperationalError: gone).")
    body = system_status.admin_status(db_session)
    checks = {c["name"]: c for c in body["checks"]}
    assert checks["risk_job"]["status"] == "error" and "OperationalError" in checks["risk_job"]["message"]
    assert checks["leaf_checker"]["status"] == "error"
    assert body["overall"] == "error"


@pytest.mark.parametrize("delta, words", [
    (timedelta(minutes=1), "1 minute"), (timedelta(minutes=59), "59 minutes"), (timedelta(hours=1), "1 hour"),
    (timedelta(hours=47), "47 hours"), (timedelta(days=3, hours=5), "3 days"),
])
def test_durations_read_naturally(delta, words):
    assert system_status.ago(delta) == words


# --- one error shape --------------------------------------------------------------------------


def test_every_error_has_a_code_and_a_plain_message(client, db_session):
    farmer = db_session.get(Farmer, make_node(db_session, phone="+254722000212").farmer_id)

    missing = client.get("/nodes/NODE-NOPE", headers=bearer(farmer)).json()
    assert missing["error"]["code"] == "not_found" and missing["detail"]  # detail kept for old callers

    invalid = client.get("/nodes/x/history?days=0", headers=bearer(farmer)).json()
    assert invalid["error"]["code"] == "invalid_input"
    assert invalid["error"]["message"].startswith("days: ")
    assert isinstance(invalid["detail"], list)  # FastAPI's usual list, unchanged

    no_login = client.get("/me")
    assert no_login.json()["error"]["code"] == "not_logged_in"
    assert no_login.headers.get("www-authenticate")  # headers survive


def test_an_endpoints_own_reason_becomes_the_code(client, db_session, cnn_loaded):
    from backend.app import error_body
    body = error_body(422, {"reason": "too_blurry", "message": "The photo is blurry."})
    assert body["error"] == {"code": "too_blurry", "message": "The photo is blurry."}


def test_an_unexpected_error_is_recorded_and_answered_plainly(db_session):
    class Broken:
        def get(self, *args, **kwargs):
            raise RuntimeError("disk full")
    farmer = Farmer(farmer_id="FRM-BROKEN0001", name="F", phone_number="+254722000213", password_hash="x",
                    role="farmer")
    app.dependency_overrides[get_db] = lambda: Broken()
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/me", headers=bearer(farmer))
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "server_error",
                                        "message": "The server had a problem. It has been recorded for the admin."}
    [event] = stored(db_session, code="server_error")
    assert "GET /me failed: RuntimeError: disk full" in event.message
