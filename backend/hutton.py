"""Hutton Criteria: the baseline blight-risk rule, and how gaps are handled.

The rule (Hutton Criteria, the successor to the Smith Period):

- A "Hutton day" is a local calendar day (Africa/Nairobi) with
  minimum temperature >= 10.0 C AND at least 6 hours with relative
  humidity >= 90%.
- A "Hutton period" is two consecutive Hutton days. It is flagged on the
  second day, because that is when the warning becomes meaningful.

Missing data is the other half of this module. A day needs at least 20 of
its 24 hourly values to be judged at all. A day with fewer is recorded as
'insufficient_data' with score NULL, not as a low-risk day, and it is
written to the database rather than skipped, so a gap is visible as a gap in
the farmer's history instead of simply being absent. Gaps are never
interpolated.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import RiskScore, SensorReading

# --- Hutton thresholds -------------------------------------------------
MIN_TEMPERATURE_C = 10.0  # daily minimum temperature must reach this
RH_THRESHOLD_PCT = 90.0  # "high humidity" means at or above this
MIN_HOURS_AT_RH = 6  # ... for at least this many hours in the day

# --- Data-completeness rule --------------------------------------------
HOURS_IN_DAY = 24
MIN_VALID_HOURS = 20  # fewer than this and the day cannot be judged

# --- Row status values (mirror the CHECK constraint on risk_scores) ----
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_NO_DATA = "no_data"

# --- Score attached to a judged day ------------------------------------
# The Hutton rule is a set of yes/no conditions, so these are conventions
# for expressing it on the 0-1 scale that fusion and the dashboard use.
# A day only ever gets one of these three values; an unjudged day gets NULL.
SCORE_NO_HUTTON_DAY = 0.0
SCORE_HUTTON_DAY = 0.6  # conditions met today, but not yet two days running
SCORE_HUTTON_PERIOD = 1.0  # second consecutive Hutton day: the real warning

LOCAL_TZ = ZoneInfo(settings.TIMEZONE)


@dataclass
class DaySummary:
    """What one local calendar day of hourly data adds up to."""

    for_date: date
    status: str
    valid_hours: int
    hutton_day: bool | None  # None = unknown, never False on missing data
    min_temperature: float | None = None
    hours_at_high_humidity: int | None = None


def summarise_day(for_date: date, readings) -> DaySummary:
    """Apply the Hutton day test to one local day's hourly readings.

    `readings` is any iterable of objects with .timestamp, .temperature and
    .humidity. Only one value per clock hour counts, so a node that reports
    twice in an hour cannot inflate its own completeness.
    """
    by_hour = {}
    for reading in readings:
        if reading.temperature is None or reading.humidity is None:
            continue  # a half-reading tells us nothing about that hour
        local_hour = reading.timestamp.astimezone(LOCAL_TZ).hour
        by_hour.setdefault(local_hour, (reading.temperature, reading.humidity))

    valid_hours = len(by_hour)

    if valid_hours == 0:
        return DaySummary(for_date, STATUS_NO_DATA, 0, None)

    if valid_hours < MIN_VALID_HOURS:
        # Known unknown: we say so, rather than guessing at the missing hours.
        return DaySummary(for_date, STATUS_INSUFFICIENT, valid_hours, None)

    temperatures = [t for t, _ in by_hour.values()]
    humidities = [h for _, h in by_hour.values()]
    min_temperature = min(temperatures)
    hours_at_high_humidity = sum(1 for h in humidities if h >= RH_THRESHOLD_PCT)

    hutton_day = (
        min_temperature >= MIN_TEMPERATURE_C
        and hours_at_high_humidity >= MIN_HOURS_AT_RH
    )
    return DaySummary(
        for_date=for_date,
        status=STATUS_OK,
        valid_hours=valid_hours,
        hutton_day=hutton_day,
        min_temperature=min_temperature,
        hours_at_high_humidity=hours_at_high_humidity,
    )


def hutton_period(previous: DaySummary | None, today: DaySummary) -> bool | None:
    """Two consecutive Hutton days, flagged on the second day.

    Returns None (unknown) if either day could not be judged: a period is a
    claim about both days, and one unknown day makes the claim unknown. It is
    never False just because yesterday's data is missing.
    """
    if today.hutton_day is None:
        return None
    if previous is None or previous.hutton_day is None:
        return None
    return previous.hutton_day and today.hutton_day


def score_for(summary: DaySummary, period: bool | None) -> float | None:
    """Map a judged day onto the 0-1 risk scale. Unjudged days get NULL.

    Never substitute 0.0 for an unknown day: 0.0 states that conditions do
    not favour blight, which we cannot know from missing data, and both the
    dashboard and the learned model would read it as a genuinely safe day.
    """
    if summary.status != STATUS_OK:
        return None
    if period:
        return SCORE_HUTTON_PERIOD
    if summary.hutton_day:
        return SCORE_HUTTON_DAY
    return SCORE_NO_HUTTON_DAY


def display_label(status: str, valid_hours: int | None, score: float | None) -> str:
    """Wording for the dashboard. An unjudged day never reads as 'Low risk'."""
    if status == STATUS_NO_DATA:
        return "No reading"
    if status == STATUS_INSUFFICIENT:
        return f"Incomplete data ({valid_hours} of {HOURS_IN_DAY} hours)"

    # Imported here to keep the risk-level bands in one place (fusion.py)
    # without the two modules importing each other at module level.
    from backend.fusion import risk_level

    return {"low": "Low risk", "medium": "Medium risk", "high": "High risk"}[
        risk_level(score)
    ]


# --- Database helpers --------------------------------------------------


def local_day_bounds(for_date: date) -> tuple[datetime, datetime]:
    """UTC start and end instants of a local (Africa/Nairobi) calendar day.

    Readings are stored in UTC, but a "day" for the Hutton rule is a local
    day, so the window is shifted by the local offset (UTC+3 here).
    """
    start_local = datetime.combine(for_date, time.min, tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local


def compute_day(session: Session, node_id: str, for_date: date) -> DaySummary:
    """Summarise one local day of a node's sensor readings."""
    start, end = local_day_bounds(for_date)
    readings = session.scalars(
        select(SensorReading)
        .where(
            SensorReading.node_id == node_id,
            SensorReading.timestamp >= start,
            SensorReading.timestamp < end,
        )
        .order_by(SensorReading.timestamp)
    ).all()
    return summarise_day(for_date, readings)


def store_day(
    session: Session,
    node_id: str,
    summary: DaySummary,
    period: bool | None,
    data_source: str = "sensor",
) -> RiskScore:
    """Write (or update) the Hutton row for one day.

    Rows are written for unjudged days too, so the history shows the gap.
    Re-running for the same day updates the existing row instead of adding a
    second one, which makes the daily job safe to re-run.
    """
    row = session.scalar(
        select(RiskScore).where(
            RiskScore.node_id == node_id,
            RiskScore.method == "hutton",
            RiskScore.data_source == data_source,
            RiskScore.horizon_hours == 0,
            RiskScore.for_date == summary.for_date,
        )
    )
    if row is None:
        row = RiskScore(
            node_id=node_id,
            method="hutton",
            data_source=data_source,
            horizon_hours=0,
            for_date=summary.for_date,
        )
        session.add(row)

    row.status = summary.status
    row.valid_hours = summary.valid_hours
    row.hutton_day = summary.hutton_day
    row.hutton_period = period
    row.score = score_for(summary, period)
    session.flush()
    return row


def compute_and_store(
    session: Session, node_id: str, for_date: date, data_source: str = "sensor"
) -> RiskScore:
    """Run the Hutton rule for one day, including yesterday's day for the period."""
    today = compute_day(session, node_id, for_date)
    yesterday = compute_day(session, node_id, for_date - timedelta(days=1))
    return store_day(session, node_id, today, hutton_period(yesterday, today), data_source)
