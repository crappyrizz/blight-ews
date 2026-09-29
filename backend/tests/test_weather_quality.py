"""Tests for ml/weather_quality.py, on a small fixture of hourly rows."""
from datetime import datetime, timedelta, timezone

from ml import weather_quality as wq


class Row:
    """Stand-in for a WeatherReading row."""

    def __init__(self, timestamp, temperature=12.0, humidity=90.0):
        self.timestamp = timestamp
        self.temperature = temperature
        self.humidity = humidity


def hours(start: datetime, count: int, **values) -> list[Row]:
    return [Row(start + timedelta(hours=h), **values) for h in range(count)]


# Start at 00:00 UTC, which is 03:00 local (Africa/Nairobi, UTC+3).
START = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def test_empty_input_reports_nothing_rather_than_failing():
    summary = wq.summarise([])

    assert summary["total_hours"] == 0
    assert summary["hutton_ready_days"] == 0
    assert "No rows" in wq.render_report(summary, "archive", -0.7, 36.6)


def test_coverage_and_range_are_reported():
    rows = hours(START, 72)  # three full days

    summary = wq.summarise(rows)

    assert summary["total_hours"] == 72
    assert summary["first"] == START
    assert summary["last"] == START + timedelta(hours=71)
    assert summary["out_of_range"] == []


def test_missing_hours_per_month_are_counted():
    # A whole March, minus 5 hours dropped from the middle.
    rows = hours(START, 24 * 31)
    del rows[100:105]

    summary = wq.summarise(rows)

    assert summary["missing_per_month"][(2026, 3)] == 5


def test_a_month_with_no_gaps_reports_zero_missing():
    rows = hours(START, 24 * 31)

    summary = wq.summarise(rows)

    assert summary["missing_per_month"][(2026, 3)] == 0


def test_partial_months_at_the_ends_are_not_counted_as_missing():
    """Coverage starting mid-month must not look like a huge gap."""
    rows = hours(datetime(2026, 3, 20, 0, 0, tzinfo=timezone.utc), 24 * 5)

    summary = wq.summarise(rows)

    assert summary["missing_per_month"][(2026, 3)] == 0


def test_out_of_range_values_are_flagged():
    rows = hours(START, 10)
    rows[2].temperature = 71.0
    rows[5].humidity = 130.0

    summary = wq.summarise(rows)

    assert [(field, value) for _, field, value in summary["out_of_range"]] == [
        ("temperature", 71.0), ("humidity", 130.0)
    ]


def test_days_are_counted_as_local_calendar_days():
    """00:00 UTC is 03:00 in Nairobi, so a UTC day spans two local days."""
    rows = hours(START, 24)

    summary = wq.summarise(rows)

    # 1 March 03:00 local through 2 March 02:00 local.
    assert sorted(str(day) for day in summary["day_hours"]) == ["2026-03-01", "2026-03-02"]


def test_hutton_ready_days_need_at_least_20_hours():
    # Five local days' worth, then thin one whole local day down to 19 hours.
    rows = hours(START, 24 * 6)
    local_day_to_thin = [
        r for r in rows
        if r.timestamp.astimezone(wq.LOCAL_TZ).date().isoformat() == "2026-03-03"
    ]
    for row in local_day_to_thin[:5]:  # 24 - 5 = 19 valid hours
        rows.remove(row)

    summary = wq.summarise(rows)

    # 144 hours from 03:00 local on 1 March covers 5 whole local days
    # (2-6 March); the partial first and last local days are excluded.
    assert summary["full_days"] == 5
    assert summary["hutton_ready_days"] == 4  # 3 March has only 19 hours
    assert summary["complete_days"] == 4


def test_report_states_the_hutton_share_and_the_gap_policy():
    rows = hours(START, 24 * 4)

    report = wq.render_report(wq.summarise(rows), "archive", -0.7, 36.6)

    assert "Days the Hutton rule can judge" in report
    assert "20 of 24" in report
    assert "Nothing is interpolated" in report
    assert "insufficient_data" in report


def test_plot_is_written(tmp_path):
    rows = hours(START, 24 * 31)
    del rows[10:20]
    path = tmp_path / "missing.png"

    result = wq.plot_missing_hours(wq.summarise(rows), path)

    assert result == path
    assert path.stat().st_size > 0


def test_no_plot_when_there_is_no_data(tmp_path):
    assert wq.plot_missing_hours(wq.summarise([]), tmp_path / "missing.png") is None
