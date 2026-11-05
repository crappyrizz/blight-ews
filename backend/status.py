"""The system's health in plain words: the admin's status page and the farmer's warnings.

admin_status() (GET /admin/status) checks each part (database, leaf checker,
risk job, learned model, SMS, sensors, weather data), each as {name, status,
message} with status ok / warning / error, plus every sensor's last contact
and the recent problems from system_events (backend/events.py).

farmer_warnings() (in GET /nodes/{id}/dashboard) gives a farmer only what
affects them: their sensor gone quiet, an SMS to them not delivered, leaf
checking unavailable. Each is {code, message}, with what to do where the
farmer can do something.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import alerts, cnn_service, events, risk_job
from backend.config import settings
from backend.models import Alert, Farmer, SensorNode, SystemEvent, WeatherReading

OK, WARNING, ERROR = "ok", "warning", "error"
QUIET_AFTER = timedelta(hours=3)  # no contact for longer: the sensor is "quiet" (as the dashboard says)
WEATHER_BEHIND_AFTER = timedelta(days=10)  # ERA5 arrives ~5 days late; twice that means fetching stopped
SMS_LOOKBACK = timedelta(days=7)  # a farmer is warned about an undelivered SMS this long
PROBLEMS_SHOWN = 50


def ago(delta: timedelta) -> str:
    """'5 minutes', '3 hours', '2 days': how long, for a sentence."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours // 24} days"


def _check(name: str, state: str, message: str) -> dict:
    return {"name": name, "status": state, "message": message}


# --- farmer -------------------------------------------------------------------------------


def farmer_warnings(session: Session, node: SensorNode, now: datetime | None = None) -> list[dict]:
    """The problems this farmer should know about, for this sensor. Empty when all is well."""
    now = now or datetime.now(UTC)
    warnings = []
    if node.last_sync_time is None:
        warnings.append({"code": "sensor_never_seen",
                         "message": "Your sensor has not sent any readings yet. Check that it has power "
                                    "and that the Wi-Fi it uses is switched on."})
    elif now - node.last_sync_time > QUIET_AFTER:
        warnings.append({"code": "sensor_quiet",
                         "message": f"Your sensor has not sent readings for {ago(now - node.last_sync_time)}. "
                                    "Check that it has power and that the Wi-Fi it uses is switched on. "
                                    "It keeps its readings and sends them when it is back."})
    last_sms = session.scalars(
        select(Alert).where(Alert.node_id == node.node_id, Alert.sent_at >= now - SMS_LOOKBACK)
        .order_by(Alert.sent_at.desc())
    ).first()
    if last_sms is not None and alerts.sms_failed(last_sms.delivery_status):
        warnings.append({"code": "sms_not_delivered",
                         "message": "The last SMS to your phone could not be delivered. Warnings still "
                                    "appear here in the app; check that your phone number is correct."})
    if not cnn_service.is_loaded():
        warnings.append({"code": "leaf_check_unavailable",
                         "message": "Leaf checking is not available right now. The weather warnings still "
                                    "work; please try a leaf photo again later."})
    return warnings


# --- admin --------------------------------------------------------------------------------


def _latest_event(session: Session, source: str, code: str) -> SystemEvent | None:
    return session.scalars(
        select(SystemEvent).where(SystemEvent.source == source, SystemEvent.code == code)
        .order_by(SystemEvent.occurred_at.desc())
    ).first()


def _risk_job_check(session: Session, now: datetime) -> dict:
    if not settings.RISK_JOB_ENABLED:
        return _check("risk_job", WARNING, "Switched off (RISK_JOB_ENABLED=false): risk is not being updated.")
    last_run = _latest_event(session, "risk_job", "risk_job_ran")
    last_failure = _latest_event(session, "risk_job", "risk_job_failed")
    if last_failure and (last_run is None or last_failure.occurred_at > last_run.occurred_at):
        return _check("risk_job", ERROR, f"The last run failed {ago(now - last_failure.occurred_at)} ago: "
                                         f"{last_failure.message}")
    if last_run is None:
        return _check("risk_job", WARNING, "No completed run recorded yet. It runs when the server starts, "
                                           f"then every {settings.RISK_JOB_INTERVAL_MINUTES} minutes.")
    age = now - last_run.occurred_at
    message = f"Last ran {ago(age)} ago: {last_run.message}"
    if age > timedelta(minutes=2 * settings.RISK_JOB_INTERVAL_MINUTES):
        return _check("risk_job", WARNING, message + " The free server sleeps when nobody uses it; the job "
                                                     "runs again as soon as it wakes.")
    return _check("risk_job", WARNING if "failed" in last_run.message else OK, message)


def _sms_check(session: Session, now: datetime) -> dict:
    if settings.SMS_DRY_RUN:
        mode = "Test mode (SMS_DRY_RUN): messages are logged, not sent."
    elif settings.AT_USERNAME == "sandbox":
        mode = "Africa's Talking sandbox: messages go to the sandbox simulator, not real phones."
    else:
        mode = "Live: messages go to farmers' phones through Africa's Talking."
    recent = session.scalars(select(Alert.delivery_status).where(Alert.sent_at >= now - timedelta(hours=24))).all()
    failed = sum(alerts.sms_failed(s) for s in recent)
    if failed:
        return _check("sms", WARNING, f"{mode} {failed} of {len(recent)} SMS in the last 24 hours were not "
                                      "sent or not delivered (see the problems below).")
    return _check("sms", OK, f"{mode} {len(recent)} sent in the last 24 hours, none failed.")


def _weather_check(session: Session, now: datetime) -> dict:
    newest = session.scalar(select(func.max(WeatherReading.timestamp)).where(WeatherReading.source == "archive"))
    if newest is None:
        return _check("weather", WARNING, "No weather history stored: the weather-API comparison cannot run.")
    age = now - newest
    message = f"Weather history (ERA5-Land) runs to {newest:%d %b %Y}, {ago(age)} ago."
    if age > WEATHER_BEHIND_AFTER:
        return _check("weather", WARNING, message + " It is not being kept up to date "
                                                    "(python -m ml.fetch_weather), so the API-based scores lag.")
    return _check("weather", OK, message)


def admin_status(session: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    nodes = session.execute(
        select(SensorNode, Farmer.name).join(Farmer, Farmer.farmer_id == SensorNode.farmer_id)
        .order_by(SensorNode.node_id)
    ).all()
    sensors = [{
        "node_id": node.node_id,
        "location": node.location,
        "owner": owner,
        "last_seen": node.last_sync_time,
        "quiet": node.last_sync_time is None or now - node.last_sync_time > QUIET_AFTER,
        "risk_level": node.last_risk_level,
    } for node, owner in nodes]
    quiet = [s for s in sensors if s["quiet"]]
    if not sensors:
        sensor_check = _check("sensors", WARNING, "No sensors are registered.")
    elif quiet:
        names = ", ".join(s["node_id"] + (f" ({ago(now - s['last_seen'])})" if s["last_seen"] else " (never)")
                          for s in quiet)
        sensor_check = _check("sensors", WARNING,
                              f"{len(sensors) - len(quiet)} of {len(sensors)} sensors sent readings in the last "
                              f"{ago(QUIET_AFTER)}. Quiet: {names}.")
    else:
        sensor_check = _check("sensors", OK, f"All {len(sensors)} sensors sent readings in the last "
                                             f"{ago(QUIET_AFTER)}.")

    leaf = (_check("leaf_checker", OK, "The CNN is loaded; leaf photos are being checked.")
            if cnn_service.is_loaded() else
            _check("leaf_checker", ERROR, "The CNN is not loaded (models/cnn.tflite or labels.txt missing on "
                                          "the server), so leaf photos are refused. Run tools/fetch_models.py."))
    learned = (_check("learned_model", OK, "The learned 24/48 h model is loaded.")
               if risk_job.learned_model_available() else
               _check("learned_model", WARNING, "The learned 24/48 h model is missing: only the Hutton rule "
                                                "is scoring, and the forecast is not shown."))
    checks = [
        _check("database", OK, "Reachable."),
        leaf,
        _risk_job_check(session, now),
        learned,
        _sms_check(session, now),
        sensor_check,
        _weather_check(session, now),
    ]
    problems = session.scalars(
        select(SystemEvent).where(SystemEvent.severity != events.INFO, SystemEvent.occurred_at >= now - timedelta(days=7))
        .order_by(SystemEvent.occurred_at.desc()).limit(PROBLEMS_SHOWN)
    ).all()
    states = {c["status"] for c in checks}
    return {
        "checked_at": now,
        "overall": ERROR if ERROR in states else (WARNING if WARNING in states else OK),
        "checks": checks,
        "sensors": sensors,
        "recent_problems": [{
            "occurred_at": p.occurred_at, "source": p.source, "severity": p.severity, "code": p.code,
            "message": p.message, "node_id": p.node_id,
        } for p in problems],
    }
