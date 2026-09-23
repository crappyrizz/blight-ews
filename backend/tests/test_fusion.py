"""Tests for the threshold rule and for what fusion does when data is missing."""
from datetime import date, timedelta

from backend import fusion
from backend.fusion import (
    BASIS_FALLBACK,
    BASIS_NEUTRAL_PRIOR,
    BASIS_TODAY,
    RiskContext,
    fuse,
    resolve_risk,
    threshold_for,
)
from backend.hutton import STATUS_INSUFFICIENT, STATUS_NO_DATA, STATUS_OK
from backend.models import Farmer, RiskScore, SensorNode

TODAY = date(2026, 3, 10)


def make_node(session):
    farmer = Farmer(name="T", phone_number="+254799000002", password_hash="x", role="farmer")
    session.add(farmer)
    session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location="p", latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def add_row(session, node, for_date, status, score, valid_hours=24):
    session.add(RiskScore(
        node_id=node.node_id, method="hutton", data_source="sensor", horizon_hours=0,
        status=status, score=score, valid_hours=valid_hours, for_date=for_date,
    ))
    session.flush()


# --- tau(r) ------------------------------------------------------------


def test_neutral_risk_gives_the_base_threshold():
    assert threshold_for(0.5) == fusion.TAU0


def test_high_risk_lowers_the_bar_and_low_risk_raises_it():
    assert threshold_for(1.0) < fusion.TAU0 < threshold_for(0.0)


def test_threshold_is_clipped_to_its_range():
    assert threshold_for(-5) == fusion.TAU_MAX
    assert threshold_for(5) == fusion.TAU_MIN


# --- choosing the risk number -----------------------------------------


def test_uses_todays_score_when_it_is_usable(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY, STATUS_OK, 1.0)
    add_row(db_session, node, TODAY - timedelta(days=1), STATUS_OK, 0.0)

    risk = resolve_risk(db_session, node.node_id, TODAY)

    assert (risk.basis, risk.score, risk.for_date) == (BASIS_TODAY, 1.0, TODAY)


def test_falls_back_to_the_most_recent_usable_day(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY, STATUS_INSUFFICIENT, None, valid_hours=12)
    add_row(db_session, node, TODAY - timedelta(days=1), STATUS_NO_DATA, None, valid_hours=0)
    add_row(db_session, node, TODAY - timedelta(days=2), STATUS_OK, 0.6)  # the one to use
    add_row(db_session, node, TODAY - timedelta(days=3), STATUS_OK, 0.0)  # older, ignored

    risk = resolve_risk(db_session, node.node_id, TODAY)

    assert risk.basis == BASIS_FALLBACK
    assert risk.score == 0.6
    assert risk.for_date == TODAY - timedelta(days=2)  # the right date is recorded
    assert "2026-03-08" in risk.note


def test_ignores_usable_days_older_than_the_fallback_window(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY, STATUS_NO_DATA, None, valid_hours=0)
    add_row(db_session, node, TODAY - timedelta(days=4), STATUS_OK, 1.0)  # too old

    risk = resolve_risk(db_session, node.node_id, TODAY)

    assert risk.basis == BASIS_NEUTRAL_PRIOR
    assert risk.score == fusion.NEUTRAL_PRIOR
    assert risk.for_date is None


def test_neutral_prior_is_explained_in_the_response(db_session):
    node = make_node(db_session)
    risk = resolve_risk(db_session, node.node_id, TODAY)  # no rows at all

    assert risk.basis == BASIS_NEUTRAL_PRIOR
    assert "neutral risk of 0.5" in risk.note
    assert "leaf image alone" in risk.note


# --- fusing with the CNN ----------------------------------------------


def test_high_risk_flags_a_borderline_leaf_that_neutral_risk_would_not():
    probabilities = {"late_blight": 0.55, "early_blight": 0.25, "healthy": 0.20}
    high = fuse(probabilities, RiskContext(1.0, BASIS_TODAY, TODAY, ""))
    neutral = fuse(probabilities, RiskContext(0.5, BASIS_NEUTRAL_PRIOR, None, ""))

    assert high.late_blight_flagged is True
    assert neutral.late_blight_flagged is False
    assert high.threshold_used < neutral.threshold_used


def test_fusion_reports_which_day_the_risk_came_from():
    borrowed = TODAY - timedelta(days=2)
    result = fuse(
        {"late_blight": 0.9, "early_blight": 0.05, "healthy": 0.05},
        RiskContext(0.6, BASIS_FALLBACK, borrowed, "Using the risk from 2026-03-08."),
    )

    assert result.risk_basis == BASIS_FALLBACK
    assert result.risk_for_date == borrowed
    assert result.label == "late_blight"
    assert result.risk_score_used == 0.6
