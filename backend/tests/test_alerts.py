"""Tests for alert decisions, especially around days we could not judge."""
from datetime import date, timedelta

from backend.alerts import (
    ALERT_HIGH_RISK,
    ALERT_LOW_RISK,
    COOLDOWN_DAYS,
    AlertState,
    decide_alert,
)
from backend.hutton import STATUS_INSUFFICIENT, STATUS_NO_DATA, STATUS_OK

DAY = date(2026, 3, 10)


def test_hutton_period_sends_a_high_risk_alert():
    decision = decide_alert(DAY, STATUS_OK, True, True, AlertState())

    assert decision.alert_type == ALERT_HIGH_RISK
    assert decision.state.last_high_risk_date == DAY
    assert "checking your potato leaves" in decision.message


def test_no_alert_from_an_insufficient_day():
    decision = decide_alert(DAY, STATUS_INSUFFICIENT, None, None, AlertState())

    assert decision.alert_type is None
    assert decision.message is None


def test_no_alert_from_a_day_with_no_readings():
    assert decide_alert(DAY, STATUS_NO_DATA, None, None, AlertState()).alert_type is None


def test_insufficient_day_does_not_clear_a_standing_warning():
    state = AlertState(last_high_risk_date=DAY)

    decision = decide_alert(DAY + timedelta(days=1), STATUS_INSUFFICIENT, None, None, state)

    # The warning still stands: a silent node is not an all-clear.
    assert decision.alert_type is None
    assert decision.state.last_high_risk_date == DAY


def test_insufficient_day_does_not_reset_the_cooldown():
    state = AlertState(last_high_risk_date=DAY)

    # A gap day, then blight weather again the next day.
    after_gap = decide_alert(DAY + timedelta(days=1), STATUS_INSUFFICIENT, None, None, state)
    decision = decide_alert(DAY + timedelta(days=2), STATUS_OK, True, True, after_gap.state)

    # Still inside the cooldown that began on DAY, so no repeat SMS.
    assert decision.alert_type is None
    assert decision.state.last_high_risk_date == DAY


def test_high_risk_repeats_once_the_cooldown_has_passed():
    state = AlertState(last_high_risk_date=DAY)
    later = DAY + timedelta(days=COOLDOWN_DAYS)

    decision = decide_alert(later, STATUS_OK, True, True, state)

    assert decision.alert_type == ALERT_HIGH_RISK
    assert decision.state.last_high_risk_date == later


def test_judged_calm_day_clears_a_standing_warning():
    state = AlertState(last_high_risk_date=DAY)

    decision = decide_alert(DAY + timedelta(days=1), STATUS_OK, False, False, state)

    assert decision.alert_type == ALERT_LOW_RISK
    assert decision.state.last_high_risk_date is None


def test_no_low_risk_message_when_no_warning_is_standing():
    decision = decide_alert(DAY, STATUS_OK, False, False, AlertState())

    assert decision.alert_type is None


def test_unknown_period_on_a_judged_day_sends_nothing():
    # Today is judged, but yesterday was not, so the period is unknown.
    decision = decide_alert(DAY, STATUS_OK, True, None, AlertState())

    assert decision.alert_type is None
