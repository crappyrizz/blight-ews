"""Tests for ml/compare_sensor_vs_api.py: agreement, kappa, humid hours and the banner.

No database: readings are hand-built tables, as in test_hutton.py.
"""
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from backend import hutton
from ml import compare_sensor_vs_api as cmp

DAY1 = date(2026, 9, 1)


def day_readings(day: date, tmin: float = 12.0, humid_hours: int = 8, hours: int = 24) -> pd.DataFrame:
    """Hourly readings for one local day: `humid_hours` at 95 % RH, the rest at 70 %, minimum `tmin`."""
    start = datetime.combine(day, time.min, tzinfo=hutton.LOCAL_TZ)
    rows = []
    for h in range(hours):
        rows.append({
            "timestamp": (start + timedelta(hours=h)).astimezone(hutton.LOCAL_TZ),
            "temperature": tmin if h == 3 else tmin + 5,
            "humidity": 95.0 if h < humid_hours else 70.0,
        })
    return pd.DataFrame(rows)


def days(*specs) -> pd.DataFrame:
    """One spec per day from DAY1 - 1 (the lead-in day): (tmin, humid_hours) or None for no data."""
    frames = [day_readings(DAY1 + timedelta(days=i - 1), *spec) for i, spec in enumerate(specs) if spec]
    return pd.concat(frames, ignore_index=True)


# --- kappa ---------------------------------------------------------------------


def test_kappa_is_1_for_perfect_agreement():
    assert cmp.cohens_kappa(5, 0, 0, 20) == pytest.approx(1.0)


def test_kappa_is_0_for_chance_agreement():
    # Both say yes half the time, independently: agreement 0.5 = chance 0.5.
    assert cmp.cohens_kappa(25, 25, 25, 25) == pytest.approx(0.0)


def test_kappa_by_hand():
    # n = 50; observed (20 + 15) / 50 = 0.7; yes-rates 0.5 and 0.6;
    # chance 0.5 * 0.6 + 0.5 * 0.4 = 0.5; kappa (0.7 - 0.5) / 0.5 = 0.4.
    assert cmp.cohens_kappa(20, 5, 10, 15) == pytest.approx(0.4)


def test_kappa_is_undefined_when_both_always_say_no():
    assert cmp.cohens_kappa(0, 0, 0, 30) is None
    assert cmp.cohens_kappa(0, 0, 0, 0) is None


# --- agreement table -------------------------------------------------------------


def test_agreement_counts_each_cell_and_keeps_unknown_apart():
    index = [DAY1 + timedelta(days=i) for i in range(6)]
    sensor = pd.Series([True, True, False, False, None, True], index=index, dtype=object)
    api = pd.Series([True, False, True, False, False, None], index=index, dtype=object)
    a = cmp.agreement(sensor, api)
    assert (a["both_true"], a["sensor_only"], a["api_only"], a["both_false"]) == (1, 1, 1, 1)
    assert a["unknown"] == 2  # never counted as "not a Hutton day"
    assert a["judged"] == 4
    assert a["agreement"] == pytest.approx(0.5)


def test_compare_on_readings():
    # Lead-in, then: Hutton day on both; sensor too cold; both dry; sensor silent.
    sensor = days((12, 8), (12, 8), (9, 8), (12, 2), None)
    api = days((12, 8), (12, 8), (12, 8), (12, 2), (12, 8))
    result = cmp.compare(sensor, api, DAY1, DAY1 + timedelta(days=3))

    d = result["hutton_day"]
    assert (d["both_true"], d["api_only"], d["both_false"], d["unknown"]) == (1, 1, 1, 1)
    assert result["status_counts"]["sensor"]["no_data"] == 1
    assert result["disagreement_reasons"]["api_only"] == {"tmin": 1, "humidity": 0, "both": 0}
    # The lead-in day makes DAY1 a known period on both sources.
    assert result["hutton_period"]["both_true"] == 1


def test_humid_hour_difference_only_on_days_both_judged():
    sensor = days((12, 8), (12, 10), (12, 3), None)
    api = days((12, 8), (12, 6), (12, 5), (12, 5))
    h = cmp.compare(sensor, api, DAY1, DAY1 + timedelta(days=2))["humid_hours"]
    assert h["days"] == 2  # the sensor's silent day is left out, not counted as 0 hours
    assert h["mean"] == pytest.approx(((10 - 6) + (3 - 5)) / 2)
    assert h["mean_absolute"] == pytest.approx(3.0)
    assert (h["sensor_more_humid_days"], h["api_more_humid_days"]) == (1, 1)


def test_a_partial_day_is_unknown_not_a_disagreement():
    sensor = days((12, 8), (12, 8, 18))  # 18 hours: cannot be judged
    api = days((12, 8), (12, 8))
    result = cmp.compare(sensor, api, DAY1, DAY1)
    assert result["hutton_day"]["unknown"] == 1
    assert result["hutton_day"]["judged"] == 0
    assert result["hutton_day"]["kappa"] is None


# --- the simulated / real banner -------------------------------------------------


def test_simulated_is_the_default():
    # Claiming field results has to be a deliberate --sensor-data real.
    assert cmp.build_parser().parse_args([]).sensor_data == "simulated"
    assert cmp.build_parser().parse_args(["--sensor-data", "real"]).sensor_data == "real"
    with pytest.raises(SystemExit):
        cmp.build_parser().parse_args(["--sensor-data", "field"])
    assert "NOT FIELD RESULTS" in cmp.BANNERS["simulated"]


def test_report_opens_and_closes_with_the_banner():
    sensor = days((12, 8), (12, 8))
    result = cmp.compare(sensor, sensor, DAY1, DAY1)
    for kind in cmp.SENSOR_KINDS:
        text = cmp.render(result, kind, "NODE-1", "Kinangop plot")
        assert text.count(cmp.BANNERS[kind]) == 2
        assert text.index(cmp.BANNERS[kind]) < text.index("## Hutton days")
    assert "SIMULATED" not in cmp.render(result, "real", "NODE-1", "Kinangop plot")


def test_figure_is_written(tmp_path):
    sensor = days((12, 8), (12, 8), (9, 8))
    result = cmp.compare(sensor, sensor, DAY1, DAY1 + timedelta(days=1))
    path = tmp_path / "timeline.png"
    cmp.timeline_figure(result, "simulated", path)
    assert path.stat().st_size > 10_000
