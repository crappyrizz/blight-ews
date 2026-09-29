"""Fusion: let the weather risk shift how sure the CNN must be.

The CNN gives a late-blight probability. On its own it needs a fixed
confidence threshold. Fusion moves that threshold with the weather risk
r (0-1):

    tau(r) = clip(tau0 - k * (r - 0.5), tau_min, tau_max)

At r = 0.5 (neutral) the threshold is tau0. High risk lowers the bar,
because a late-blight looking leaf during blight weather is more likely to
really be blight; low risk raises it. The result is decision support: a flag
for the farmer to check, never an instruction to spray.

Missing weather data must not silently become "low risk". If today has no
usable score, we fall back to the most recent usable day within the last
three days and say which date that was; failing that we use the neutral
prior r = 0.5 and say so.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import RiskScore

# --- Threshold rule ----------------------------------------------------
# Starting values, to be tuned against the cost of a missed outbreak versus
# a false alarm in ml/cost_threshold.py.
TAU0 = 0.60  # threshold at neutral risk
K = 0.20  # how strongly risk moves the threshold
TAU_MIN = 0.40  # never demand less confidence than this
TAU_MAX = 0.80  # never demand more than this

NEUTRAL_PRIOR = 0.5  # what we use when the weather is simply unknown
FALLBACK_DAYS = 3  # how far back a usable day may be reused

# --- Risk-level bands (shared with the dashboard wording) --------------
MEDIUM_RISK_FROM = 0.34
HIGH_RISK_FROM = 0.67

# Where the risk number came from.
BASIS_TODAY = "today"
BASIS_FALLBACK = "fallback"
BASIS_NEUTRAL_PRIOR = "neutral_prior"


@dataclass
class RiskContext:
    """The risk number used, and an honest account of where it came from."""

    score: float
    basis: str
    for_date: date | None  # the day the score actually describes
    note: str  # plain wording for the API response and the app


@dataclass
class FusionResult:
    label: str
    confidence: float
    threshold_used: float
    late_blight_flagged: bool
    risk_level: str
    risk_score_used: float
    risk_basis: str
    risk_for_date: date | None
    note: str


def threshold_for(risk: float) -> float:
    """tau(r), clipped to its allowed range."""
    tau = TAU0 - K * (risk - 0.5)
    return min(max(tau, TAU_MIN), TAU_MAX)


def risk_level(score: float) -> str:
    """Band a 0-1 score into low / medium / high."""
    if score >= HIGH_RISK_FROM:
        return "high"
    if score >= MEDIUM_RISK_FROM:
        return "medium"
    return "low"


def resolve_risk(session: Session, node_id: str, on_date: date) -> RiskContext:
    """Pick the risk number to fuse with, preferring today's own score.

    Order: today's usable score, else the most recent usable day within
    FALLBACK_DAYS, else the neutral prior. A day with status other than 'ok'
    has score NULL and is skipped here, never read as 0.0.
    """
    usable = (
        select(RiskScore)
        .where(
            RiskScore.node_id == node_id,
            RiskScore.method == "hutton",
            RiskScore.horizon_hours == 0,
            RiskScore.status == "ok",
            RiskScore.score.is_not(None),
        )
        .order_by(RiskScore.for_date.desc())
    )

    today = session.scalar(usable.where(RiskScore.for_date == on_date))
    if today is not None:
        return RiskContext(
            score=today.score,
            basis=BASIS_TODAY,
            for_date=on_date,
            note="Risk from today's readings.",
        )

    earliest = on_date - timedelta(days=FALLBACK_DAYS)
    recent = session.scalars(
        usable.where(RiskScore.for_date < on_date, RiskScore.for_date >= earliest)
    ).first()
    if recent is not None:
        return RiskContext(
            score=recent.score,
            basis=BASIS_FALLBACK,
            for_date=recent.for_date,
            note=(
                f"No complete readings for {on_date}. "
                f"Using the risk from {recent.for_date}."
            ),
        )

    return RiskContext(
        score=NEUTRAL_PRIOR,
        basis=BASIS_NEUTRAL_PRIOR,
        for_date=None,
        note=(
            f"No complete readings in the last {FALLBACK_DAYS} days. "
            "Using a neutral risk of 0.5, so this diagnosis rests on the leaf image alone."
        ),
    )


def fuse(probabilities: dict[str, float], risk: RiskContext) -> FusionResult:
    """Combine CNN class probabilities with the weather risk.

    The predicted label is still the CNN's most likely class; the threshold
    only decides whether late blight is *flagged* for the farmer's attention.
    """
    label = max(probabilities, key=probabilities.get)
    confidence = probabilities[label]
    threshold = threshold_for(risk.score)
    late_blight_flagged = probabilities.get("late_blight", 0.0) >= threshold

    return FusionResult(
        label=label,
        confidence=confidence,
        threshold_used=threshold,
        late_blight_flagged=late_blight_flagged,
        risk_level=risk_level(risk.score),
        risk_score_used=risk.score,
        risk_basis=risk.basis,
        risk_for_date=risk.for_date,
        note=risk.note,
    )
