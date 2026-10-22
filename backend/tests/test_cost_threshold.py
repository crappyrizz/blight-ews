"""Tests for ml/cost_threshold.py: the costs, the threshold, and playing a policy.

No model and no database: probabilities and Hutton periods are hand-made arrays.
"""
import numpy as np
import pytest

from ml import cost_threshold as ct

NAN = np.nan


# --- costs and the threshold ---------------------------------------------------


def test_spray_cost_is_chemical_plus_labour_per_pump_load():
    assert ct.spray_cost(10) == 2500 + 50 * 10
    assert ct.spray_cost(0) == 2500


def test_loss_is_bags_times_price():
    assert ct.loss_if_missed(2500) == 50_000
    assert ct.loss_if_missed(5000) == 100_000


def test_threshold_is_spray_over_loss():
    # C_FP = 3,000 and C_FN = 75,000 - 3,000, so C_FP / (C_FP + C_FN) = 3,000 / 75,000.
    assert ct.optimal_threshold(3000, 75_000) == pytest.approx(0.04)


def test_full_loss_version_is_slightly_lower():
    assert ct.threshold_with_full_loss(3000, 75_000) == pytest.approx(3000 / 78_000)
    assert ct.threshold_with_full_loss(3000, 75_000) < ct.optimal_threshold(3000, 75_000)


def test_never_spray_when_spraying_costs_as_much_as_the_loss():
    assert ct.optimal_threshold(5000, 5000) == 1.0
    assert ct.optimal_threshold(6000, 5000) == 1.0


def test_threshold_falls_as_the_price_rises_and_rises_with_pump_loads():
    by_price = [ct.optimal_threshold(ct.spray_cost(10), ct.loss_if_missed(p)) for p in ct.prices()]
    assert by_price == sorted(by_price, reverse=True)
    assert ct.optimal_threshold(ct.spray_cost(20), 50_000) > ct.optimal_threshold(ct.spray_cost(5), 50_000)


def test_price_range_includes_both_ends():
    assert ct.prices(2500, 5000, 500) == [2500, 3000, 3500, 4000, 4500, 5000]


# --- cover and missed infections -------------------------------------------------


def test_a_spray_covers_its_day_and_the_next_six():
    protected = ct.covered(12, [2], cover_days=7)
    assert protected.tolist() == [False, False] + [True] * 7 + [False] * 3


def test_a_spell_is_one_miss_however_long():
    period = np.array([0, 1, 1, 1, 0, 0, 1, 0], dtype=float)
    assert ct.missed_infections(period, np.zeros(8, dtype=bool)) == 2


def test_partial_cover_does_not_split_a_spell_into_two_misses():
    period = np.array([1, 1, 1, 1, 1], dtype=float)
    middle_covered = np.array([False, False, True, False, False])
    assert ct.missed_infections(period, middle_covered) == 1
    assert ct.missed_infections(period, np.ones(5, dtype=bool)) == 0


def test_unknown_days_are_never_infections():
    period = np.array([NAN, NAN, 1, NAN], dtype=float)
    assert ct.missed_infections(period, np.zeros(4, dtype=bool)) == 1


# --- schedules -------------------------------------------------------------------


def test_always_keeps_every_day_covered():
    days = ct.always_schedule(30, cover_days=7)
    assert days == [0, 7, 14, 21, 28]
    assert ct.covered(30, days, 7).all()


def test_calendar_is_evenly_spaced_in_each_crop():
    assert ct.calendar_schedule(240, crop_days=120, sprays=4) == [0, 30, 60, 90, 120, 150, 180, 210]
    assert ct.calendar_schedule(100, crop_days=120, sprays=4) == [0, 30, 60, 90]


def test_model_sprays_the_day_after_a_high_forecast_and_not_while_covered():
    p48 = np.array([0.0, 0.9, 0.9, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9, 0.0])
    # Day 1 is high: spray day 2, covering 2-8. Days 2-3 are high but day 3-4 are covered.
    # Day 9 is high and day 10 is not covered: spray day 10.
    assert ct.model_schedule(p48, 0.5, cover_days=7) == [2, 10]


def test_no_forecast_never_triggers_a_spray():
    assert ct.model_schedule(np.array([NAN, NAN, NAN]), 0.0) == []


def test_the_last_day_cannot_spray_beyond_the_year():
    assert ct.model_schedule(np.array([0.0, 0.0, 0.9]), 0.5) == []


def test_play_adds_spray_costs_and_losses():
    period = np.array([0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 1], dtype=float)
    result = ct.play([0], period, spray=3000, loss=75_000, cover_days=7)
    # The spray on day 0 covers the spell on days 2-3; the spell on day 10 is missed.
    assert result == {"sprays": 1, "missed": 1, "cost": 3000 + 75_000}


def test_evaluate_policies_on_a_small_year():
    period = np.array([0, 0, 1, 1, 0, 0, 0, 0, 0, 0], dtype=float)
    p48 = np.array([0.8, 0.2, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    r = ct.evaluate_policies(p48, period, spray=3000, loss=75_000, f1_threshold=0.5,
                             cover_days=7, crop_days=10, calendar_sprays=2)
    assert r["never"] == {"sprays": 0, "missed": 1, "cost": 75_000}
    assert r["always"]["missed"] == 0 and r["always"]["sprays"] == 2
    assert r["model_cost"]["missed"] == 0  # threshold 0.04: day 0's 0.8 sprays day 1
    assert r["model_f1"]["sprays"] == 1


def test_cost_curve_matches_play_at_each_threshold():
    period = np.array([0, 1, 1, 0, 0, 1], dtype=float)
    p48 = np.array([0.6, 0.1, 0.0, 0.3, 0.0, 0.0])
    curve = ct.cost_by_threshold(p48, period, 1000, 20_000, [0.05, 0.5], cover_days=2)
    assert curve[0] == ct.play(ct.model_schedule(p48, 0.05, 2), period, 1000, 20_000, 2)["cost"]
    assert curve[1] == ct.play(ct.model_schedule(p48, 0.5, 2), period, 1000, 20_000, 2)["cost"]
