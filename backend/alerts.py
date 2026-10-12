"""SMS alerts (FR vii): what to send, when, and the record of every message.

backend/sms.py only sends; this module decides.

THREE MESSAGES, all decision support (what the weather or the photo shows and
what might be worth checking, never an instruction such as "spray"):

- high_risk: the plot's risk level has just become HIGH.
- diagnosis: the result of a leaf photo whose late-blight call was flagged.
- low_risk:  a status message, only when the farmer asks for one in the app.

Each fits one SMS: at most 160 characters, and only characters from the
basic GSM-7 alphabet. A single curly quote or dash would switch the whole
message to Unicode, where one SMS holds only 70 characters.

THE POLICY:

1. high_risk is sent when a node's JUDGED risk level CHANGES to high (from
   medium, low, or no level yet), at most once per 24 hours per node, so a
   level flickering high-medium-high does not repeat the alarm. The same
   high level hour after hour sends nothing. Unknown risk (no recent judged
   data: fusion's neutral prior) changes nothing, neither the remembered
   level nor the cooldown, and is never reported as low.
2. diagnosis is sent whenever late_blight_flagged is True, once per
   diagnosis.
3. low_risk is sent only on request, only when the current risk is judged
   and low, and at most once an hour per node.

Every message sent (or attempted, or logged in dry-run mode) writes an
alerts row with alert_type, message, provider_message_id and delivery_status,
so the history shows exactly what the farmer was told.
"""
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import fusion, sms
from backend.models import Alert, DiagnosisResult, Farmer, SensorNode

ALERT_HIGH_RISK = "high_risk"
ALERT_DIAGNOSIS = "diagnosis"
ALERT_LOW_RISK = "low_risk"

HIGH_RISK_COOLDOWN = timedelta(hours=24)
STATUS_COOLDOWN = timedelta(hours=1)

SMS_MAX_CHARS = 160
PLOT_MAX_CHARS = 36  # keeps every template inside 160 characters (tested)
SIGNATURE = " -Blight EWS"

# Characters that stay in the GSM-7 basic set (one SMS = 160 of them).
_NOT_GSM_SAFE = re.compile(r"[^A-Za-z0-9 .,:;!?'\"()%&+=/#@*<>_$-]")

LABEL_WORDS = {"late_blight": "late blight", "early_blight": "early blight", "healthy": "a healthy leaf"}


# --- the three templates ------------------------------------------------------------


def gsm_safe(text: str) -> str:
    """Replace anything outside the basic GSM-7 set (curly quotes, dashes, emoji...)."""
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return _NOT_GSM_SAFE.sub("", text)


def plot_name(location: str, farm_id: str | None) -> str:
    """'Kinangop plot (FARM-1)', at most PLOT_MAX_CHARS.

    If it is too long, the location is shortened, never the farm id, so the
    farmer can always tell which plot the message is about.
    """
    farm = f" ({gsm_safe(farm_id)[:16]})" if farm_id else ""
    room = PLOT_MAX_CHARS - len(farm)
    place = " ".join(gsm_safe(location).split())[:room].strip() or "your plot"
    return f"{place}{farm}"


def high_risk_message(plot: str) -> str:
    return (f"Blight risk at {plot} is now HIGH: weather favours late blight. "
            f"Consider checking your crop over the next 1-2 days.{SIGNATURE}")


def diagnosis_message(label: str, confidence: float, risk_level: str) -> str:
    """Sent only when late blight was flagged."""
    words = LABEL_WORDS.get(label, gsm_safe(label.replace("_", " ")))
    return (f"Leaf photo result: {words} ({confidence:.0%}), weather risk {risk_level.upper()}. "
            f"Late blight flagged; consider checking nearby plants.{SIGNATURE}")


def low_risk_message(plot: str) -> str:
    return f"Status for {plot}: blight risk is LOW at the moment. No action suggested.{SIGNATURE}"


# --- the policy (pure functions) ------------------------------------------------------


def should_send_high_risk(previous_level: str | None, current_level: str,
                          last_high_risk_at: datetime | None, now: datetime) -> tuple[bool, str]:
    """Send when the level CHANGES to high, at most once per 24 h. Returns (send?, reason)."""
    if current_level != "high":
        return False, f"level is {current_level}, not high"
    if previous_level == "high":
        return False, "still high: not a change"
    if last_high_risk_at is not None and now - last_high_risk_at < HIGH_RISK_COOLDOWN:
        return False, f"cooldown: last high-risk SMS at {last_high_risk_at:%Y-%m-%d %H:%M} UTC"
    return True, f"level changed {previous_level or 'none'} -> high"


# --- sending and recording ------------------------------------------------------------


def _farmer(session: Session, node: SensorNode) -> Farmer:
    return session.get(Farmer, node.farmer_id)


def send_and_record(session: Session, node: SensorNode, alert_type: str, message: str,
                    result_id: str | None = None, risk_score_id: str | None = None) -> Alert:
    """Send one SMS to the node's farmer and write its alerts row (flushed, not committed)."""
    if len(message) > SMS_MAX_CHARS:
        raise ValueError(f"{alert_type} message is {len(message)} characters: {message}")
    outcome = sms.send(_farmer(session, node).phone_number, message)
    alert = Alert(
        node_id=node.node_id, result_id=result_id, risk_score_id=risk_score_id,
        alert_type=alert_type, message=message,
        provider_message_id=outcome.message_id, delivery_status=outcome.status,
    )
    session.add(alert)
    session.flush()
    return alert


def _last_alert_at(session: Session, node_id: str, alert_type: str) -> datetime | None:
    return session.scalar(
        select(Alert.sent_at).where(Alert.node_id == node_id, Alert.alert_type == alert_type)
        .order_by(Alert.sent_at.desc()).limit(1)
    )


def check_high_risk(session: Session, node: SensorNode, today: date,
                    now: datetime | None = None) -> Alert | None:
    """Called by the risk job after scoring a node. Sends a high_risk SMS if the policy says so."""
    now = now or datetime.now(UTC)
    risk = fusion.resolve_risk(session, node.node_id, today)
    if risk.basis == fusion.BASIS_NEUTRAL_PRIOR:
        return None  # unknown: change nothing, say nothing

    level = fusion.risk_level(risk.score)
    previous, node.last_risk_level = node.last_risk_level, level
    send, _reason = should_send_high_risk(previous, level,
                                          _last_alert_at(session, node.node_id, ALERT_HIGH_RISK), now)
    if not send:
        session.flush()
        return None
    farmer = _farmer(session, node)
    return send_and_record(session, node, ALERT_HIGH_RISK,
                           high_risk_message(plot_name(node.location, farmer.farm_id)),
                           risk_score_id=risk.score_id)


def send_diagnosis_alert(session: Session, node: SensorNode, diagnosis: DiagnosisResult) -> Alert | None:
    """A diagnosis SMS when late blight was flagged; never twice for the same diagnosis."""
    if not diagnosis.late_blight_flagged:
        return None
    already = session.scalar(select(Alert).where(Alert.result_id == diagnosis.result_id))
    if already is not None:
        return None
    message = diagnosis_message(diagnosis.disease_label, diagnosis.confidence, diagnosis.risk_level)
    return send_and_record(session, node, ALERT_DIAGNOSIS, message, result_id=diagnosis.result_id)


@dataclass
class StatusRefused(Exception):
    """The status SMS was not sent; `reason` is a code, `message` says why in words."""

    reason: str
    message: str


def send_status(session: Session, node: SensorNode, today: date, now: datetime | None = None) -> Alert:
    """The on-demand low_risk SMS. Raises StatusRefused when it would be wrong or a repeat."""
    now = now or datetime.now(UTC)
    risk = fusion.resolve_risk(session, node.node_id, today)
    if risk.basis == fusion.BASIS_NEUTRAL_PRIOR:
        raise StatusRefused("unknown", "There are no recent complete readings, so the risk is unknown; "
                                       "no 'low risk' message is sent from missing data.")
    level = fusion.risk_level(risk.score)
    if level != "low":
        raise StatusRefused(f"risk_{level}", f"The current risk is {level}, not low; the dashboard has the details.")
    last = _last_alert_at(session, node.node_id, ALERT_LOW_RISK)
    if last is not None and now - last < STATUS_COOLDOWN:
        raise StatusRefused("recently_sent", f"A status SMS was already sent at {last:%H:%M} UTC.")
    farmer = _farmer(session, node)
    return send_and_record(session, node, ALERT_LOW_RISK,
                           low_risk_message(plot_name(node.location, farmer.farm_id)),
                           risk_score_id=risk.score_id)


def apply_delivery_report(session: Session, message_id: str, status: str,
                          failure_reason: str | None = None) -> Alert | None:
    """Update an alert from Africa's Talking's delivery report. None if the id is unknown."""
    alert = session.scalar(select(Alert).where(Alert.provider_message_id == message_id))
    if alert is None:
        return None
    alert.delivery_status = (f"{status}: {failure_reason}" if failure_reason else status)[:30]
    session.flush()
    return alert
