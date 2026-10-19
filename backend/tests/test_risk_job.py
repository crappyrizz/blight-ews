"""Tests for the risk job (backend/risk_job.py), the risk-level bands, and the
dashboard, history and symptom-report endpoints."""
from datetime import UTC, date, datetime, timedelta

import pytest
from test_build_features import synthetic
from test_risk_model import save_tiny_models

from backend import dashboard, fusion, hutton, risk_job, risk_model
from backend.config import settings
from backend.models import (
    Alert,
    DiagnosisResult,
    Farmer,
    LeafImage,
    RiskScore,
    SensorNode,
    SensorReading,
)

PASSWORD = "correct-horse-battery"
PHONE, OTHER_PHONE = "+254733100200", "+254733100300"
NOW = datetime.now(UTC)
TODAY = NOW.astimezone(hutton.LOCAL_TZ).date()


# --- helpers --------------------------------------------------------------------


def login(client, phone=PHONE):
    client.post("/auth/register", json={"name": "Risk Farmer", "phone_number": phone, "password": PASSWORD})
    token = client.post("/auth/login", json={"phone_number": phone, "password": PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/me", headers=headers).json()["farmer_id"]


def make_node(session, farmer_id):
    node = SensorNode(farmer_id=farmer_id, location="plot", latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def seed_sensor(session, node, days=20, until=NOW):
    """`days` of synthetic hourly readings up to `until` (nothing from the future)."""
    start = TODAY - timedelta(days=days - 1)
    weather = synthetic(days=days, start=start)
    weather = weather[weather["timestamp"] <= until]
    for row in weather.itertuples():
        session.add(SensorReading(node_id=node.node_id, timestamp=row.timestamp,
                                  temperature=row.temperature, humidity=row.humidity))
    session.flush()
    return weather


@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_model, "MODEL_PATHS", save_tiny_models(tmp_path))
    monkeypatch.setattr(risk_model, "_bundles", {})


def rows(session, node, **filters):
    query = session.query(RiskScore).filter_by(node_id=node.node_id, **filters)
    return {(r.method, r.data_source, r.horizon_hours, r.for_date): r for r in query}


# --- risk-level cut-offs ------------------------------------------------------------


@pytest.mark.parametrize("score, level", [
    (0.0, "low"), (0.3299, "low"), (0.33, "medium"), (0.5, "medium"),
    (0.6599, "medium"), (0.66, "high"), (1.0, "high"),
])
def test_risk_level_cut_offs(score, level):
    assert fusion.risk_level(score) == level


def test_cut_offs_come_from_config():
    assert (fusion.MEDIUM_RISK_FROM, fusion.HIGH_RISK_FROM) == (settings.RISK_MEDIUM_FROM, settings.RISK_HIGH_FROM)
    assert (settings.RISK_MEDIUM_FROM, settings.RISK_HIGH_FROM) == (0.33, 0.66)


# --- the job ---------------------------------------------------------------------------


def test_run_once_scores_both_sources_for_today_and_the_lookback(db_session, models):
    farmer = Farmer(name="F", phone_number="+254733999001", password_hash="x", role="farmer")
    db_session.add(farmer)
    db_session.flush()
    node = make_node(db_session, farmer.farmer_id)
    seed_sensor(db_session, node)

    summary = risk_job.run_once(db_session, TODAY)

    days = [TODAY - timedelta(days=n) for n in range(risk_job.LOOKBACK_DAYS + 1)]
    stored = rows(db_session, node)
    for day in days:
        for source in ("sensor", "api"):
            assert ("hutton", source, 0, day) in stored
            for horizon in (24, 48):
                assert ("learned", source, horizon, day) in stored
    # Yesterday is complete from the sensor: judged, with probabilities.
    yesterday = stored[("learned", "sensor", 24, TODAY - timedelta(days=1))]
    assert yesterday.status == "ok" and 0 <= yesterday.score <= 1
    assert yesterday.model_version == "test-24h"
    # No archive weather was seeded, so the API rows say so, never 0.
    api = stored[("hutton", "api", 0, TODAY - timedelta(days=1))]
    assert api.status == "no_data" and api.score is None
    # Every row obeys the rule: a score exactly when the day was judged.
    assert all((r.status == "ok") == (r.score is not None) for r in stored.values())
    assert {s["data_source"] for s in summary} == {"sensor", "api"}


def test_run_once_twice_gives_the_same_table(db_session, models):
    farmer = Farmer(name="F", phone_number="+254733999002", password_hash="x", role="farmer")
    db_session.add(farmer)
    db_session.flush()
    node = make_node(db_session, farmer.farmer_id)
    seed_sensor(db_session, node)

    risk_job.run_once(db_session, TODAY)
    first = {k: (r.status, r.score) for k, r in rows(db_session, node).items()}
    risk_job.run_once(db_session, TODAY)
    second = {k: (r.status, r.score) for k, r in rows(db_session, node).items()}

    assert first == second


def test_without_model_files_hutton_still_runs(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(risk_model, "MODEL_PATHS", {24: tmp_path / "none.joblib"})
    monkeypatch.setattr(risk_model, "_bundles", {})
    farmer = Farmer(name="F", phone_number="+254733999003", password_hash="x", role="farmer")
    db_session.add(farmer)
    db_session.flush()
    node = make_node(db_session, farmer.farmer_id)

    summary = risk_job.run_once(db_session, TODAY)

    methods = {key[0] for key in rows(db_session, node)}
    assert methods == {"hutton"}
    assert summary[0]["learned_days_judged"] is None


def test_scheduler_is_off_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "RISK_JOB_ENABLED", False)
    assert risk_job.start_scheduler() is None


def test_scheduler_runs_the_job_on_the_configured_interval(monkeypatch):
    monkeypatch.setattr(settings, "RISK_JOB_ENABLED", True)
    monkeypatch.setattr(risk_job, "run_job", lambda: None)  # don't touch the real database
    scheduler = risk_job.start_scheduler()
    try:
        job = scheduler.get_job("risk_job")
        assert job.trigger.interval == timedelta(minutes=settings.RISK_JOB_INTERVAL_MINUTES)
        assert job.max_instances == 1
    finally:
        scheduler.shutdown(wait=False)


def test_command_line_needs_once():
    with pytest.raises(SystemExit):
        risk_job.main([])


# --- the summary sentence -----------------------------------------------------------------


@pytest.mark.parametrize("level, basis, p48, period", [
    (lv, b, p, per)
    for lv in ("low", "medium", "high")
    for b in (fusion.BASIS_TODAY, fusion.BASIS_FALLBACK, fusion.BASIS_NEUTRAL_PRIOR)
    for p in (None, 0.1, 0.5, 0.9)
    for per in (None, False, True)
])
def test_summary_is_decision_support_never_an_instruction(level, basis, p48, period):
    text = dashboard.summary_sentence(level, basis, TODAY, p48, period, 1.0).lower()
    assert "spray" not in text
    assert "must" not in text


def test_high_forecast_says_conditions_favour_blight():
    text = dashboard.summary_sentence("low", fusion.BASIS_TODAY, TODAY, 0.8, False, 0.5)
    assert text == "Conditions favour blight over the next 2 days, so consider checking the crop."


def test_medium_and_low_wording():
    assert "partly favourable" in dashboard.summary_sentence("medium", fusion.BASIS_TODAY, TODAY, 0.1, False, 0.5)
    assert dashboard.summary_sentence("low", fusion.BASIS_TODAY, TODAY, 0.1, False, 0.5) == \
        "Conditions do not currently favour blight."


def test_unknown_risk_is_never_called_low():
    text = dashboard.summary_sentence("medium", fusion.BASIS_NEUTRAL_PRIOR, None, None, None, None)
    assert "unknown" in text
    assert "do not currently favour" not in text


def test_fallback_and_a_quiet_sensor_are_mentioned():
    text = dashboard.summary_sentence("low", fusion.BASIS_FALLBACK, date(2026, 10, 11), 0.1, None, 5.2)
    assert "Based on the readings for 11 Oct" in text
    assert "last reported 5 hours ago" in text


# --- GET /nodes/{id}/dashboard ------------------------------------------------------------


def test_dashboard_after_a_job_run(client, db_session, models):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)
    weather = seed_sensor(db_session, node)
    risk_job.run_once(db_session, TODAY)

    response = client.get(f"/nodes/{node.node_id}/dashboard", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["latest_reading"]["temperature"] == pytest.approx(weather["temperature"].iloc[-1])
    assert 0 <= body["latest_reading"]["age_minutes"] <= 60
    assert body["last_24h"]["valid_hours"] >= 23
    assert body["last_24h"]["min_temperature"] <= body["last_24h"]["max_temperature"]
    assert body["hutton_today"]["for_date"] == str(TODAY)
    # The newest judged forecast: from yesterday, or from today once today has
    # 20 hours of readings (late in the local day). Either way it covers the next days.
    issued = date.fromisoformat(body["forecast"]["p24"]["issued_from"])
    assert issued in (TODAY - timedelta(days=1), TODAY)
    assert body["forecast"]["p48"]["covers"] == [str(issued + timedelta(days=1)), str(issued + timedelta(days=2))]
    assert 0 <= body["forecast"]["p24"]["probability"] <= 1
    assert body["risk"]["risk_level"] == fusion.risk_level(body["risk"]["score"])
    assert body["summary"]
    assert "spray" not in body["summary"].lower()


def test_dashboard_with_no_data_says_the_risk_is_unknown(client, db_session):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)

    body = client.get(f"/nodes/{node.node_id}/dashboard", headers=headers).json()

    assert body["latest_reading"] is None
    assert body["hutton_today"]["status"] == "not_computed"
    assert body["forecast"] == {"p24": None, "p48": None}
    assert body["risk"]["basis"] == fusion.BASIS_NEUTRAL_PRIOR
    assert "unknown" in body["summary"]


def test_dashboard_of_another_farmers_node_is_404(client, db_session):
    _, owner_id = login(client)
    node = make_node(db_session, owner_id)
    stranger, _ = login(client, OTHER_PHONE)
    assert client.get(f"/nodes/{node.node_id}/dashboard", headers=stranger).status_code == 404


def test_dashboard_needs_a_token(client, db_session):
    _, farmer_id = login(client)
    node = make_node(db_session, farmer_id)
    assert client.get(f"/nodes/{node.node_id}/dashboard").status_code == 401


# --- GET /nodes/{id}/history -------------------------------------------------------------


def test_history_is_newest_first_with_gaps_shown(client, db_session, models):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)
    seed_sensor(db_session, node)
    risk_job.run_once(db_session, TODAY)

    body = client.get(f"/nodes/{node.node_id}/history?days=10", headers=headers).json()

    dates = [d["for_date"] for d in body["risk"]]
    assert len(dates) == 10
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == str(TODAY)
    yesterday = body["risk"][1]
    assert yesterday["hutton"]["sensor"]["status"] == "ok"
    assert yesterday["learned"]["p24"]["sensor"] is not None
    oldest = body["risk"][-1]  # before the job's lookback: not computed, shown as a gap
    assert oldest["hutton"]["sensor"] is None and oldest["risk_level"] is None


def test_history_lists_diagnoses_and_alerts_newest_first(client, db_session):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)
    for days_ago in (3, 1):
        image = LeafImage(farmer_id=farmer_id, file_path=f"x{days_ago}.jpg",
                          capture_date=NOW - timedelta(days=days_ago))
        db_session.add(image)
        db_session.flush()
        db_session.add(DiagnosisResult(
            image_id=image.image_id, disease_label="late_blight", confidence=0.9,
            probabilities={"late_blight": 0.9}, risk_score_used=0.5, threshold_used=0.6,
            risk_level="medium", late_blight_flagged=True, timestamp=NOW - timedelta(days=days_ago)))
        db_session.add(Alert(node_id=node.node_id, alert_type="high_risk", message=f"m{days_ago}",
                             delivery_status="sent", sent_at=NOW - timedelta(days=days_ago)))
    db_session.flush()

    body = client.get(f"/nodes/{node.node_id}/history?days=7", headers=headers).json()

    assert [a["message"] for a in body["alerts"]] == ["m1", "m3"]
    assert [d["timestamp"] for d in body["diagnoses"]] == sorted(
        [d["timestamp"] for d in body["diagnoses"]], reverse=True)
    assert len(body["diagnoses"]) == 2


@pytest.mark.parametrize("days", [0, 366])
def test_history_days_must_be_sensible(client, db_session, days):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)
    assert client.get(f"/nodes/{node.node_id}/history?days={days}", headers=headers).status_code == 422


def test_history_of_another_farmers_node_is_404(client, db_session):
    _, owner_id = login(client)
    node = make_node(db_session, owner_id)
    stranger, _ = login(client, OTHER_PHONE)
    assert client.get(f"/nodes/{node.node_id}/history", headers=stranger).status_code == 404


# --- symptom reports ----------------------------------------------------------------------


def report(client, headers, node_id, onset, notes="Dark patches on lower leaves"):
    return client.post("/symptom-reports", headers=headers,
                       json={"node_id": node_id, "onset_date": str(onset), "notes": notes})


def test_farmer_reports_symptom_onset(client, db_session):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)

    response = report(client, headers, node.node_id, TODAY - timedelta(days=2))

    assert response.status_code == 201
    body = response.json()
    assert body["report_id"].startswith("SYM-")
    assert body["farmer_id"] == farmer_id
    assert body["onset_date"] == str(TODAY - timedelta(days=2))


def test_onset_today_is_allowed_but_not_tomorrow(client, db_session):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)
    assert report(client, headers, node.node_id, TODAY).status_code == 201
    response = report(client, headers, node.node_id, TODAY + timedelta(days=1))
    assert response.status_code == 422
    assert "future" in response.json()["detail"]


def test_cannot_report_on_another_farmers_node(client, db_session):
    _, owner_id = login(client)
    node = make_node(db_session, owner_id)
    stranger, _ = login(client, OTHER_PHONE)
    assert report(client, stranger, node.node_id, TODAY).status_code == 404


def test_list_shows_only_own_reports_newest_onset_first(client, db_session):
    headers, farmer_id = login(client)
    node = make_node(db_session, farmer_id)
    other_headers, other_id = login(client, OTHER_PHONE)
    other_node = make_node(db_session, other_id)
    report(client, headers, node.node_id, TODAY - timedelta(days=5))
    report(client, headers, node.node_id, TODAY - timedelta(days=1))
    report(client, other_headers, other_node.node_id, TODAY)

    mine = client.get("/symptom-reports", headers=headers).json()

    assert [r["onset_date"] for r in mine] == [str(TODAY - timedelta(days=1)), str(TODAY - timedelta(days=5))]
    assert {r["farmer_id"] for r in mine} == {farmer_id}
    assert client.get(f"/symptom-reports?node_id={other_node.node_id}", headers=headers).status_code == 404


def test_symptom_reports_need_a_token(client):
    assert client.get("/symptom-reports").status_code == 401
