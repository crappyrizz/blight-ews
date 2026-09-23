"""Deciding whether a day is worth an SMS, and what it should say.

This module only decides. Sending belongs to sms.py.

Two rules matter most, and both exist because of missing data:

1. A day we could not judge never produces a "low risk" message. Telling the
   farmer conditions are calm, when really the node was offline, is the one
   mistake that would cost him a crop and cost us his trust.
2. A day we could not judge never changes the high-risk state either. If a
   warning is standing and the node then goes quiet, the warning stays
   standing until a *judged* calm day clears it.

All wording is decision support: what the weather did, and what he might
want to check. Never an instruction to spray.
"""
from dataclasses import dataclass, replace
from datetime import date, timedelta

from backend.hutton import STATUS_OK

# Don't repeat a high-risk SMS for this many days; blight weather often runs
# in spells and daily repeats would train him to ignore the messages.
COOLDOWN_DAYS = 3

ALERT_HIGH_RISK = "high_risk"
ALERT_LOW_RISK = "low_risk"


@dataclass
class AlertState:
    """What the alerting has said so far, carried between days."""

    last_high_risk_date: date | None = None  # None = no standing warning


@dataclass
class AlertDecision:
    alert_type: str | None  # None = send nothing
    message: str | None
    state: AlertState  # state to store for next time
    reason: str  # why, for the logs and for the write-up


def decide_alert(
    for_date: date,
    status: str,
    hutton_day: bool | None,
    hutton_period: bool | None,
    state: AlertState,
) -> AlertDecision:
    """Decide the alert for one day. Pure function: no database, no SMS."""
    if status != STATUS_OK:
        # Unjudged day: say nothing, change nothing. In particular this does
        # NOT clear a standing high-risk warning, and must never be reported
        # as a calm day.
        return AlertDecision(
            alert_type=None,
            message=None,
            state=state,
            reason=f"{for_date}: day not judged ({status}); no alert, state unchanged",
        )

    if hutton_period:
        standing = state.last_high_risk_date
        if standing is not None and for_date - standing < timedelta(days=COOLDOWN_DAYS):
            return AlertDecision(
                alert_type=None,
                message=None,
                state=state,
                reason=f"{for_date}: high risk, but within {COOLDOWN_DAYS}-day cooldown since {standing}",
            )
        return AlertDecision(
            alert_type=ALERT_HIGH_RISK,
            message=(
                f"Blight alert {for_date}: two days running of blight weather "
                "(cool nights, long damp spells) at your plot. Worth checking your "
                "potato leaves for spots in the next day or two."
            ),
            state=replace(state, last_high_risk_date=for_date),
            reason=f"{for_date}: Hutton period confirmed",
        )

    if state.last_high_risk_date is not None and hutton_day is False:
        # A judged calm day, and a warning was standing: an all-clear is
        # useful now, and only now.
        return AlertDecision(
            alert_type=ALERT_LOW_RISK,
            message=(
                f"Update {for_date}: blight weather has eased at your plot. "
                "Keep checking leaves as usual."
            ),
            state=replace(state, last_high_risk_date=None),
            reason=f"{for_date}: judged calm day ends the standing warning",
        )

    return AlertDecision(
        alert_type=None,
        message=None,
        state=state,
        reason=f"{for_date}: nothing worth sending",
    )
