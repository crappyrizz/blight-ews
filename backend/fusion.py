"""Fusion: let the weather risk decide how sure the CNN must be before late
blight is flagged.

THE IDEA. A leaf photo alone is ambiguous: early late-blight lesions can look
like other damage, so the CNN is sometimes only moderately sure. The weather
tells us something the photo cannot: whether the last day or two favoured
infection. Bayes' rule says the same evidence should count for more when the
prior is higher. Fusion applies that idea in its simplest defensible form:
it keeps the CNN's probability as it is, and MOVES THE BAR it has to clear.

    tau(r) = clip(TAU0 - K * (r - 0.5), TAU_MIN, TAU_MAX)
    late_blight_flagged = p_late >= tau(r)

- r in [0, 1] is the weather risk (below). At r = 0.5 (no information
  either way) the bar is TAU0 = 0.60.
- High risk lowers the bar; low risk raises it. With K = 0.40 the bar runs
  from 0.80 at r = 0 down to 0.40 at r = 1. So the same leaf with
  p_late = 0.55 is flagged after blight weather (r = 0.9, tau = 0.44) and not
  after dry, cold weather (r = 0.1, tau = 0.76).
- The clip to [TAU_MIN, TAU_MAX] = [0.35, 0.85] is a safety net. With these
  constants and r in [0, 1] it never actually binds (tau stays in
  [0.40, 0.80]); it only matters if TAU0 or K are re-tuned, and guarantees
  that no weather can make the system flag a leaf the CNN is barely sure of
  (below 0.35), or ignore one it is very sure of (above 0.85).
- Why a threshold shift rather than multiplying probabilities: the CNN and
  the weather model were trained separately and are not calibrated against
  each other, so a formal Bayesian product would claim more precision than
  we have. A bounded shift is transparent, monotone, and easy to explain:
  "in blight weather, the system needs less visual evidence to raise a flag".
  TAU0, K and the bounds are settings (config), to be tuned against the cost
  of a missed outbreak versus a false alarm.

ONLY LATE BLIGHT IS GATED. The CNN's label and confidence are reported as
they are, for every class. Fusion only decides whether to FLAG late blight,
because that is the fast-spreading disease this early-warning system exists
for. Early blight and healthy predictions pass through untouched.

WHICH r (resolve_risk), in order:
1. The learned model's 24 h probability that covers the photo's day (the
   forecast issued from the previous day's complete sensor data). This is
   the forward-looking estimate, so it is preferred.
2. Otherwise the Hutton score from the sensor: that day's, or the most recent
   judged day within FALLBACK_DAYS (and the date is recorded).
3. Otherwise the neutral prior r = 0.5, and the result says so: the diagnosis
   then rests on the photo alone. Missing weather never becomes "low risk".
Whichever is used is stored with the diagnosis (risk_basis, risk_for_date).

The output is decision support: something worth a closer look, never an
instruction to spray.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import hutton
from backend.config import settings
from backend.models import DiagnosisResult, ReadingContribution, RiskScore, SensorReading

# --- The threshold rule (values in config, see backend/config.py) -------
TAU0 = settings.FUSION_TAU0  # 0.60: the bar at neutral risk
K = settings.FUSION_K  # 0.40: how strongly the risk moves the bar
TAU_MIN = settings.FUSION_TAU_MIN  # 0.35: never demand less confidence than this
TAU_MAX = settings.FUSION_TAU_MAX  # 0.85: never demand more than this

NEUTRAL_PRIOR = 0.5  # what we use when the weather is simply unknown
FALLBACK_DAYS = 3  # how far back a judged Hutton day may be reused

# Sensor readings from this long before the photo are linked to the
# diagnosis (reading_contributions), so each result can be traced back to
# the weather that informed it.
CONTRIBUTION_WINDOW_HOURS = 48

# --- Risk-level bands, shared with the dashboard (config) ---------------
# low < MEDIUM_RISK_FROM <= medium < HIGH_RISK_FROM <= high
MEDIUM_RISK_FROM = settings.RISK_MEDIUM_FROM
HIGH_RISK_FROM = settings.RISK_HIGH_FROM

# Where r came from (stored in diagnosis_results.risk_basis).
BASIS_LEARNED = "learned_24h"
BASIS_TODAY = "hutton_today"
BASIS_FALLBACK = "hutton_fallback"
BASIS_NEUTRAL_PRIOR = "neutral_prior"

LATE_BLIGHT = "late_blight"


@dataclass
class RiskContext:
    """The risk number used, and an honest account of where it came from."""

    score: float
    basis: str
    for_date: date | None  # the day the score describes
    note: str  # plain wording for the API response and the app
    score_id: str | None = None  # the risk_scores row, when there is one


@dataclass
class FusionResult:
    label: str  # the CNN's most probable class, unchanged
    confidence: float  # its probability, unchanged
    p_late: float  # the CNN's late-blight probability
    threshold_used: float  # tau(r)
    late_blight_flagged: bool  # p_late >= tau(r)
    risk_level: str  # r banded into low / medium / high
    risk_score_used: float  # r
    risk_basis: str
    risk_for_date: date | None
    note: str  # where r came from, in words
    summary: str  # one decision-support sentence for the farmer


def threshold_for(risk: float) -> float:
    """tau(r) = clip(TAU0 - K * (r - 0.5), TAU_MIN, TAU_MAX)."""
    tau = TAU0 - K * (risk - 0.5)
    return min(max(tau, TAU_MIN), TAU_MAX)


def risk_level(score: float) -> str:
    """Band a 0-1 score into low / medium / high (the dashboard's cut-offs)."""
    if score >= HIGH_RISK_FROM:
        return "high"
    if score >= MEDIUM_RISK_FROM:
        return "medium"
    return "low"


# --- choosing r ------------------------------------------------------------------


def learned_risk(session: Session, node_id: str, on_date: date,
                 data_source: str = "sensor") -> RiskContext | None:
    """The learned 24 h probability covering `on_date`, or None.

    The 24 h row with for_date = D is the forecast made from data up to the
    end of D, so the one that covers `on_date` was issued the day before.
    """
    issued = on_date - timedelta(days=1)
    row = session.scalar(select(RiskScore).where(
        RiskScore.node_id == node_id,
        RiskScore.method == "learned",
        RiskScore.data_source == data_source,
        RiskScore.horizon_hours == 24,
        RiskScore.for_date == issued,
        RiskScore.status == hutton.STATUS_OK,
        RiskScore.score.is_not(None),
    ))
    if row is None:
        return None
    return RiskContext(
        score=row.score,
        basis=BASIS_LEARNED,
        for_date=on_date,
        note=f"Risk from the 24-hour forecast made from the readings of {issued}.",
        score_id=row.score_id,
    )


def hutton_risk(session: Session, node_id: str, on_date: date,
                data_source: str = "sensor") -> RiskContext:
    """The Hutton score: that day's, else a recent judged day, else neutral.

    A day with status other than 'ok' has score NULL and is skipped, never
    read as 0.0.
    """
    usable = (
        select(RiskScore)
        .where(
            RiskScore.node_id == node_id,
            RiskScore.method == "hutton",
            RiskScore.data_source == data_source,
            RiskScore.horizon_hours == 0,
            RiskScore.status == hutton.STATUS_OK,
            RiskScore.score.is_not(None),
        )
        .order_by(RiskScore.for_date.desc())
    )

    today = session.scalar(usable.where(RiskScore.for_date == on_date))
    if today is not None:
        return RiskContext(score=today.score, basis=BASIS_TODAY, for_date=on_date,
                           note="Risk from today's readings (Hutton criteria).", score_id=today.score_id)

    earliest = on_date - timedelta(days=FALLBACK_DAYS)
    recent = session.scalars(
        usable.where(RiskScore.for_date < on_date, RiskScore.for_date >= earliest)
    ).first()
    if recent is not None:
        return RiskContext(
            score=recent.score, basis=BASIS_FALLBACK, for_date=recent.for_date,
            note=f"No complete readings for {on_date}. Using the risk from {recent.for_date}.",
            score_id=recent.score_id,
        )

    return RiskContext(
        score=NEUTRAL_PRIOR, basis=BASIS_NEUTRAL_PRIOR, for_date=None,
        note=(f"No complete readings in the last {FALLBACK_DAYS} days. Using a neutral risk "
              "of 0.5, so this diagnosis rests on the leaf image alone."),
    )


def resolve_risk(session: Session, node_id: str | None, on_date: date,
                 data_source: str = "sensor") -> RiskContext:
    """r for a node on a day: learned 24 h if available, else the Hutton chain.

    Only rows from `data_source` count: the risk job also writes API-based
    rows for comparison, and those must never stand in for the plot's own.
    """
    if node_id is None:  # a farmer with no sensor node yet
        return RiskContext(
            score=NEUTRAL_PRIOR, basis=BASIS_NEUTRAL_PRIOR, for_date=None,
            note="No sensor node is linked, so this diagnosis rests on the leaf image alone.",
        )
    return (learned_risk(session, node_id, on_date, data_source)
            or hutton_risk(session, node_id, on_date, data_source))


# --- fusing with the CNN -----------------------------------------------------------


def diagnosis_sentence(label: str, confidence: float, flagged: bool, level: str, basis: str) -> str:
    """One plain, decision-support sentence about the photo. Never an instruction."""
    percent = f"{confidence:.0%}"
    if flagged:
        sentence = ("The photo shows signs that look like late blight"
                    + (", and the recent weather has favoured it" if level == "high" else "")
                    + ", so it may be worth checking the nearby plants closely.")
    elif label == LATE_BLIGHT:
        sentence = (f"The photo leans towards late blight ({percent}), but not clearly enough to "
                    "flag at the current weather risk; another photo in a day or two may help.")
    elif label == "early_blight":
        sentence = (f"The leaf looks most like early blight ({percent}). Late blight is not flagged.")
    else:
        sentence = f"The leaf looks healthy ({percent})."
        if level == "high":
            sentence += " The recent weather has favoured blight, so other plants may still be worth a look."
    if basis == BASIS_NEUTRAL_PRIOR:
        sentence += " (No recent weather readings were available, so this is based on the photo alone.)"
    return sentence


def fuse(probabilities: dict[str, float], risk: RiskContext) -> FusionResult:
    """Combine CNN class probabilities with the weather risk r.

    The label is still the CNN's most likely class; the threshold only decides
    whether late blight is FLAGGED for the farmer's attention.
    """
    label = max(probabilities, key=probabilities.get)
    confidence = probabilities[label]
    p_late = probabilities.get(LATE_BLIGHT, 0.0)
    threshold = threshold_for(risk.score)
    flagged = p_late >= threshold
    level = risk_level(risk.score)
    return FusionResult(
        label=label, confidence=confidence, p_late=p_late,
        threshold_used=threshold, late_blight_flagged=flagged,
        risk_level=level, risk_score_used=risk.score,
        risk_basis=risk.basis, risk_for_date=risk.for_date, note=risk.note,
        summary=diagnosis_sentence(label, confidence, flagged, level, risk.basis),
    )


# --- storing a diagnosis --------------------------------------------------------------


def contributing_readings(session: Session, node_id: str, taken_at: datetime) -> list[str]:
    """IDs of the node's readings in the 48 h up to the moment the photo was taken."""
    return list(session.scalars(select(SensorReading.reading_id).where(
        SensorReading.node_id == node_id,
        SensorReading.timestamp > taken_at - timedelta(hours=CONTRIBUTION_WINDOW_HOURS),
        SensorReading.timestamp <= taken_at,
    )))


def diagnose(session: Session, image_id: str, node_id: str | None, taken_at: datetime,
             probabilities: dict[str, float]) -> tuple[DiagnosisResult, FusionResult, int]:
    """Fuse, write the diagnosis_results row and its reading_contributions.

    The risk is looked up for the photo's own local day, not the upload time,
    so a photo uploaded late (weak network) is judged against its own weather.
    Flushes but does not commit: the caller commits the image and its
    diagnosis together.
    """
    on_date = taken_at.astimezone(hutton.LOCAL_TZ).date()
    result = fuse(probabilities, resolve_risk(session, node_id, on_date))

    row = DiagnosisResult(
        image_id=image_id,
        disease_label=result.label,
        confidence=result.confidence,
        probabilities=probabilities,
        risk_score_used=result.risk_score_used,
        threshold_used=result.threshold_used,
        risk_level=result.risk_level,
        late_blight_flagged=result.late_blight_flagged,
        risk_basis=result.risk_basis,
        risk_for_date=result.risk_for_date,
    )
    session.add(row)
    session.flush()  # result_id is needed for the contribution rows

    reading_ids = contributing_readings(session, node_id, taken_at) if node_id else []
    session.add_all(ReadingContribution(reading_id=r, result_id=row.result_id) for r in reading_ids)
    session.flush()
    return row, result, len(reading_ids)
