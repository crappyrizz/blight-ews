"""What the app's dashboard and history screens show for one node.

The numbers come from risk_scores (written by backend/risk_job.py) and from
the node's latest readings. The wording is DECISION SUPPORT: it says what
the weather is doing and what the farmer might want to check, never what
he must do. The farmer has said he will not act on an automated
instruction until the system has earned it, so "Spray now" never appears.

Which number is "the current risk"? The same one fusion uses for a leaf
diagnosis (fusion.resolve_risk: today's Hutton score from the sensor, else
the latest judged day within 3 days, else the neutral 0.5), so the dashboard
and a diagnosis taken at the same moment can never disagree. The learned
24 h / 48 h probabilities are shown beside it as the forecast, and the
summary sentence takes both into account.
"""
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import fusion, hutton
from backend.models import Alert, DiagnosisResult, LeafImage, RiskScore, SensorNode, SensorReading

STALE_READING_HOURS = 3  # older than this, the summary mentions that the node is quiet
FORECAST_HORIZONS = (24, 48)


def local_today(now: datetime) -> date:
    return now.astimezone(hutton.LOCAL_TZ).date()


# --- the summary sentence ---------------------------------------------------------


def summary_sentence(risk_level: str, risk_basis: str, risk_for_date: date | None,
                     p48: float | None, hutton_period_today: bool | None,
                     reading_age_hours: float | None) -> str:
    """One plain sentence for the top of the dashboard.

    The level said is the higher of the current risk and the 48 h forecast,
    because the farmer's question is "should I be watching the crop over the
    next couple of days?". Unknown data is said to be unknown, never "low".
    """
    if risk_basis == fusion.BASIS_NEUTRAL_PRIOR:
        sentence = ("There are no complete sensor readings for the last few days, so the blight "
                    "risk is unknown; it may be worth checking that the sensor node has power "
                    "and signal.")
    else:
        forecast_level = fusion.risk_level(p48) if p48 is not None else "low"
        levels = ["low", "medium", "high"]
        level = max(risk_level, forecast_level, key=levels.index)
        if hutton_period_today or level == "high":
            sentence = "Conditions favour blight over the next 2 days, so consider checking the crop."
        elif level == "medium":
            sentence = ("Conditions are partly favourable for blight; it may be worth keeping an eye "
                        "on the crop over the next 2 days.")
        else:
            sentence = "Conditions do not currently favour blight."
        if risk_basis == fusion.BASIS_FALLBACK and risk_for_date is not None:
            sentence += f" (Based on the readings for {risk_for_date:%d %b}; today is not complete yet.)"

    if reading_age_hours is not None and reading_age_hours >= STALE_READING_HOURS:
        sentence += f" The sensor last reported {reading_age_hours:.0f} hours ago."
    return sentence


# --- the dashboard -------------------------------------------------------------------


def _score_row(session, node_id, method, horizon, for_date=None, judged_only=False,
               data_source="sensor"):
    query = select(RiskScore).where(
        RiskScore.node_id == node_id, RiskScore.method == method,
        RiskScore.data_source == data_source, RiskScore.horizon_hours == horizon,
    )
    if for_date is not None:
        query = query.where(RiskScore.for_date == for_date)
    if judged_only:
        query = query.where(RiskScore.status == hutton.STATUS_OK)
    return session.scalars(query.order_by(RiskScore.for_date.desc())).first()


def _last_24h(session: Session, node_id: str, now: datetime) -> dict:
    """Min/max temperature and humid hours over the 24 hours before now."""
    rows = session.execute(
        select(SensorReading.timestamp, SensorReading.temperature, SensorReading.humidity)
        .where(SensorReading.node_id == node_id,
               SensorReading.timestamp > now - timedelta(hours=24),
               SensorReading.timestamp <= now)
    ).all()
    if not rows:
        return {"readings": 0, "valid_hours": 0, "min_temperature": None,
                "max_temperature": None, "humid_hours": 0}
    readings = pd.DataFrame(rows, columns=["timestamp", "temperature", "humidity"])
    hourly = hutton.to_hourly(readings).dropna()  # hourly means, as the Hutton rule counts them
    return {
        "readings": len(readings),
        "valid_hours": len(hourly),
        "min_temperature": float(readings["temperature"].min()),
        "max_temperature": float(readings["temperature"].max()),
        "humid_hours": int((hourly["humidity"] >= hutton.RH_THRESHOLD_PCT).sum()),
    }


def build_dashboard(session: Session, node: SensorNode, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    today = local_today(now)

    latest = session.scalars(
        select(SensorReading).where(SensorReading.node_id == node.node_id)
        .order_by(SensorReading.timestamp.desc())
    ).first()
    age_hours = (now - latest.timestamp).total_seconds() / 3600 if latest else None
    latest_out = None if latest is None else {
        "timestamp": latest.timestamp.astimezone(UTC),
        "temperature": latest.temperature,
        "humidity": latest.humidity,
        "age_minutes": int(round(age_hours * 60)),
    }

    hutton_row = _score_row(session, node.node_id, "hutton", 0, for_date=today)
    hutton_today = {
        "for_date": today,
        "status": hutton_row.status if hutton_row else "not_computed",
        "valid_hours": hutton_row.valid_hours if hutton_row else None,
        "hutton_day": hutton_row.hutton_day if hutton_row else None,
        "hutton_period": hutton_row.hutton_period if hutton_row else None,
        "label": (hutton.display_label(hutton_row.status, hutton_row.valid_hours, hutton_row.score)
                  if hutton_row else "Not computed yet"),
    }

    forecast = {}
    for horizon in FORECAST_HORIZONS:
        row = _score_row(session, node.node_id, "learned", horizon, judged_only=True)
        forecast[f"p{horizon}"] = None if row is None else {
            "probability": row.score,
            "risk_level": fusion.risk_level(row.score),
            "issued_from": row.for_date,  # data up to the end of this day
            "covers": [row.for_date + timedelta(days=n) for n in range(1, horizon // 24 + 1)],
            "model_version": row.model_version,
        }

    risk = fusion.resolve_risk(session, node.node_id, today)
    level = fusion.risk_level(risk.score)
    p48 = forecast["p48"]["probability"] if forecast["p48"] else None
    return {
        "node_id": node.node_id,
        "generated_at": now,
        "latest_reading": latest_out,
        "last_24h": _last_24h(session, node.node_id, now),
        "hutton_today": hutton_today,
        "forecast": forecast,
        "risk": {"score": risk.score, "risk_level": level, "basis": risk.basis,
                 "for_date": risk.for_date, "note": risk.note},
        "summary": summary_sentence(level, risk.basis, risk.for_date, p48,
                                    hutton_row.hutton_period if hutton_row else None, age_hours),
    }


# --- history (FR viii) ------------------------------------------------------------------


def _hutton_out(row: RiskScore | None) -> dict | None:
    if row is None:
        return None
    return {"status": row.status, "score": row.score, "valid_hours": row.valid_hours,
            "hutton_day": row.hutton_day, "hutton_period": row.hutton_period,
            "label": hutton.display_label(row.status, row.valid_hours, row.score)}


def build_history(session: Session, node: SensorNode, days: int, now: datetime | None = None) -> dict:
    """The last `days` local days, newest first: risk per day, diagnoses and alerts.

    Every day in the range appears, including days with no score at all, so a
    gap shows as a gap.
    """
    now = now or datetime.now(UTC)
    today = local_today(now)
    first = today - timedelta(days=days - 1)
    since, _ = hutton.local_day_bounds(first)

    rows = session.scalars(select(RiskScore).where(
        RiskScore.node_id == node.node_id, RiskScore.for_date >= first, RiskScore.for_date <= today,
    )).all()
    by_key = {(r.for_date, r.method, r.data_source, r.horizon_hours): r for r in rows}

    risk_days = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        sensor = by_key.get((day, "hutton", "sensor", 0))
        risk_days.append({
            "for_date": day,
            "hutton": {"sensor": _hutton_out(sensor), "api": _hutton_out(by_key.get((day, "hutton", "api", 0)))},
            "learned": {
                f"p{h}": {s: (by_key[(day, "learned", s, h)].score if (day, "learned", s, h) in by_key else None)
                          for s in ("sensor", "api")}
                for h in FORECAST_HORIZONS
            },
            "risk_level": (fusion.risk_level(sensor.score)
                           if sensor is not None and sensor.score is not None else None),
        })

    diagnoses = session.execute(
        select(DiagnosisResult, LeafImage.capture_date)
        .join(LeafImage, LeafImage.image_id == DiagnosisResult.image_id)
        .where(LeafImage.farmer_id == node.farmer_id, DiagnosisResult.timestamp >= since)
        .order_by(DiagnosisResult.timestamp.desc())
    ).all()
    alerts = session.scalars(
        select(Alert).where(Alert.node_id == node.node_id, Alert.sent_at >= since)
        .order_by(Alert.sent_at.desc())
    ).all()

    return {
        "node_id": node.node_id,
        "from_date": first,
        "to_date": today,
        "risk": risk_days,
        "diagnoses": [{
            "result_id": d.result_id, "image_id": d.image_id, "timestamp": d.timestamp,
            "capture_date": captured, "disease_label": d.disease_label, "confidence": d.confidence,
            "risk_level": d.risk_level, "late_blight_flagged": d.late_blight_flagged,
        } for d, captured in diagnoses],
        "alerts": [{
            "alert_id": a.alert_id, "alert_type": a.alert_type, "message": a.message,
            "sent_at": a.sent_at, "delivery_status": a.delivery_status,
        } for a in alerts],
    }
