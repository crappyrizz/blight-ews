"""Tests for fusion: the threshold rule, the choice of r, and the stored diagnosis."""
from datetime import UTC, date, datetime, timedelta

import pytest

from backend import fusion
from backend.config import settings
from backend.fusion import (
    BASIS_FALLBACK,
    BASIS_LEARNED,
    BASIS_NEUTRAL_PRIOR,
    BASIS_TODAY,
    RiskContext,
    fuse,
    resolve_risk,
    threshold_for,
)
from backend.hutton import LOCAL_TZ, STATUS_INSUFFICIENT, STATUS_NO_DATA, STATUS_OK
from backend.models import (
    DiagnosisResult,
    Farmer,
    LeafImage,
    ReadingContribution,
    RiskScore,
    SensorNode,
    SensorReading,
)

TODAY = date(2026, 3, 10)
BORDERLINE = {"late_blight": 0.55, "early_blight": 0.30, "healthy": 0.15}


def make_node(session, phone="+254799000002"):
    farmer = Farmer(name="T", phone_number=phone, password_hash="x", role="farmer")
    session.add(farmer)
    session.flush()
    node = SensorNode(farmer_id=farmer.farmer_id, location="p", latitude=-0.7, longitude=36.6)
    session.add(node)
    session.flush()
    return node


def add_row(session, node, for_date, status, score, valid_hours=24, method="hutton",
            horizon=0, data_source="sensor"):
    session.add(RiskScore(
        node_id=node.node_id, method=method, data_source=data_source, horizon_hours=horizon,
        status=status, score=score, valid_hours=valid_hours, for_date=for_date,
    ))
    session.flush()


def risk(r, basis=BASIS_TODAY):
    return RiskContext(r, basis, TODAY, "")


# --- tau(r) -------------------------------------------------------------------


def test_constants_come_from_config():
    assert (fusion.TAU0, fusion.K, fusion.TAU_MIN, fusion.TAU_MAX) == (0.60, 0.40, 0.35, 0.85)
    assert fusion.TAU0 == settings.FUSION_TAU0


def test_neutral_risk_gives_the_base_threshold():
    assert threshold_for(0.5) == pytest.approx(0.60)


def test_threshold_values_at_the_ends_of_the_risk_scale():
    assert threshold_for(0.9) == pytest.approx(0.44)  # 0.60 - 0.40 * 0.4
    assert threshold_for(0.1) == pytest.approx(0.76)  # 0.60 + 0.40 * 0.4
    assert threshold_for(1.0) < threshold_for(0.5) < threshold_for(0.0)


def test_tau_is_clamped_at_both_ends():
    # r outside [0, 1] (which a bug upstream could produce) is held to the bounds.
    assert threshold_for(-5) == fusion.TAU_MAX == 0.85
    assert threshold_for(5) == fusion.TAU_MIN == 0.35


def test_clamps_bind_when_k_is_retuned_steeper(monkeypatch):
    monkeypatch.setattr(fusion, "K", 1.0)  # bar would run 1.10 .. 0.10 without the clip
    assert threshold_for(0.0) == 0.85
    assert threshold_for(1.0) == 0.35


def test_with_the_chosen_constants_tau_stays_inside_the_bounds_for_any_probability():
    """The clip is a safety net: for r in [0, 1], tau runs 0.80 .. 0.40."""
    taus = [threshold_for(r / 100) for r in range(101)]
    assert min(taus) == pytest.approx(0.40)
    assert max(taus) == pytest.approx(0.80)


# --- fusing with the CNN -------------------------------------------------------


def test_same_leaf_is_flagged_after_blight_weather_and_not_after_dry_weather():
    high = fuse(BORDERLINE, risk(0.9))
    low = fuse(BORDERLINE, risk(0.1))

    assert high.late_blight_flagged is True  # 0.55 >= 0.44
    assert low.late_blight_flagged is False  # 0.55 <  0.76
    assert high.threshold_used == pytest.approx(0.44)
    assert low.threshold_used == pytest.approx(0.76)
    # The CNN's own answer is reported unchanged either way.
    assert high.label == low.label == "late_blight"
    assert high.confidence == low.confidence == 0.55


def test_only_late_blight_is_gated():
    early = {"early_blight": 0.80, "late_blight": 0.15, "healthy": 0.05}
    healthy = {"healthy": 0.70, "late_blight": 0.20, "early_blight": 0.10}

    for probabilities, label in ((early, "early_blight"), (healthy, "healthy")):
        result = fuse(probabilities, risk(1.0))  # even at the highest risk
        assert result.label == label
        assert result.confidence == probabilities[label]
        assert result.late_blight_flagged is False  # p_late is below the lowest bar (0.40)


def test_a_late_blight_probability_below_the_top_label_can_still_be_flagged():
    """Fusion gates p_late itself, not the top label."""
    mixed = {"early_blight": 0.50, "late_blight": 0.45, "healthy": 0.05}
    result = fuse(mixed, risk(0.9))
    assert result.label == "early_blight"
    assert result.late_blight_flagged is True  # 0.45 >= 0.44


@pytest.mark.parametrize("r, level", [(0.1, "low"), (0.33, "medium"), (0.65, "medium"), (0.66, "high")])
def test_risk_level_uses_the_dashboard_cut_offs(r, level):
    assert fuse(BORDERLINE, risk(r)).risk_level == level


@pytest.mark.parametrize("r", [0.0, 0.5, 1.0])
@pytest.mark.parametrize("probabilities", [
    BORDERLINE, {"late_blight": 0.95, "early_blight": 0.03, "healthy": 0.02},
    {"early_blight": 0.9, "late_blight": 0.05, "healthy": 0.05},
    {"healthy": 0.9, "late_blight": 0.05, "early_blight": 0.05},
])
@pytest.mark.parametrize("basis", [BASIS_LEARNED, BASIS_NEUTRAL_PRIOR])
def test_summary_is_decision_support_never_an_instruction(r, probabilities, basis):
    text = fuse(probabilities, risk(r, basis)).summary.lower()
    assert "spray" not in text and "must" not in text


def test_summary_says_when_the_photo_alone_decided():
    text = fuse(BORDERLINE, risk(0.5, BASIS_NEUTRAL_PRIOR)).summary
    assert "photo alone" in text


# --- choosing r ------------------------------------------------------------------


def test_learned_24h_forecast_covering_the_day_is_preferred(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY, STATUS_OK, 0.6)  # Hutton for today
    add_row(db_session, node, TODAY - timedelta(days=1), STATUS_OK, 0.83, method="learned", horizon=24)

    r = resolve_risk(db_session, node.node_id, TODAY)

    assert (r.score, r.basis, r.for_date) == (0.83, BASIS_LEARNED, TODAY)
    assert str(TODAY - timedelta(days=1)) in r.note  # says which day's data made the forecast


def test_hutton_fallback_when_no_learned_score_exists(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY, STATUS_OK, 0.6)

    r = resolve_risk(db_session, node.node_id, TODAY)

    assert (r.score, r.basis) == (0.6, BASIS_TODAY)


def test_an_unjudged_learned_row_is_not_used(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY - timedelta(days=1), STATUS_INSUFFICIENT, None,
            valid_hours=12, method="learned", horizon=24)
    add_row(db_session, node, TODAY, STATUS_OK, 1.0)

    assert resolve_risk(db_session, node.node_id, TODAY).basis == BASIS_TODAY


def test_a_learned_forecast_for_another_day_is_not_used(db_session):
    node = make_node(db_session)
    # Issued two days ago: it covers yesterday, not today.
    add_row(db_session, node, TODAY - timedelta(days=2), STATUS_OK, 0.9, method="learned", horizon=24)
    add_row(db_session, node, TODAY, STATUS_OK, 0.0)
    assert resolve_risk(db_session, node.node_id, TODAY).basis == BASIS_TODAY


def test_api_rows_never_stand_in_for_the_plot(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY - timedelta(days=1), STATUS_OK, 0.9,
            method="learned", horizon=24, data_source="api")
    add_row(db_session, node, TODAY, STATUS_OK, 1.0, data_source="api")
    assert resolve_risk(db_session, node.node_id, TODAY).basis == BASIS_NEUTRAL_PRIOR


def test_falls_back_to_the_most_recent_judged_hutton_day(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY, STATUS_INSUFFICIENT, None, valid_hours=12)
    add_row(db_session, node, TODAY - timedelta(days=1), STATUS_NO_DATA, None, valid_hours=0)
    add_row(db_session, node, TODAY - timedelta(days=2), STATUS_OK, 0.6)
    add_row(db_session, node, TODAY - timedelta(days=3), STATUS_OK, 0.0)

    r = resolve_risk(db_session, node.node_id, TODAY)

    assert (r.score, r.basis, r.for_date) == (0.6, BASIS_FALLBACK, TODAY - timedelta(days=2))


def test_ignores_judged_days_older_than_the_fallback_window(db_session):
    node = make_node(db_session)
    add_row(db_session, node, TODAY - timedelta(days=fusion.FALLBACK_DAYS + 1), STATUS_OK, 1.0)
    r = resolve_risk(db_session, node.node_id, TODAY)
    assert (r.score, r.basis) == (fusion.NEUTRAL_PRIOR, BASIS_NEUTRAL_PRIOR)


def test_no_node_means_the_neutral_prior_and_says_why(db_session):
    r = resolve_risk(db_session, None, TODAY)
    assert (r.score, r.basis) == (0.5, BASIS_NEUTRAL_PRIOR)
    assert "No sensor node" in r.note


# --- the stored diagnosis -----------------------------------------------------------


def test_diagnose_writes_the_result_and_links_48h_of_readings(db_session):
    node = make_node(db_session)
    taken_at = datetime(2026, 3, 10, 9, 0, tzinfo=LOCAL_TZ).astimezone(UTC)
    offsets = {"too_old": -49, "inside_old": -47, "inside_recent": -1, "at_photo": 0, "after": 2}
    readings = {}
    for name, hours in offsets.items():
        reading = SensorReading(node_id=node.node_id, timestamp=taken_at + timedelta(hours=hours),
                                temperature=12.0, humidity=95.0)
        db_session.add(reading)
        db_session.flush()
        readings[name] = reading.reading_id
    image = LeafImage(farmer_id=node.farmer_id, file_path="x.jpg", capture_date=taken_at)
    db_session.add(image)
    db_session.flush()
    add_row(db_session, node, TODAY - timedelta(days=1), STATUS_OK, 0.9, method="learned", horizon=24)

    row, result, count = fusion.diagnose(db_session, image.image_id, node.node_id, taken_at, BORDERLINE)

    stored = db_session.get(DiagnosisResult, row.result_id)
    assert stored.risk_basis == BASIS_LEARNED
    assert stored.risk_score_used == 0.9
    assert stored.threshold_used == pytest.approx(0.44)
    assert stored.late_blight_flagged is True
    assert stored.risk_for_date == TODAY  # the photo's own local day
    linked = {c.reading_id for c in db_session.query(ReadingContribution).filter_by(result_id=row.result_id)}
    assert linked == {readings["inside_old"], readings["inside_recent"], readings["at_photo"]}
    assert count == 3


def test_diagnose_without_a_node_links_nothing(db_session):
    node = make_node(db_session)
    image = LeafImage(farmer_id=node.farmer_id, file_path="x.jpg", capture_date=datetime.now(UTC))
    db_session.add(image)
    db_session.flush()

    row, result, count = fusion.diagnose(db_session, image.image_id, None, image.capture_date, BORDERLINE)

    assert row.risk_basis == BASIS_NEUTRAL_PRIOR
    assert count == 0


# --- ml/fusion_eval.py (pure parts; no model needed) ------------------------------------


def test_sweep_counts_flags_against_the_true_labels():
    import numpy as np

    from ml import fusion_eval

    is_late = np.array([True, True, True, False, False])
    p_late = np.array([0.95, 0.55, 0.30, 0.50, 0.02])

    rows = {row["r"]: row for row in fusion_eval.sweep(is_late, p_late, (0.1, 0.9))}

    # r = 0.1, tau 0.76: only the 0.95 image is flagged.
    assert (rows[0.1]["tp"], rows[0.1]["fp"], rows[0.1]["fn"]) == (1, 0, 2)
    assert rows[0.1]["precision"] == 1.0 and rows[0.1]["recall"] == pytest.approx(1 / 3)
    # r = 0.9, tau 0.44: 0.95, 0.55 (late) and 0.50 (not late) are flagged.
    assert (rows[0.9]["tp"], rows[0.9]["fp"], rows[0.9]["fn"]) == (2, 1, 1)
    assert rows[0.9]["recall"] > rows[0.1]["recall"]
    assert rows[0.9]["precision"] <= rows[0.1]["precision"]


def test_borderline_images_are_those_between_the_extreme_thresholds():
    import numpy as np

    from ml import fusion_eval

    p_late = np.array([0.30, 0.44, 0.60, 0.759, 0.76, 0.99])
    mask = fusion_eval.borderline(p_late, (0.1, 0.9))  # tau 0.76 .. 0.44
    assert mask.tolist() == [False, True, True, True, False, False]


def test_nothing_flagged_gives_unmeasurable_precision_not_zero():
    import numpy as np

    from ml import fusion_eval

    row = fusion_eval.sweep(np.array([True, False]), np.array([0.1, 0.1]), (0.5,))[0]
    assert row["precision"] is None
    assert row["recall"] == 0.0
